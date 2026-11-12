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
import sqlite3
import threading
import uuid
from functools import wraps
from typing import TYPE_CHECKING

import numpy as np
from flask import Flask, Response, abort, jsonify, request, send_from_directory

if TYPE_CHECKING:
    from ..core.pipeline import App

from ..core import settings

log = logging.getLogger(__name__)

ENDPOINTS = {"edge_events", "edge_photo", "edge_heartbeat", "edge_state", "edge_sync", "edge_sync_embeddings",
             "edge_file", "edge_job_result", "healthz"}
UPLOAD_PATH = re.compile(r"uploads/[0-9a-f]{32}")
MAX_JOB_EMBEDDINGS = 500
# What the Mac needs to run the camera. The detection area is drawn on the Mac and stays there.
MAC_SECTIONS = ("schedule", "camera", "tracking", "recognition", "collect", "greeting")
SHADOW_DIFF_KEY = "shadow_diff"
MAX_DIFF_LISTED = 50
HEARTBEAT_KEY = "edge_heartbeat"
MAX_EVENTS = 200
PHOTO_PATH = re.compile(r"crops/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_\-]+\.jpg|faces/[0-9a-f]{32}/\d{14}_[0-9a-f]{6}\.jpg")


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


def _greeting_status(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    result = {"enabled": bool(value.get("enabled")), "ready": int(value.get("ready") or 0), "voice": bool(value.get("voice")),
            "spoken": int(value.get("spoken") or 0), "error": str(error)[:300] if error else None}
    if isinstance(value.get("voices"), list):
        result["voices"] = sorted({name for name in value["voices"][:1000]
                                   if isinstance(name, str) and name != "auto"
                                   and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name)})
    daily = value.get("daily_voice")
    if isinstance(daily, dict) and daily.get("name") in result.get("voices", []):
        try:
            at = dt.datetime.fromisoformat(daily.get("at", ""))
            if at.tzinfo is not None:
                result["daily_voice"] = {"name": daily["name"], "at": at.isoformat()}
        except (TypeError, ValueError):
            pass
    return result


def heartbeat(store) -> dict | None:
    raw = store.get(HEARTBEAT_KEY)
    return json.loads(raw) if raw else None


def _instants(times) -> set[float]:
    return {dt.datetime.fromisoformat(t).timestamp() for t in times}


def _note_differences(store, day: str, ours: set[str], theirs: set[str]) -> None:
    """Shadow check: camera arrivals one side has and the other does not, kept until someone looks."""
    only_server, only_mac = _instants(ours) - _instants(theirs), _instants(theirs) - _instants(ours)
    report = json.loads(store.get(SHADOW_DIFF_KEY) or "{}")
    entry = report.setdefault(day, {"only_server": [], "only_mac": []})
    entry["mac_camera"] = len(theirs)
    for name, found in (("only_server", only_server), ("only_mac", only_mac)):
        entry[name] = sorted({*entry[name], *found})[:MAX_DIFF_LISTED]
    store.put(SHADOW_DIFF_KEY, json.dumps(report))


def register(app: Flask, core: "App", token_sha256: str, shadow: bool = False) -> None:
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
        if event["kind"] == "unknown" and event.get("source") == "collect":
            store.add_unknown(ts.date(), ts, image, core.cfg.recognition.model_id(), vector, event.get("guess_id"),
                              event.get("guess_score"), source="collect", quality=event.get("quality"))
        elif event["kind"] == "identified":
            service.on_identified(str(event["employee_id"]), ts, float(event["score"]), image, vector, event.get("sha256"))
        elif event["kind"] == "unknown":
            service.on_unknown(ts, image, vector, event.get("guess_id"), event.get("guess_score"), event.get("sha256"),
                               event.get("quality"), track=_pair(event.get("track")), follows=_pair(event.get("follows")),
                               frames=event.get("frames"), votes=event.get("votes"))
        elif event["kind"] == "reference":
            employee = str(event["employee_id"])
            if store.employee(employee) is None:
                raise ValueError("no such person")
            store.add_embedding(str(event["model"]), employee, _vector(event["vector"]), str(event["source"]), ts, image,
                                event.get("quality_ok"), event.get("quality"))
            core.library.reload()
        elif event["kind"] == "alert":
            service.alert(str(event["text"])[:1000])
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
                                             "backlog": int(beat.get("backlog") or 0),
                                             "greeting": _greeting_status(beat.get("greeting"))}))
        return jsonify(ok=True, mode="shadow" if shadow else "live")

    def mac_config() -> dict:
        current = settings.current(core.cfg)
        config = {section: dict(current[section]) for section in MAC_SECTIONS}
        config["camera"].pop("area", None)
        return config

    @app.get("/api/edge/sync")
    @from_the_mac
    def edge_sync():
        if shadow:
            return jsonify(mode="shadow")
        config = mac_config()
        config_version = hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()
        gallery = store.edge_gallery_version()
        answer = {"mode": "live", "gallery": gallery, "config_version": config_version, "jobs": store.edge_jobs()}
        if request.args.get("gallery") != gallery:
            answer.update(employees=store.edge_employee_rows(), embedding_keys=store.edge_embedding_keys())
        if request.args.get("config") != config_version:
            answer["config"] = config
        if core.cfg.greeting.enabled:
            from ..core.greeting import rules_for

            answer["greeting"] = rules_for(service, service.now().date())
        today = service.now().date()  # also the arrivals an admin recorded here: the Mac only knows its own
        answer["arrived"] = {"day": today.isoformat(), "people": sorted(store.checkins(today))}
        return jsonify(answer)

    @app.post("/api/edge/sync/embeddings")
    @from_the_mac
    def edge_sync_embeddings():
        ids = (request.get_json(silent=True) or {}).get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_JOB_EMBEDDINGS or not all(isinstance(i, int) for i in ids):
            abort(400, "expected a list of ids")
        return jsonify(store.edge_embeddings_by_id(ids))

    @app.get("/api/edge/files/<path:rel>")
    @from_the_mac
    def edge_file(rel: str):
        if not UPLOAD_PATH.fullmatch(rel):
            abort(400, "bad path")
        return send_from_directory(core.data_dir, rel, mimetype="application/octet-stream")

    @app.post("/api/edge/jobs/<int:job_id>/result")
    @from_the_mac
    def edge_job_result(job_id: int):
        result = request.get_json(silent=True) or {}
        payload = store.edge_job_take(job_id)
        if payload is None:
            return jsonify(ok=True)  # the answer to a job already closed
        if payload.get("kind") == "say":
            store.log(service.now(), "office computer", "test greeting",
                      ("said: " if result.get("ok") else "failed: ") + str(result.get("message", ""))[:400])
            return jsonify(ok=True)
        if UPLOAD_PATH.fullmatch(payload.get("image", "")):
            (core.data_dir / payload["image"]).unlink(missing_ok=True)
        if payload.get("voice"):
            if result.get("ok"):
                settings.save(core.cfg, store, {"greeting": {"engine": "zerotts", "zerotts_voice": payload["voice"],
                                                            "zerotts_voices_dir": "data/voices"}})
            store.log(service.now(), "office computer", "installed voice",
                      f"{payload['voice']}: " + ("installed" if result.get("ok") else
                                                f"failed: {str(result.get('message', ''))[:300]}"))
            return jsonify(ok=True)
        person = store.employee(payload.get("employee_id", ""))
        who = person.email if person else payload.get("employee_id", "")
        name = payload.get("filename", "photo")
        text = f"{who}: {name} added" if result.get("ok") else f"{who}: {name} not added: {result.get('message', '')}"
        for warning in result.get("warnings", []):
            text += f" ({warning})"
        store.log(service.now(), "office computer", "uploaded photo", text[:500])
        return jsonify(ok=True)

    @app.post("/api/edge/state")
    @from_the_mac
    def edge_state():
        if not shadow:  # once the server is live it is the one that decides
            abort(404)
        state = request.get_json(silent=True)
        if not isinstance(state, dict) or not isinstance(state.get("days"), list):
            abort(400, "expected a state update")
        days, tables = state["days"], state.get("tables") or {}
        embeddings = state.get("embeddings") or []
        with ingest:
            try:
                for day in days:
                    ours = store.edge_camera_times(day)
                    theirs = {r["ts"] for r in tables.get("sightings", []) if r["day"] == day and r["source"] == "camera"}
                    _note_differences(store, day, ours, theirs)
                store.edge_replace_state(days, tables, state.get("employees"), state.get("embedding_ids"), embeddings)
                if state.get("camera"):  # the address the server hands back to the Mac once it is in charge
                    settings.save(core.cfg, store, {"camera": {k: state["camera"][k] for k in ("url", "more")}})
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                log.warning("Refused a state update: %s", exc)
                abort(400, "bad state update")
        if state.get("employees") is not None or state.get("embedding_ids") is not None or embeddings:
            core.library.reload()
        wanted = [r.get("image") for table in ("unknowns", "sightings", "checkins") for r in tables.get(table, [])]
        wanted += [r.get("image") for r in embeddings]
        missing = sorted({rel for rel in wanted if rel and PHOTO_PATH.fullmatch(rel) and not (core.data_dir / rel).exists()})
        return jsonify(missing=missing)

    @app.get("/healthz")
    def healthz():
        return Response("ok\n", mimetype="text/plain")
