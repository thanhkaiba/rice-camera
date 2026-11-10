"""The API the office Mac calls. Only the Mac holds the token; the server stores just its SHA-256."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid
from functools import wraps
from typing import TYPE_CHECKING

import numpy as np
from flask import Flask, Response, abort, jsonify, request

if TYPE_CHECKING:
    from ..core.pipeline import App

log = logging.getLogger(__name__)

ENDPOINTS = {"edge_events", "edge_photo", "edge_heartbeat", "healthz"}
HEARTBEAT_KEY = "edge_heartbeat"
MAX_EVENTS = 200
PHOTO_PATH = re.compile(r"crops/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_\-]+\.jpg")


def _vector(value: str | None) -> np.ndarray | None:
    if value is None:
        return None
    vector = np.frombuffer(base64.b64decode(value, validate=True), dtype=np.float32)
    if not vector.size:
        raise ValueError("empty vector")
    return vector.copy()


def _pair(value) -> tuple | None:
    return None if value is None else tuple(value)


def _when(value: str) -> dt.datetime:
    ts = dt.datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("ts needs a UTC offset")
    return ts


def _image(value: str | None) -> str | None:
    if value is not None and not PHOTO_PATH.fullmatch(value):
        raise ValueError("bad image path")
    return value


def heartbeat(store) -> dict | None:
    raw = store.get(HEARTBEAT_KEY)
    return json.loads(raw) if raw else None


def register(app: Flask, core: "App", token_sha256: str) -> None:
    store, service = core.store, core.service
    ingest = threading.Lock()  # events are applied one at a time, in the order they arrived

    def from_the_mac(view):
        @wraps(view)
        def checked(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            presented = hashlib.sha256(header.removeprefix("Bearer ").encode()).hexdigest()
            if not header.startswith("Bearer ") or not hmac.compare_digest(presented, token_sha256):
                abort(401)
            return view(*args, **kwargs)
        return checked

    def apply(event: dict) -> None:
        ts, image = _when(event["ts"]), _image(event.get("image"))
        vector = _vector(event.get("vector"))
        if event["kind"] == "identified":
            service.on_identified(str(event["employee_id"]), ts, float(event["score"]), image, vector, event.get("sha256"))
        elif event["kind"] == "unknown":
            service.on_unknown(ts, image, vector, event.get("guess_id"), event.get("guess_score"), event.get("sha256"),
                               event.get("quality"), track=_pair(event.get("track")), follows=_pair(event.get("follows")),
                               frames=event.get("frames"), votes=event.get("votes"))
        else:
            raise ValueError("unknown kind")

    @app.post("/api/edge/events")
    @from_the_mac
    def edge_events():
        events = (request.get_json(silent=True) or {}).get("events")
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            abort(400, "expected a list of events")
        accepted, rejected = [], []
        with ingest:
            for event in events:
                event_id = event.get("id") if isinstance(event, dict) else None
                if not event_id:
                    abort(400, "every event needs an id")
                if not store.edge_applied(event_id):
                    try:
                        apply(event)
                    except (KeyError, TypeError, ValueError) as exc:
                        log.warning("Refused event %s: %s", event_id, exc)
                        rejected.append({"id": event_id, "reason": type(exc).__name__})
                        continue
                    store.edge_mark_applied(event_id, service.now())
                accepted.append(event_id)
        return jsonify(accepted=accepted, rejected=rejected)

    @app.put("/api/edge/photos/<path:rel>")
    @from_the_mac
    def edge_photo(rel: str):
        if not PHOTO_PATH.fullmatch(rel):
            abort(400, "bad photo path")
        data = request.get_data()
        if not data.startswith(b"\xff\xd8"):
            abort(415, "not a JPEG")
        path = core.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{uuid.uuid4().hex}.part")
        partial.write_bytes(data)
        os.replace(partial, path)
        return jsonify(ok=True)

    @app.post("/api/edge/heartbeat")
    @from_the_mac
    def edge_heartbeat():
        beat = request.get_json(silent=True) or {}
        store.put(HEARTBEAT_KEY, json.dumps({"at": service.now().isoformat(), "camera_ok": bool(beat.get("camera_ok")),
                                             "camera_state": str(beat.get("camera_state", ""))[:200],
                                             "version": str(beat.get("version", ""))[:40],
                                             "backlog": int(beat.get("backlog") or 0)}))
        return jsonify(ok=True)

    @app.get("/healthz")
    def healthz():
        return Response("ok\n", mimetype="text/plain")
