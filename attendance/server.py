"""Run the admin site on a server, with the camera and face recognition left on the office Mac.

    CHECKIN_HOME              folder for config.yaml, the database and photos (keep it out of the web root)
    CHECKIN_ADMIN_PASSWORD    the admin login; the site is on the internet, so it will not start without one
    CHECKIN_EDGE_TOKEN_SHA256 SHA-256 (hex) of the token the Mac sends
    CHECKIN_ADMIN_USER        admin user name (optional)
    CHECKIN_PUBLIC_URL        https address of the site (optional)
    CHECKIN_SHADOW            1 (default): receive and show only, post nothing to Chat, allow no edits; 0: live
    CHECKIN_OFFICE_URL        address of the admin page on the office Mac, shown where only that computer can help (optional)

Serve `attendance.server:application` with exactly one worker process and several threads.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import abort, render_template_string, request

from .core import config as config_module
from .core.config import Config
from .core.pipeline import App, _leave_sync_loop
from .web import PUBLIC, create_app
from .web import edge_api, edge_import

log = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 180.0
OFFICE_URL = "http://192.168.10.243:8000"
MIN_PASSWORD = 12
TICK_S = 5.0
OFFICE_ONLY = """{% extends "base.html" %}{% block body %}<div class="card"><h2>Camera</h2>
<p>The live camera is only available on the office computer. Open <a href="{{ office.url }}/live">{{ office.url }}/live</a>
from a computer with an address in {{ office.network }}, the same network as the camera.</p></div>{% endblock %}"""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def camera_status(app: App) -> tuple[bool, str]:
    """(camera working, what is wrong), as last reported by the Mac."""
    beat = edge_api.heartbeat(app.store)
    if not beat:
        return False, "the office Mac has not connected yet"
    age = (app.service.now() - dt.datetime.fromisoformat(beat["at"])).total_seconds()
    if age > HEARTBEAT_STALE_S:
        return False, f"no contact from the office Mac for {int(age // 60)} min"
    return beat["camera_ok"], beat["camera_state"]


def _tick_loop(app: App) -> None:
    while True:
        try:
            app.service.tick(*camera_status(app))
        except Exception:
            log.exception("Scheduled work failed")
        time.sleep(TICK_S)


def _only_process(home: Path):
    """The lock is held for the life of the process, so a second worker cannot post every message twice."""
    handle = (home / "scheduler.lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _mute(app: App) -> None:
    def muted(text, *args, **kwargs):
        log.info("Shadow mode, not posted: %s", str(text).replace("\n", " | "))

    app.chat.send = muted
    app.chat.kick = lambda now: None  # nor does anything already queued go out
    app.camera_sync.sync = lambda: None  # late arrivals reported there become fines


def _camera_is_at_the_office(application, office_url: str) -> None:
    """The camera is on the office network, out of this server's reach: its pages point to the Mac instead."""
    office_url = office_url.rstrip("/")
    host = urlsplit(office_url).hostname or ""
    application.jinja_env.globals["office"] = {"url": office_url, "network": host.rpartition(".")[0] + ".x"}

    def live_page():
        return render_template_string(OFFICE_ONLY)

    application.view_functions["live_page"] = live_page


def build(home: Path, admin_password: str, token_sha256: str, *, admin_user: str = "", public_url: str = "",
          shadow: bool = True, now=None, office_url: str = OFFICE_URL):
    if len(admin_password) < MIN_PASSWORD:
        raise RuntimeError(f"CHECKIN_ADMIN_PASSWORD must have at least {MIN_PASSWORD} characters")
    if not re.fullmatch(r"[0-9a-f]{64}", token_sha256):
        raise RuntimeError("CHECKIN_EDGE_TOKEN_SHA256 must be a SHA-256 in hex")
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    if path.exists():
        cfg = config_module.load(path)
    else:
        cfg = Config()
        cfg.base_dir = home
    if shadow:
        edge_import.adopt(cfg.path(cfg.data_dir))
    core = App(cfg, now=now)
    # After App(): saved Settings must not replace the login the server was started with.
    cfg.web.password = admin_password
    cfg.web.username = admin_user or cfg.web.username
    cfg.web.public_url = public_url or cfg.web.public_url
    if shadow:
        _mute(core)
    application = create_app(core)
    from .core.remote_voice_creation import RemoteVoiceCreator

    core.voice_creator = RemoteVoiceCreator(cfg, core.store, application.extensions["voice_previews"],
                                           application.extensions["voice_preview_lock"])
    _camera_is_at_the_office(application, office_url)
    core.photos_read_elsewhere = True
    application.jinja_env.globals["shadow_site"] = shadow
    PUBLIC.update(edge_api.ENDPOINTS)
    edge_api.register(application, core, token_sha256, shadow=shadow)
    if shadow:
        edge_import.register(application, core)

    writable = edge_api.ENDPOINTS | edge_import.ENDPOINTS

    @application.before_request
    def read_only():
        if shadow and request.method not in ("GET", "HEAD") and request.endpoint not in writable:
            abort(403, "Shadow mode: this site only mirrors the office Mac for now.")

    return application, core


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    home = Path(_required("CHECKIN_HOME"))
    shadow = os.environ.get("CHECKIN_SHADOW", "1").strip() != "0"
    application, core = build(home, _required("CHECKIN_ADMIN_PASSWORD"), _required("CHECKIN_EDGE_TOKEN_SHA256"),
                              admin_user=os.environ.get("CHECKIN_ADMIN_USER", "").strip(),
                              public_url=os.environ.get("CHECKIN_PUBLIC_URL", "").strip(), shadow=shadow,
                              office_url=os.environ.get("CHECKIN_OFFICE_URL", "").strip() or OFFICE_URL)
    lock = _only_process(home)
    if lock is None:
        log.error("Another worker already runs the scheduled work; run exactly one worker")
    else:
        application.config["SCHEDULER_LOCK"] = lock  # kept referenced so the lock stays held
        if not shadow:
            core.chat.start_background()
        threading.Thread(target=_tick_loop, args=(core,), name="tick", daemon=True).start()
        threading.Thread(target=_leave_sync_loop, args=(core,), name="leave-sync", daemon=True).start()
    log.info("Admin site ready (%s)", "shadow" if shadow else "live")
    return application


if __name__ != "__main__" and os.environ.get("CHECKIN_HOME"):
    application = main()
