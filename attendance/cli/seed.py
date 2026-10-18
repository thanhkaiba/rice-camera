"""Import unlabelled seed images for manual review, without recording attendance."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np

from .enroll import read_image
from ..store import body_photo, scene_photo, normalize_email


def _review_vector(app, path: Path):
    image = read_image(path)
    if image is None:
        raise ValueError(f"Review sample cannot be decoded: {path}")
    faces = app.recognizer().analyze_photo(image)
    if len(faces) == 1 and faces[0].det_score >= .5 and faces[0].size >= 40:
        return faces[0].embedding
    return None


def _review_metadata(app, row, seed, uid, report):
    sample = app.store.unknown(uid)
    if sample.source != "seed":
        return
    app.store.put(f"seed_review_email:{uid}", row.get("expected_email", ""))
    email = row.get("auto_resolve_email")
    if not email or sample.status != "pending":
        return
    report["auto_pending"] += 1
    if (app.store.get(f"seed_review_manual:{uid}") or sample.employee_id is not None
            or sample.model != app.service.model_id or sample.vector is None
            or row["auto_resolve_model"] != app.service.model_id):
        return
    person = app.store.employee_by_email(email)
    if person is None or not person.active:
        return
    # Recheck the actual photo; portable metadata alone never establishes identity.
    vector = _review_vector(app, seed / row["file"])
    if vector is None or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-12:
        return
    matches = app.gallery.match(vector)
    params = app.cfg.recognition
    if (len(matches) < 2 or matches[0][0] != person.id
            or matches[0][1] < max(.60, params.accept_score)
            or matches[0][1] - matches[1][1] < max(.12, params.min_margin)):
        return
    evidence = {"email": email, "model": app.service.model_id, "score": matches[0][1],
                "margin": matches[0][1] - matches[1][1], "sha256": row["sha256"]}
    if app.store.label_seed(uid, person.id, app.service.model_id, json.dumps(evidence), app.service.now()):
        report["resolved"] += 1
        report["auto_pending"] -= 1


def import_review_samples(app, seed: Path) -> dict:
    if not (seed / "review.csv").exists():
        return {"imported": 0, "skipped": 0, "refreshed": 0, "resolved": 0, "auto_pending": 0}
    seed = seed.resolve()
    manifest = json.loads((seed / "manifest.json").read_text(encoding="utf-8"))
    with (seed / "review.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    # Validate the entire batch before adding rows or copying sample files.
    for relative in ["review.csv"] + [r[key] for r in rows for key in ("file", "body_file", "scene_file") if r.get(key)]:
        path = (seed / relative).resolve()
        if (not path.is_relative_to(seed) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get(relative)):
            raise ValueError(f"Review seed file missing or changed: {relative}")
    for row in rows:
        if row["sha256"] != manifest[row["file"]]:
            raise ValueError("Review sample hash differs from manifest")
        stamp = dt.datetime.fromisoformat(row["captured_at"])
        if stamp.tzinfo is None:
            raise ValueError("Review timestamps must include a timezone")
        for key in ("expected_email", "auto_resolve_email"):
            row[key] = normalize_email(row[key]) if row.get(key) else ""
        if row["auto_resolve_email"] and (row["auto_resolve_email"] != row["expected_email"]
                                          or not row.get("auto_resolve_model")):
            raise ValueError("Seed auto-resolution email must match the source email and include its model")
    existing = {}
    for face in app.store.unknowns(status=None):
        path = app.data_dir / face.image if face.image else None
        if path and path.is_file():
            existing[hashlib.sha256(path.read_bytes()).hexdigest()] = face.id
    report = {"imported": 0, "skipped": 0, "refreshed": 0, "resolved": 0, "auto_pending": 0}
    for row in rows:
        digest = row["sha256"]
        key = "seed_review:" + digest
        if app.store.get(key) or digest in existing:
            if digest in existing:
                sample = app.store.unknown(existing[digest])
                if sample.source == "seed" and sample.status == "pending" and sample.vector is None:
                    vector = _review_vector(app, seed / row["file"])
                    if vector is not None:
                        report["refreshed"] += app.store.refresh_seed_vector(sample.id, app.service.model_id, vector)
                app.store.put(key, str(existing[digest]))
                _review_metadata(app, row, seed, existing[digest], report)
            report["skipped"] += 1
            continue
        source = seed / row["file"]
        vector = _review_vector(app, source)
        relative = "review-seed/" + digest + source.suffix.lower()
        target = app.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        for column, sidecar in (("body_file", body_photo), ("scene_file", scene_photo)):
            if row.get(column):
                shutil.copyfile(seed / row[column], app.data_dir / sidecar(relative))
        stamp = dt.datetime.fromisoformat(row["captured_at"]).astimezone(app.cfg.tz)
        uid = app.store.add_unknown(stamp.date(), stamp, relative, app.service.model_id,
                                    vector, None, None, source="seed")
        app.store.put(key, str(uid))
        existing[digest] = uid
        _review_metadata(app, row, seed, uid, report)
        report["imported"] += 1
        if report["imported"] % 25 == 0:
            print(f'Review samples imported: {report["imported"]}', flush=True)
    return report
