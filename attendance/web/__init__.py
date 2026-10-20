"""Admin web page: attendance and corrections, late/absence report, employees and photos, live camera, settings."""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import re
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import setup as first_setup
from .icons import icon

if TYPE_CHECKING:
    from flask import Flask

    from ..core.pipeline import App

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_UPLOAD_MB = 50
# Pages anyone may open without logging in: a person's own page (its link carries a signature) and
# the stylesheet/script it needs. Everything else needs the admin login when a password is set.
PUBLIC = {"personal_page", "personal_note", "static"}
# What the read-only login may open; it may never send a form.
VIEWER_PAGES = {"index", "evidence_page", "evidence_zip", "unknowns", "report", "accuracy", "audit",
                "report_summary_csv", "report_details_csv", "media", "models_json"}


def _date(value: str | None, default: dt.date) -> dt.date:
    if not value:
        return default
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        from flask import abort

        abort(400, "dates must be YYYY-MM-DD")


def _time(value: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        raise ValueError("enter the time as HH:MM") from None


def current_user(cfg) -> str:
    from flask import request

    auth = request.authorization
    return auth.username if cfg.web.password and auth and auth.username else "admin"


def role(cfg) -> str | None:
    """'admin', 'viewer' (read-only login) or None (not logged in). Without a password everyone is admin."""
    from flask import request

    if not cfg.web.password:
        return "admin"
    auth = request.authorization
    if not auth:
        return None
    user, password, w = auth.username or "", auth.password or "", cfg.web
    if hmac.compare_digest(user, w.username) and hmac.compare_digest(password, w.password):
        return "admin"
    if (w.viewer_password and hmac.compare_digest(user, w.viewer_username)
            and hmac.compare_digest(password, w.viewer_password)):
        return "viewer"
    return None


def leave_status(core: "App") -> dict:
    from ..integrations import leave_api

    cfg, store, service = core.cfg, core.store, core.service
    status = leave_api.read_status(store)
    for key in ("at", "last_success"):
        if status.get(key):
            status[key] = dt.datetime.fromisoformat(status[key]).astimezone(service.tz)
    return {"enabled": core.leave_sync.enabled, "warning": leave_api.warning(cfg, store, service.now()),
            **status}


def late_message_status(core: "App", day: dt.date) -> dict:
    cfg, store, service = core.cfg, core.store, core.service
    lm = cfg.late_message
    if not lm.enabled:
        return {"enabled": False}
    sent = store.notice_time(day, "late_message")
    skipped = store.notice_time(day, "late_message_skipped")
    no_id = [a.name for a in service.late_people(day)
             if not getattr(store.employee(a.employee_id), "chat_user", "")]
    return {"enabled": True, "preview": service.late_text(day, preview=True),
            "sent": sent.astimezone(service.tz) if sent else None,
            "skipped": skipped.astimezone(service.tz) if skipped else None,
            "missing_chat": no_id, "send_at": lm.send_at or cfg.schedule.window_end, "auto": lm.auto_send}


def deadline_status(core: "App", day: dt.date) -> dict:
    """What happened at the late time: the not-seen list was posted, or skipped because everyone was in."""
    store, service = core.store, core.service
    sent, skipped = store.notice_time(day, "deadline"), store.notice_time(day, "deadline_skipped")
    return {"sent": sent.astimezone(service.tz) if sent else None,
            "skipped": skipped.astimezone(service.tz) if skipped else None}


def _back(endpoint: str, **kwargs):
    """Where to go after a form: the page it came from when it says so (setup forms), else `endpoint`."""
    from flask import redirect, request, url_for

    back = request.form.get("back", "")
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(url_for(endpoint, **kwargs))


def done(message: str, endpoint: str, **kwargs):
    from flask import flash

    flash(message, "ok")
    return _back(endpoint, **kwargs)


def failed(message: str, endpoint: str, **kwargs):
    from flask import flash

    flash(message, "error")
    return _back(endpoint, **kwargs)


# The Camera page asks for a new picture several times a second and pages load photos from /media:
# logging every successful one buried everything else (95 % of the log). Failures are still logged.
_ROUTINE_REQUEST = re.compile(r'"(?:\x1b\[[0-9;]*m)*(?:GET|HEAD) /(?:live\.json|live\.jpg|media/|static/)[^"]*" (?:2\d\d|304) ')


class QuietRoutineRequests(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _ROUTINE_REQUEST.search(record.getMessage())


def create_app(core: "App") -> "Flask":
    from flask import Flask, Response, abort, request, send_from_directory

    from ..core import health
    from ..core.service import SOURCES
    from ..store import LEAVE_KINDS

    cfg, store = core.cfg, core.store
    app = Flask(__name__)
    werkzeug_log = logging.getLogger("werkzeug")
    if not any(isinstance(f, QuietRoutineRequests) for f in werkzeug_log.filters):
        werkzeug_log.addFilter(QuietRoutineRequests())
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    secret = store.get("web_secret")
    if not secret:
        secret = secrets.token_hex(32)
        store.put("web_secret", secret)
    app.secret_key = secret
    app.jinja_env.globals["icon"] = icon
    # Browsers keep the stylesheet and script until they change.
    static_dir = Path(app.static_folder or "")
    app.jinja_env.globals["asset_version"] = str(int(max((f.stat().st_mtime for f in static_dir.glob("*")), default=0)))

    @app.before_request
    def guard():
        if request.endpoint not in PUBLIC:
            who = role(cfg)
            if who is None:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="attendance"'})
            if who == "viewer" and (request.method != "GET" or request.endpoint not in VIEWER_PAGES):
                abort(403, "This login can only look, not change anything.")
        if request.method == "POST":
            # Reject form posts coming from other websites (cross-site request forgery).
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin and urlparse(origin).netloc != request.host:
                abort(403)
        return None

    @app.context_processor
    def common():
        return {"pending": store.pending_group_count(), "nav": request.endpoint, "SOURCES": SOURCES,
                "setup_open": first_setup.open_count(core), "models_status": core.live.models,
                "translocated": sys.platform == "darwin" and "/AppTranslocation/" in sys.executable,
                "leave_label": lambda kind: LEAVE_KINDS.get(kind, kind), "WEEKDAYS": WEEKDAYS,
                "employee_emails": store.employee_emails(), "config_warnings": health.config_warnings(cfg),
                "call_name": core.service.chat_name,  # how people are called: their email name by default
                "role": "admin" if request.endpoint in PUBLIC else role(cfg)}

    from . import attendance as attendance_routes
    from . import capture as capture_routes
    from . import employees as employees_routes
    from . import leave as leave_routes
    from . import personal as personal_routes
    from . import reports as reports_routes
    from . import settings as settings_routes
    from . import status as status_routes

    attendance_routes.register(app, core)
    capture_routes.register(app, core)
    employees_routes.register(app, core)
    leave_routes.register(app, core)
    personal_routes.register(app, core)
    reports_routes.register(app, core)
    settings_routes.register(app, core)
    status_routes.register(app, core)

    @app.get("/media/<path:path>")
    def media(path: str):
        if not path.startswith(("crops/", "faces/", "review-seed/", "captures/")) or ".." in Path(path).parts:
            abort(404)
        return send_from_directory(core.data_dir, path)

    return app
