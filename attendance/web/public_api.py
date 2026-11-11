"""A read-only API for other systems (payroll, HR): a person's arrival and lateness by day.

Each system gets its own key, made and revoked on Settings → Access. Only a SHA-256 of a key is kept, so
a key is shown once, when it is made. The description for the other system's developers is at /api/docs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
from typing import TYPE_CHECKING

from flask import Flask, abort, jsonify, render_template_string, request

from . import PUBLIC, current_user, done, failed

if TYPE_CHECKING:
    from ..core.pipeline import App

KEYS = "api_keys"
PREFIX = "att_"
MAX_DAYS = 62
LAST_USED_EVERY = dt.timedelta(minutes=5)  # how often a key's "last used" is written, not on every call
SWAGGER = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"
ENDPOINTS = {"api_attendance", "api_openapi", "api_docs"}

CREATED = """{% extends "base.html" %}{% set title = 'API key' %}{% block body %}
<div class="card"><h2 class="row">{{ icon('key') }}API key for {{ name }}</h2>
<p>Copy it now and give it to that system: it is not shown again. Lost keys are revoked and replaced.</p>
<p><code style="font-size:1.05em; user-select:all; word-break:break-all">{{ key }}</code></p>
<p class="hint">The system sends it as <code>Authorization: Bearer &lt;key&gt;</code>. How to call the API:
<a href="{{ url_for('api_docs') }}" target="_blank" rel="noopener">{{ docs }}</a></p>
<a class="btn" href="{{ url_for('settings_page') }}#admin">{{ icon('chevron-left') }}Back to Settings</a></div>
{% endblock %}"""

DOCS = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance API</title><link rel="stylesheet" href="{{ swagger }}/swagger-ui.css"></head>
<body style="margin:0"><div id="ui"></div><script src="{{ swagger }}/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({ url: "{{ url_for('api_openapi') }}", dom_id: "#ui", deepLinking: true });</script></body></html>"""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def keys(store) -> list[dict]:
    return json.loads(store.get(KEYS) or "[]")


def create_key(store, name: str, now: dt.datetime) -> str:
    key = PREFIX + secrets.token_urlsafe(32)
    entry = {"id": secrets.token_hex(4), "name": name, "hash": _hash(key), "created_at": now.isoformat(timespec="seconds"),
             "last_used": None}
    store.put(KEYS, json.dumps([*keys(store), entry]))
    return key


def revoke_key(store, key_id: str) -> dict | None:
    found = [k for k in keys(store) if k["id"] == key_id]
    store.put(KEYS, json.dumps([k for k in keys(store) if k["id"] != key_id]))
    return found[0] if found else None


def check_key(store, presented: str, now: dt.datetime) -> dict | None:
    if not presented.startswith(PREFIX):
        return None
    digest, all_keys = _hash(presented), keys(store)
    for entry in all_keys:
        if hmac.compare_digest(entry["hash"], digest):
            last = entry.get("last_used")
            if not last or now - dt.datetime.fromisoformat(last) >= LAST_USED_EVERY:
                entry["last_used"] = now.isoformat(timespec="seconds")
                store.put(KEYS, json.dumps(all_keys))
            return entry
    return None


def _arrival(a, window) -> dict:
    from ..core.service import minutes_late

    return {"arrived_at": a.ts.isoformat(timespec="seconds"), "late_minutes": max(0, minutes_late(a, window))}


def day_status(service, employee_id: str, day: dt.date) -> dict:
    """What the attendance page says about one person on one day."""
    rep = service.report(day)
    w = rep.window
    entry = {"date": day.isoformat(), "workday": rep.workday, "status": "not_seen", "arrived_at": None, "late_minutes": 0,
             "late_from": w.late_from.strftime("%H:%M") if rep.workday else None, "registered": None,
             "request_pending": False, "leave": None, "final": day < service.now().date() or service.now() >= w.end,
             "camera_data": not rep.no_camera_data}
    for status, arrivals in (("late", rep.late), ("on_time", rep.on_time)):
        for a in arrivals:
            if a.employee_id == employee_id:
                entry.update(status=status, **_arrival(a, w), registered=a.registered or None, request_pending=a.pending)
                if status == "on_time":
                    entry["late_minutes"] = 0
                return entry
    for emp, _, kind in rep.on_leave:
        if emp == employee_id:
            entry.update(status="on_leave", leave=kind)
            if emp in rep.permitted_arrivals:
                entry["arrived_at"] = rep.permitted_arrivals[emp].ts.isoformat(timespec="seconds")
            return entry
    for emp, _, a in [*rep.exempt, *rep.support]:
        if emp == employee_id:
            entry.update(status="not_tracked", arrived_at=a.ts.isoformat(timespec="seconds") if a else None)
            return entry
    if not rep.workday:
        entry["status"] = "day_off"
        checkin = service.store.checkins(day).get(employee_id)
        if checkin is not None:
            entry["arrived_at"] = checkin.first_seen.astimezone(service.tz).isoformat(timespec="seconds")
    return entry


def _dates() -> tuple[dt.date, dt.date]:
    try:
        if request.args.get("date"):
            start = end = dt.date.fromisoformat(request.args["date"])
        else:
            start, end = dt.date.fromisoformat(request.args["from"]), dt.date.fromisoformat(request.args["to"])
    except (KeyError, ValueError):
        abort(400, "give date=YYYY-MM-DD, or from=YYYY-MM-DD and to=YYYY-MM-DD")
    if end < start or (end - start).days >= MAX_DAYS:
        abort(400, f"from must not be after to, and at most {MAX_DAYS} days at a time")
    return start, end


def openapi(public_url: str) -> dict:
    day = {"type": "object", "properties": {
        "date": {"type": "string", "format": "date"},
        "workday": {"type": "boolean"},
        "status": {"type": "string", "enum": ["on_time", "late", "not_seen", "on_leave", "day_off", "not_tracked"],
                   "description": "not_seen: expected and no arrival (absent, or not arrived yet when final is false). "
                                  "on_leave: approved leave, WFH or an approved coming-late request that covered the arrival. "
                                  "not_tracked: someone whose arrival is recorded but never counted (manager, support staff)."},
        "arrived_at": {"type": "string", "format": "date-time", "nullable": True, "description": "First sighting, office time zone"},
        "late_minutes": {"type": "integer", "description": "Minutes after the late time; 0 unless status is late"},
        "late_from": {"type": "string", "nullable": True, "example": "09:01", "description": "From this minute an arrival is late"},
        "registered": {"type": "string", "nullable": True, "example": "9:00-09:30",
                       "description": "A coming-late request on Base that the arrival still missed"},
        "request_pending": {"type": "boolean", "description": "That request is still waiting for approval"},
        "leave": {"type": "string", "nullable": True, "description": "Kind of leave when status is on_leave"},
        "final": {"type": "boolean", "description": "false while the day's camera hours are still running: the status can change"},
        "camera_data": {"type": "boolean", "description": "false when the camera never worked that day: not_seen then proves nothing"},
    }}
    error = {"type": "object", "properties": {"error": {"type": "string"}}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Attendance API", "version": "1",
                 "description": "Arrival and lateness of one person by day, from the office camera and the HR system. "
                                "Read-only. Ask the administrator for a key; every call needs it."},
        "servers": [{"url": public_url.rstrip("/")}] if public_url else [],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"},
                                           "header": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
                       "schemas": {"Day": day, "Error": error}},
        "security": [{"bearer": []}, {"header": []}],
        "paths": {"/api/v1/attendance": {"get": {
            "summary": "A person's attendance for a day or a range of days",
            "parameters": [
                {"name": "email", "in": "query", "required": True, "schema": {"type": "string", "format": "email"}},
                {"name": "date", "in": "query", "schema": {"type": "string", "format": "date"}, "description": "One day"},
                {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}, "description": "First day of a range"},
                {"name": "to", "in": "query", "schema": {"type": "string", "format": "date"},
                 "description": f"Last day of a range, at most {MAX_DAYS} days in all"}],
            "responses": {
                "200": {"description": "One entry per day", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"email": {"type": "string"}, "name": {"type": "string"},
                                                     "timezone": {"type": "string", "example": "Asia/Ho_Chi_Minh"},
                                                     "days": {"type": "array", "items": {"$ref": "#/components/schemas/Day"}}}}}}},
                "400": {"description": "Missing or wrong dates", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                "401": {"description": "No key, or a key that was revoked", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                "404": {"description": "No such person", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
            }}}},
    }


def register(app: Flask, core: "App") -> None:
    cfg, store, service = core.cfg, core.store, core.service
    PUBLIC.update(ENDPOINTS)  # they check their own key
    app.jinja_env.globals["api_keys"] = lambda: keys(store)

    def refuse(code: int, message: str):
        return jsonify(error=message), code

    @app.get("/api/v1/attendance")
    def api_attendance():
        header = request.headers.get("Authorization", "")
        presented = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.headers.get("X-API-Key", "")
        if not check_key(store, presented.strip(), service.now()):
            return refuse(401, "a valid API key is needed")
        person = store.employee_by_email(request.args.get("email", "").strip().lower())
        if person is None:
            return refuse(404, "no person with that email")
        try:
            start, end = _dates()
        except Exception as exc:  # abort(400) with its message
            return refuse(400, getattr(exc, "description", "bad dates"))
        days = [day_status(service, person.id, start + dt.timedelta(days=i)) for i in range((end - start).days + 1)]
        return jsonify(email=person.email, name=person.name, timezone=cfg.timezone, days=days)

    @app.get("/api/openapi.json")
    def api_openapi():
        return jsonify(openapi(cfg.web.public_url or request.host_url))

    @app.get("/api/docs")
    def api_docs():
        return render_template_string(DOCS, swagger=SWAGGER)

    @app.post("/settings/api-keys")
    def api_key_create():
        name = request.form.get("name", "").strip()[:60]
        if not name:
            return failed("Give the key a name: the system that will use it.", "settings_page")
        key = create_key(store, name, service.now())
        store.log(service.now(), current_user(cfg), "create API key", name)
        return render_template_string(CREATED, key=key, name=name, docs=(cfg.web.public_url or request.host_url).rstrip("/") + "/api/docs")

    @app.post("/settings/api-keys/<key_id>/revoke")
    def api_key_revoke(key_id: str):
        gone = revoke_key(store, key_id)
        if gone is None:
            abort(404)
        store.log(service.now(), current_user(cfg), "revoke API key", gone["name"])
        return done(f"The key for {gone['name']} no longer works.", "settings_page")
