"""Reference photos: upload, delete, learn from admin confirmations, re-embed, look-alike checks.

Reference photos are saved under <data>/faces/<employee>/ so they survive the retention
clean-up of camera snapshots and can be re-embedded when the recognition model changes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..core.config import Config
from .gallery import Gallery, normalize
from ..store import Store

log = logging.getLogger(__name__)

MIN_FACE_PX = 40
MIN_DET_SCORE = 0.5
NEAR_DUPLICATE = 0.97  # a learned face this similar to a kept one is not stored again
GOOD_PHOTOS = 5  # sharp, frontal reference photos a person needs; fewer is flagged on the People page
BLURRED = "The face is blurred or turned away; a sharper, frontal photo works better."


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


@dataclass
class ReferenceCheck:
    status: str  # useful, review, unverified: reference consistency, not measured camera accuracy
    reason: str
    own: float | None
    other_id: str | None
    other: float | None
    duplicate_of: int | None

    @property
    def margin(self) -> float | None:
        return None if self.own is None else self.own - (self.other if self.other is not None else -1.0)


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
        self.rate_in_background = False  # set by the running program: check new photos' quality in a thread
        self._rating = threading.Lock()

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
        warnings = []
        if not face.quality_ok:
            warnings.append(BLURRED)
        image_rel = self._save(employee_id, encode_jpeg(crop_face(image, face.bbox)))
        self.store.add_embedding(self.model_id, employee_id, face.embedding, source, self._now(), image_rel,
                                 face.quality_ok, face.quality)
        self.gallery.add(employee_id, face.embedding)
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
        v = vector / max(float(np.linalg.norm(vector)), 1e-12)
        for ref in self.store.references(model, employee_id):
            if float(np.dot(v, ref.vector / max(float(np.linalg.norm(ref.vector)), 1e-12))) >= NEAR_DUPLICATE:
                return False
        image_rel = None
        if snapshot and (self.data_dir / snapshot).exists():
            image_rel = self._save(employee_id, (self.data_dir / snapshot).read_bytes())
        self.store.add_embedding(model, employee_id, vector, "admin", self._now(), image_rel)
        if model == self.model_id:
            self.gallery.add(employee_id, vector)
        if image_rel:
            self.rate_soon()
        return True

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
        from ..cli.enroll import read_image

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
            self.store.add_embedding(self.model_id, employee_id, face.embedding, source, self._now(), image_rel,
                                     face.quality_ok, face.quality)
            added += 1
        if added:
            log.info("Embedded %d reference photos for model %s", added, self.model_id)
            self.reload()
        return added

    def rate_soon(self) -> None:
        if self.rate_in_background and not self._rating.locked():
            threading.Thread(target=self.rate_missing, name="photo-quality", daemon=True).start()

    def rate_missing(self) -> int:
        """Check the sharpness and pose of reference photos saved before that was recorded (or
        learned from the camera), once, so the People page can count the good ones. Returns how many."""
        from ..cli.enroll import read_image

        if not self._rating.acquire(blocking=False):
            return 0
        rated = 0
        try:
            for image_rel in sorted({r.image for r in self.store.references(self.model_id)
                                     if r.image and (r.quality_ok is None or r.quality_score is None)}):
                path = self.data_dir / image_rel
                image = read_image(path) if path.exists() else None
                if image is None:
                    continue
                face = self._largest_face(image)  # no face found any more: not a good photo either
                self.store.set_reference_quality(image_rel, face is not None and face.quality_ok,
                                                 face.quality if face is not None else 0.0)
                rated += 1
        finally:
            self._rating.release()
        if rated:
            log.info("Checked the quality of %d reference photos", rated)
        return rated

    def quality(self, employee_id: str | None = None) -> dict[str, tuple[int, int, int]]:
        """employee_id -> (photos, good, not checked yet) for the current model."""
        out: dict[str, list[int]] = {}
        for r in self.store.references(self.model_id, employee_id):
            n = out.setdefault(r.employee_id, [0, 0, 0])
            n[0] += 1
            n[1] += r.quality_ok is True
            n[2] += r.quality_ok is None and r.image is not None  # without a photo it cannot be checked
        return {k: (a, b, c) for k, (a, b, c) in out.items()}

    def _quality_signature(self) -> str:
        refs = self.store.references(self.model_id)
        rc = self.cfg.recognition
        metadata = [self.model_id, rc.center_embeddings, rc.accept_score, rc.min_margin, NEAR_DUPLICATE,
                    sorted(self.store.employees()),
                    [(r.id, r.employee_id, r.image, r.quality_ok, r.quality_score) for r in refs]]
        digest = hashlib.sha256(json.dumps(metadata).encode())
        for ref in refs:
            digest.update(ref.vector.tobytes())
        return digest.hexdigest()

    def reevaluate_quality(self) -> int:
        """Explicitly assess references and persist the complete report for fast, read-only pages."""
        signature = self._quality_signature()
        checks = self.recognition_quality()
        gallery = Gallery(self.store.embeddings(self.model_id), center=self.cfg.recognition.center_embeddings)
        look_alikes = self.look_alikes(gallery)
        if signature != self._quality_signature():
            raise ValueError("Photos or recognition settings changed during evaluation. Please try again.")
        self.store.put("reference_quality", json.dumps({
            "signature": signature, "checked_at": self._now().isoformat(timespec="seconds"),
            "checks": {ref_id: asdict(check) for ref_id, check in checks.items()},
            "look_alikes": [asdict(pair) for pair in look_alikes],
        }))
        return len(checks)

    def saved_quality(self) -> dict:
        """Read the last explicit report; changed inputs require another button press, not a GET job."""
        report = {"checks": {}, "look_alikes": [], "checked_at": None, "stale": False}
        raw = self.store.get("reference_quality")
        if not raw:
            return report
        saved = json.loads(raw)
        report["checked_at"] = saved["checked_at"]
        report["stale"] = saved["signature"] != self._quality_signature()
        if not report["stale"]:
            report["checks"] = {int(ref_id): ReferenceCheck(**check) for ref_id, check in saved["checks"].items()}
            report["look_alikes"] = [LookAlike(**pair) for pair in saved["look_alikes"]]
        return report

    def recognition_quality(self, employee_id: str | None = None) -> dict[int, ReferenceCheck]:
        """Check each reference against other photos using the camera's matching rules.
        Self and near-duplicates are held out together; inactive colleagues cannot cause a conflict.
        These are consistency checks on the reference labels, not independent accuracy measurements.
        """
        refs = self.store.references(self.model_id)
        active = self.store.employees()
        vectors = {r.id: normalize(r.vector) for r in refs}
        by_person = {}
        for ref in refs:
            by_person.setdefault(ref.employee_id, []).append(ref)
        matrices = {p: np.stack([vectors[r.id] for r in rows]) for p, rows in by_person.items()}
        active_matrices = {p: m for p, m in matrices.items() if p in active}
        rc = self.cfg.recognition
        out = {}
        for ref in refs:
            if employee_id is not None and ref.employee_id != employee_id:
                continue
            own_refs = by_person[ref.employee_id]
            held_out = np.array([r.id == ref.id or float(vectors[r.id] @ vectors[ref.id]) >= NEAR_DUPLICATE
                                 for r in own_refs])
            similar = [r for r, held in zip(own_refs, held_out) if held]
            representative = max(similar, key=lambda r: (r.quality_score if r.quality_score is not None else -1, -r.id))
            duplicate = representative.id if representative.id != ref.id else None
            grouped = dict(active_matrices)
            remaining = matrices[ref.employee_id][~held_out]
            if len(remaining):
                grouped[ref.employee_id] = remaining
            else:
                grouped.pop(ref.employee_id, None)
            gallery = Gallery.from_normalized(grouped, center=rc.center_embeddings)
            candidates = gallery.match(ref.vector, k=len(active) + 1)
            own = dict(candidates).get(ref.employee_id)
            other_id, other = next(((p, s) for p, s in candidates if p != ref.employee_id), (None, None))
            gap = None if own is None else own - (other if other is not None else -1.0)
            if own is None and other is not None and other >= rc.accept_score:
                status, reason = "review", "Strong match to another employee without independent evidence for this person; check the identity."
            elif own is None:
                status, reason = "unverified", "Needs a different photo of this person for comparison."
            elif other is not None and other >= rc.accept_score and other - own >= rc.min_margin:
                status, reason = "review", "Matches another employee more strongly; check the identity."
            elif own < rc.accept_score:
                status, reason = "review", "Weak match to this person's other photos; check the identity or add another angle."
            elif gap < rc.min_margin:
                status, reason = "review", "Not enough separation from another employee; check for a misplaced photo."
            else:
                status, reason = "useful", "Matches this person's other photos with enough separation."
            out[ref.id] = ReferenceCheck(status, reason, own, other_id, other, duplicate)
        return out

    def look_alikes_of(self, employee_id: str) -> list[tuple[str, float]]:
        """(other employee, score) whose reference photos match this person's at the acceptance threshold."""
        return [(p.b if p.a == employee_id else p.a, p.score) for p in self.look_alikes()
                if employee_id in (p.a, p.b)]

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

    def look_alikes(self, gallery: Gallery | None = None) -> list[LookAlike]:
        """Pairs of active employees whose reference photos match each other at the acceptance threshold."""
        threshold = self.cfg.recognition.accept_score
        items = sorted((gallery if gallery is not None else self.gallery).snapshot().items())
        pairs = []
        for i, (a, ra) in enumerate(items):
            for b, rb in items[i + 1:]:
                score = float(np.max(ra @ rb.T))
                if score >= threshold:
                    pairs.append(LookAlike(a, b, score))
        return sorted(pairs, key=lambda p: -p.score)
