"""Admin web page: attendance and corrections, late/absence report, employees and photos, live camera, settings."""
from __future__ import annotations

import datetime as dt
import hmac
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


def create_app(core: "App") -> "Flask":
    from flask import Flask, Response, abort, request, send_from_directory

    from ..core import health
    from ..core.service import SOURCES
    from ..store import LEAVE_KINDS

    cfg, store = core.cfg, core.store
    app = Flask(__name__)
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
        if cfg.web.password:
            auth = request.authorization
            if not (auth and hmac.compare_digest(auth.username or "", cfg.web.username)
                    and hmac.compare_digest(auth.password or "", cfg.web.password)):
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="attendance"'})
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
                "employee_emails": store.employee_emails(), "config_warnings": health.config_warnings(cfg)}

    from . import attendance as attendance_routes
    from . import employees as employees_routes
    from . import reports as reports_routes
    from . import settings as settings_routes
    from . import status as status_routes

    attendance_routes.register(app, core)
    employees_routes.register(app, core)
    reports_routes.register(app, core)
    settings_routes.register(app, core)
    status_routes.register(app, core)

    @app.get("/media/<path:path>")
    def media(path: str):
        if not path.startswith(("crops/", "faces/", "review-seed/")) or ".." in Path(path).parts:
            abort(404)
        return send_from_directory(core.data_dir, path)

    return app
