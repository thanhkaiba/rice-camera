"""Reference photos: upload, delete, learn from admin confirmations, re-embed, look-alike checks.

Reference photos are saved under <data>/faces/<employee>/ so they survive the retention
clean-up of camera snapshots and can be re-embedded when the recognition model changes.
"""
from __future__ import annotations

import datetime as dt
import logging
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .config import Config
from .gallery import Gallery
from .store import Store

log = logging.getLogger(__name__)

MIN_FACE_PX = 40
MIN_DET_SCORE = 0.5
NEAR_DUPLICATE = 0.97  # a learned face this similar to a kept one is not stored again


@dataclass
class AddResult:
    ok: bool
    message: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class LookAlike:
    a: str
    b: str
    score: float


def encode_jpeg(image) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError("could not encode image")
    return buf.tobytes()


def decode_image(data: bytes):
    import cv2

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        raise ValueError("not a readable image file")
    return image


class FaceLibrary:
    def __init__(self, cfg: Config, store: Store, gallery: Gallery, recognizer: Callable[[], object],
                 lock: threading.Lock, alert: Callable[[str], None] = lambda text: None,
                 now: Callable[[], dt.datetime] | None = None):
        """`recognizer` returns the FaceRecognizer (loaded lazily); `lock` serialises its use with the camera loop."""
        self.cfg = cfg
        self.store = store
        self.gallery = gallery
        self._recognizer = recognizer
        self._lock = lock
        self._alert = alert
        self._now = now or (lambda: dt.datetime.now(cfg.tz))
        self.data_dir = cfg.path(cfg.data_dir)

    @property
    def model_id(self) -> str:
        return self.cfg.recognition.model_id()

    def _largest_face(self, image):
        with self._lock:
            return self._recognizer().largest_face(image, mirror=True)

    def _save(self, employee_id: str, jpeg: bytes) -> str:
        rel = Path("faces") / employee_id / f"{self._now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}.jpg"
        path = self.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg)
        return rel.as_posix()

    def add_photo(self, employee_id: str, image, source: str = "upload") -> AddResult:
        from .recognizer import crop_face

        name = self.store.employees().get(employee_id) or employee_id
        face = self._largest_face(image)
        if face is None:
            return AddResult(False, "No face found in this photo.")
        if face.det_score < MIN_DET_SCORE or face.size < MIN_FACE_PX:
            return AddResult(False, f"The face is too small or unclear ({face.size:.0f}px). Use a closer photo.")
        if self.near_duplicate(employee_id, face.embedding, self.model_id):
            return AddResult(False, f"Almost the same as a photo {name} already has; a different angle, light "
                                    f"or expression adds more.")
        warnings = []
        if not face.quality_ok:
            warnings.append("The face is blurred or turned away; a sharper, frontal photo works better.")
        self._keep(employee_id, face.embedding, encode_jpeg(crop_face(image, face.bbox)), source, self.model_id)
        for other, score in self.similar_to(employee_id, face.embedding):
            other_name = self.store.employees().get(other, other)
            text = (f"This photo of {name} looks like {other_name} ({score:.0%} match), so the camera may "
                    f"confuse them. Check that it really is {name}.")
            warnings.append(text)
            self._alert(f"⚠️ Look-alike reference photos: {name} and {other_name} ({score:.0%} match). "
                        f"Check the photos on the People page.")
        return AddResult(True, f"Photo added for {name}.", warnings)

    def learn(self, employee_id: str, vector: np.ndarray, snapshot: str | None, model: str) -> bool:
        """Keep a face confirmed by the admin as a reference (copying the snapshot out of crops/).
        A face almost identical to one already kept adds nothing and is skipped, so the references
        stay varied instead of filling up with the same pose. Returns whether it was kept."""
        if self.near_duplicate(employee_id, vector, model):
            return False
        jpeg = (self.data_dir / snapshot).read_bytes() if snapshot and (self.data_dir / snapshot).exists() else None
        self._keep(employee_id, vector, jpeg, "admin", model)
        return True

    def keep_face(self, employee_id: str, vector: np.ndarray, image, source: str = "enrol") -> bool:
        """Keep a face seen by the camera (its crop, as an image) as a reference photo, unless an
        almost identical one is already kept. Returns whether it was kept."""
        if self.near_duplicate(employee_id, vector, self.model_id):
            return False
        self._keep(employee_id, vector, encode_jpeg(image), source, self.model_id)
        return True

    def near_duplicate(self, employee_id: str, vector: np.ndarray, model: str) -> bool:
        """Whether this person already has a reference almost identical to `vector` (same photo,
        or the same frame twice): it would add nothing but weight to that one view."""
        v = vector / max(float(np.linalg.norm(vector)), 1e-12)
        for ref in self.store.references(model, employee_id):
            if float(np.dot(v, ref.vector / max(float(np.linalg.norm(ref.vector)), 1e-12))) >= NEAR_DUPLICATE:
                return True
        return False

    def _keep(self, employee_id: str, vector: np.ndarray, jpeg: bytes | None, source: str, model: str) -> None:
        image_rel = self._save(employee_id, jpeg) if jpeg else None
        self.store.add_embedding(model, employee_id, vector, source, self._now(), image_rel)
        if model == self.model_id:
            self.gallery.add(employee_id, vector)

    def delete_photo(self, image_rel: str) -> None:
        self.store.delete_reference_image(image_rel)
        path = self.data_dir / image_rel
        if image_rel.startswith("faces/") and path.exists():
            path.unlink()
        self.reload()

    def delete_employee(self, employee_id: str) -> None:
        self.store.delete_employee(employee_id)
        shutil.rmtree(self.data_dir / "faces" / employee_id, ignore_errors=True)
        self.reload()

    def reload(self) -> None:
        self.gallery.replace(self.store.embeddings(self.model_id))

    def reembed_missing(self) -> int:
        """Embed stored reference photos that have no embedding for the current model yet
        (e.g. after switching backend). Returns how many were added."""
        from .enroll import read_image

        have = {r.image for r in self.store.references(self.model_id) if r.image}
        added = 0
        for employee_id, image_rel, source in self.store.reference_images():
            if image_rel in have or not (self.data_dir / image_rel).exists():
                continue
            image = read_image(self.data_dir / image_rel)
            face = self._largest_face(image) if image is not None else None
            if face is None:
                log.warning("No face found when re-embedding %s", image_rel)
                continue
            self.store.add_embedding(self.model_id, employee_id, face.embedding, source, self._now(), image_rel)
            added += 1
        if added:
            log.info("Embedded %d reference photos for model %s", added, self.model_id)
            self.reload()
        return added

    def similar_to(self, employee_id: str, vector: np.ndarray) -> list[tuple[str, float]]:
        threshold = self.cfg.recognition.accept_score
        out = []
        vector = self.gallery.prepare(vector)
        for other, refs in self.gallery.snapshot().items():
            if other == employee_id:
                continue
            score = float(np.max(refs @ vector))
            if score >= threshold:
                out.append((other, score))
        return sorted(out, key=lambda x: -x[1])

    def look_alikes(self) -> list[LookAlike]:
        """Pairs of active employees whose reference photos match each other at the acceptance threshold."""
        threshold = self.cfg.recognition.accept_score
        items = sorted(self.gallery.snapshot().items())
        pairs = []
        for i, (a, ra) in enumerate(items):
            for b, rb in items[i + 1:]:
                score = float(np.max(ra @ rb.T))
                if score >= threshold:
                    pairs.append(LookAlike(a, b, score))
        return sorted(pairs, key=lambda p: -p.score)
