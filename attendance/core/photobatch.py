"""Faces from a .zip of photos (a trip, a team day), for Review: People -> "Faces from photos".

The server keeps the upload and gives the camera computer a job (only it has the face models). Here every
photo is looked at in tiles, so a face in a group picture is not too small for the detector, and a face is
kept only when it passes the checks the camera uses for a good photo: big enough, sharp, not turned too
far (quality_ok), clearly detected, nothing over the mouth and chin (looks_covered), and a crop around it
holding that face alone. The same person shows up in many photos: at most PER_PERSON clearly different
faces of one person are kept. Each kept face goes to Review as a "seed" sample: no attendance; confirming
it as someone teaches the camera that face.
"""
from __future__ import annotations

import copy
import logging
import zipfile
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

IMAGES = (".jpg", ".jpeg", ".png")
MAX_PHOTO_BYTES = 60 * 1024 * 1024
MAX_PHOTOS = 2000
LONG_SIDE, TILE, STEP = 3000, 1100, 800  # photos scaled to 3000 px, looked at in overlapping 1100 px tiles
MIN_FACE = 60  # px in the photo
CROP_SIDE = 360
PER_PERSON, SAME_PERSON, TOO_ALIKE = 3, 0.5, 0.85
MAX_FACES = 300  # sent to Review from one upload


def _iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def extract(cfg, archive: Path, out: Path, stem: str, recognizer=None) -> tuple[list[dict], dict]:
    """Faces worth reviewing in the photos of `archive`: [{"file", "scene", "vector" (np.float32)}], files written
    to `out` as <stem>_<n>.jpg and <stem>_<n>_scene.jpg (the photo, face boxed), and the counts of what was skipped."""
    import cv2

    from ..vision.recognizer import FaceRecognizer, looks_covered

    if recognizer is None:  # its own models, on the CPU: the camera keeps the shared one
        rc = copy.deepcopy(cfg.recognition)
        rc.use_gpu, rc.cpu_threads = False, 2
        recognizer = FaceRecognizer(rc, cfg.base_dir)
    out.mkdir(parents=True, exist_ok=True)
    stats = {"photos": 0, "unreadable": 0, "faces": 0, "kept": 0, "small": 0, "unclear": 0, "covered": 0,
             "crowded": 0, "repeat": 0}
    groups: list[list] = []  # [first embedding, kept embeddings]
    kept: list[dict] = []
    with zipfile.ZipFile(archive) as z:
        members = [m for m in z.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in IMAGES
                   and not Path(m.filename).name.startswith(".") and "__MACOSX" not in m.filename]
        for member in sorted(members, key=lambda m: m.filename)[:MAX_PHOTOS]:
            if len(kept) >= MAX_FACES:
                break
            if member.file_size > MAX_PHOTO_BYTES:
                stats["unreadable"] += 1
                continue
            img = cv2.imdecode(np.frombuffer(z.read(member), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                stats["unreadable"] += 1
                continue
            stats["photos"] += 1
            scale = min(1.0, LONG_SIDE / max(img.shape[:2]))
            small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
            found: list = []
            h, w = small.shape[:2]
            for y in range(0, max(1, h - TILE + STEP), STEP):
                for x in range(0, max(1, w - TILE + STEP), STEP):
                    for f in recognizer.analyze(small[y:y + TILE, x:x + TILE]):
                        box = [(f.bbox[0] + x) / scale, (f.bbox[1] + y) / scale,
                               (f.bbox[2] + x) / scale, (f.bbox[3] + y) / scale]
                        if not any(_iou(box, other) > 0.3 for other in found):
                            found.append(box)
            for box in found:
                stats["faces"] += 1
                face = _keep(recognizer, img, box, stats, looks_covered)
                if face is None:
                    continue
                crop, embedding = face
                group = next((g for g in groups if float(g[0] @ embedding) >= SAME_PERSON), None)
                if group is None:
                    groups.append([embedding, [embedding]])
                elif len(group[1]) >= PER_PERSON or max(float(k @ embedding) for k in group[1]) >= TOO_ALIKE:
                    stats["repeat"] += 1
                    continue
                else:
                    group[1].append(embedding)
                name = f"{stem}_{len(kept):03d}"
                cv2.imwrite(str(out / f"{name}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
                r = 1280 / max(img.shape[:2])
                scene = cv2.resize(img, None, fx=r, fy=r, interpolation=cv2.INTER_AREA) if r < 1 else img.copy()
                r = min(r, 1.0)
                cv2.rectangle(scene, (int(box[0] * r), int(box[1] * r)), (int(box[2] * r), int(box[3] * r)), (0, 200, 255), 3)
                cv2.imwrite(str(out / f"{name}_scene.jpg"), scene, [cv2.IMWRITE_JPEG_QUALITY, 85])
                kept.append({"file": f"{name}.jpg", "scene": f"{name}_scene.jpg",
                             "vector": np.asarray(embedding, np.float32), "photo": Path(member.filename).name})
                stats["kept"] += 1
    stats["people"] = len(groups)
    log.info("Faces from photos: %s", stats)
    return kept, stats


def _keep(recognizer, img, box, stats, looks_covered):
    """(crop, embedding) of the face in `box` when it is good enough to review, else None (counted in stats)."""
    import cv2

    x1, y1, x2, y2 = box
    if min(x2 - x1, y2 - y1) < MIN_FACE:
        stats["small"] += 1
        return None
    mw, mh = (x2 - x1) * 0.3, (y2 - y1) * 0.3
    crop = img[int(max(0, y1 - mh)):int(min(img.shape[0], y2 + mh)), int(max(0, x1 - mw)):int(min(img.shape[1], x2 + mw))]
    k = CROP_SIDE / max(crop.shape[:2])
    if k < 1:
        crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    faces = recognizer.analyze_photo(crop)
    if len(faces) != 1:
        stats["crowded"] += 1
        return None
    f = faces[0]
    if not (f.quality_ok and f.det_score >= 0.6 and f.size >= 60):
        stats["unclear"] += 1
        return None
    if looks_covered(crop, f.landmarks):
        stats["covered"] += 1
        return None
    return crop, f.embedding


def is_zip(path: Path) -> bool:
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


STATUS_KEY = "faces_from_photos"  # the last upload: {"state", "filename", "at", "message", "cards"}


def add_to_review(core, samples: list, model: str) -> int:
    """Review cards ("seed" samples: no attendance, learned once confirmed) for the faces the camera computer
    sent back; their pictures were uploaded beside them. Returns how many were added."""
    import base64
    import datetime as dt

    from ..store import scene_photo

    store, now = core.store, core.service.now()
    added = 0
    for n, sample in enumerate(samples):
        rel = str(sample.get("image", ""))
        if not (rel.startswith("crops/") and rel.endswith(".jpg") and ".." not in rel
                and (core.data_dir / rel).is_file()):
            continue
        try:
            vector = np.frombuffer(base64.b64decode(sample["vector"]), dtype=np.float32).copy()
        except (KeyError, ValueError, TypeError):
            continue
        if vector.size < 64 or not np.all(np.isfinite(vector)) or float(np.linalg.norm(vector)) < 1e-6:
            continue
        if not (core.data_dir / scene_photo(rel)).is_file():
            log.info("Faces from photos: %s came without its photo", rel)
        ts = now + dt.timedelta(seconds=n)
        store.add_unknown(ts.date(), ts, rel, model, vector / np.linalg.norm(vector), None, None, source="seed")
        added += 1
    return added
