"""Routes for taking reference photos from the camera: freeze the live picture, then name its faces.

The frozen picture and the faces found in it are kept in <data>/captures/ for a day, so the admin
can add several faces from one picture; adding goes through the same checks as an upload.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from . import current_user, failed

if TYPE_CHECKING:
    from ..core.pipeline import App

KEEP_S = 24 * 3600
TOKEN = re.compile(r"\d{14}_[0-9a-f]{6}")


def register(app: Flask, core: "App") -> None:
    cfg, store, service, library, live = core.cfg, core.store, core.service, core.library, core.live
    folder = core.data_dir / "captures"

    def load(token: str) -> dict:
        path = folder / f"{token}.json"
        if not TOKEN.fullmatch(token) or not path.exists():
            abort(404)
        return json.loads(path.read_text(encoding="utf-8"))

    def remove_old() -> None:
        for path in folder.glob("*"):
            if time.time() - path.stat().st_mtime > KEEP_S:
                path.unlink(missing_ok=True)

    @app.post("/live/capture")
    def capture():
        from ..vision.faces import encode_jpeg
        from ..vision.recognizer import crop_face

        frame = live.raw_frame()
        if frame is None:
            return failed("No camera picture right now: open this page during camera hours, or start a preview.",
                          "live_page")
        try:
            core.recognizer()
        except Exception as exc:
            return failed(f"Face models are not available: {exc}", "live_page")
        with core.recognizer_lock:
            found = core.recognizer().analyze_photo(frame)
        if not found:
            return failed("No face in the picture. Capture again when the person faces the camera.", "live_page")
        folder.mkdir(parents=True, exist_ok=True)
        remove_old()
        token = f"{service.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
        (folder / f"{token}.jpg").write_bytes(encode_jpeg(frame))
        faces = []
        for i, face in enumerate(sorted(found, key=lambda f: -f.size)):
            (folder / f"{token}_{i}.jpg").write_bytes(encode_jpeg(crop_face(frame, face.bbox)))
            matches = library.gallery.match(face.embedding, k=1)
            guess = matches[0][0] if matches and matches[0][1] >= cfg.recognition.accept_score else None
            faces.append({"bbox": [float(v) for v in face.bbox], "size": round(face.size), "ok": face.quality_ok,
                          "guess": guess, "added": None})
        (folder / f"{token}.json").write_text(json.dumps({"faces": faces}), encoding="utf-8")
        return redirect(url_for("capture_page", token=token))

    @app.get("/live/capture/<token>")
    def capture_page(token: str):
        return render_template("capture.html", token=token, faces=load(token)["faces"],
                               people=[e for e in store.all_employees() if e.active], names=store.employees())

    @app.post("/live/capture/<token>/<int:index>")
    def capture_add(token: str, index: int):
        from ..cli.enroll import read_image

        data = load(token)
        if not 0 <= index < len(data["faces"]):
            abort(404)
        employee_id = request.form.get("employee_id", "")
        person = store.employee(employee_id)
        if person is None:
            return failed("Choose who this is.", "capture_page", token=token)
        frame = read_image(folder / f"{token}.jpg")
        if frame is None:
            abort(404)
        x1, y1, x2, y2 = data["faces"][index]["bbox"]
        # A generous area around the chosen face: enough context for the detector, and it stays the largest face.
        w, h = x2 - x1, y2 - y1
        H, W = frame.shape[:2]
        region = frame[int(max(0, y1 - h)):int(min(H, y2 + h)), int(max(0, x1 - w)):int(min(W, x2 + w))]
        result = library.add_photo(employee_id, region, source="capture")
        if not result.ok:
            return failed(result.message, "capture_page", token=token)
        for warning in result.warnings:
            flash(warning, "warn")
        data["faces"][index]["added"] = employee_id
        (folder / f"{token}.json").write_text(json.dumps(data), encoding="utf-8")
        store.log(service.now(), current_user(cfg), "capture photo", f"{person.email}: from the camera")
        flash(result.message, "ok")
        return redirect(url_for("capture_page", token=token))
