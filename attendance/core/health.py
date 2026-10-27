"""How the system is doing, for the Status page and the diagnostics bundle: camera uptime, speed,
backup, updates, disk space and photos, plus the setup gaps shown as a banner on every page."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import time
from typing import TYPE_CHECKING

from . import machine

if TYPE_CHECKING:
    from .config import Config
    from .pipeline import App

UPTIME_DAYS = 7
FEW_PHOTOS = 5  # fewer reference photos than this recognise poorly (as on the People page)
LOW_DISK_GB = 5

_STARTED = time.time()
_cpu_sample: tuple[float, float] | None = None  # (wall clock, CPU seconds) at the previous look


def config_warnings(cfg: "Config") -> list[dict]:
    """Setup gaps that make the system look fine while it is not: (id, text, link to the setting)."""
    found = []
    if not cfg.google_chat.webhook_url.strip():
        found.append({"id": "chat", "anchor": "notify",
                      "text": "Google Chat is not set up: nobody is told who is late or when the camera stops."})
    if not cfg.backup.folder.strip():
        found.append({"id": "backup", "anchor": "admin",
                      "text": "No backup folder: a broken disk would lose the database and all photos."})
    known = machine.addresses_if_ready()
    url = machine.public_url_problem(cfg, known) if known else None
    if url:
        found.append({"id": "public_url", "anchor": "admin", "text": url["text"]})
    return found


def camera_uptime(app: "App", days: int = UPTIME_DAYS) -> list[dict]:
    """Per work day, newest first: how much of the camera hours the camera was working."""
    service, store = app.service, app.store
    now = service.now()
    rows = []
    for back in range(days):
        day = now.date() - dt.timedelta(days=back)
        if not service.is_workday(day):
            continue
        w = service.window(day)
        if now <= w.start:
            continue
        end = min(now, w.end)
        total = (end - w.start).total_seconds()
        down = _covered([(max(a, w.start), min(b, end)) for a, b in service._outages(day, now)])
        if not store.notice_sent(day, "camera_ok") and not down:
            rows.append({"day": day, "share": None, "down_min": 0, "note": "no data (program not running)"})
            continue
        share = max(0.0, 1 - down / total) if total else 1.0
        rows.append({"day": day, "share": share, "down_min": round(down / 60),
                     "note": "so far" if end < w.end else ""})
    return rows


def _covered(spans) -> float:
    """Seconds covered by possibly overlapping (start, end) spans."""
    total, reach = 0.0, None
    for a, b in sorted(spans):
        if reach is not None and a < reach:
            a = reach
        if b > a:
            total += (b - a).total_seconds()
            reach = b if reach is None else max(reach, b)
    return total


def process_usage() -> dict:
    """CPU use of this program since the previous look (or since it started) and its peak memory."""
    global _cpu_sample
    times = os.times()
    cpu, wall = times.user + times.system, time.time()
    if _cpu_sample is None:
        since = max(1.0, wall - _STARTED)
        share = cpu / since
    else:
        share = (cpu - _cpu_sample[1]) / max(1e-6, wall - _cpu_sample[0])
    _cpu_sample = (wall, cpu)
    usage = {"cpu_percent": round(share * 100), "cores": os.cpu_count() or 1, "memory_mb": None}
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        usage["memory_mb"] = round(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024)  # bytes vs KiB
    except (ImportError, OSError):
        pass
    return usage


def photo_summary(app: "App") -> dict:
    """People and how many have too few reference photos (or, when photo quality is known, too few good ones)."""
    people = app.store.all_employees()
    active = [p for p in people if getattr(p, "active", True)]
    counts = app.gallery.counts()
    good = getattr(app.library, "quality", None)  # employee_id -> (photos, good, not checked yet)
    if callable(good):
        try:
            counts = {k: q[1] + q[2] for k, q in good().items()}  # not checked yet: not counted against them
        except Exception:
            pass
    few = sorted((p for p in active if counts.get(p.id, 0) < FEW_PHOTOS), key=lambda p: counts.get(p.id, 0))
    return {"people": len(active), "few": [{"id": p.id, "name": p.name, "photos": counts.get(p.id, 0)} for p in few],
            "quality_known": callable(good)}


def disk(app: "App") -> dict:
    usage = shutil.disk_usage(app.data_dir)
    free_gb = usage.free / (1 << 30)
    return {"free_gb": round(free_gb, 1), "total_gb": round(usage.total / (1 << 30)), "low": free_gb < LOW_DISK_GB}


def camera_detail(cfg: "Config", live) -> str:
    """The frame rates for the Status page: one line, every camera when there are several."""
    if len(live.cameras) > 1:
        return " · ".join(f"{c['label']}: {c['fps']} of {cfg.camera.process_fps} frames/s"
                          + (f", sends {c['source_fps']}/s" if c["source_fps"] else "") for c in live.cameras)
    source = getattr(live, "source_fps", 0.0)
    return (f"{round(live.fps, 1)} of {cfg.camera.process_fps} frames/s analysed"
            + (f" · camera sends {round(source, 1)}/s" if source else ""))


def status(app: "App") -> dict:
    """Everything on the Status page."""
    from .. import __version__
    from ..cli import updater

    cfg, live = app.cfg, app.live
    backup = app.backup.status()
    return {
        "version": __version__, "packaged": getattr(sys, "frozen", False),
        "camera": {"state": live.camera_state, "ok": live.camera_state == "ok", "problem": live.problem,
                   "fps": round(live.fps, 1), "target_fps": cfg.camera.process_fps,
                   "source_fps": round(getattr(live, "source_fps", 0.0), 1), "decode_threads": cfg.camera.decode_threads,
                   "in_window": app.service.in_window(app.service.now()), "configured": bool(cfg.camera.urls()),
                   "cameras": list(live.cameras), "detail": camera_detail(cfg, live)},
        "uptime": camera_uptime(app),
        "usage": process_usage(),
        "backup": {"folder": cfg.backup.folder, "at": backup.get("at"), "ok": backup.get("ok"),
                   "message": backup.get("message", ""), "warning": app.backup.warning(app.service.now())},
        "update": {"enabled": cfg.updates.enabled, **updater.read_status(app.data_dir)},
        "disk": disk(app),
        "photos": photo_summary(app),
        "warnings": config_warnings(cfg),
        "power": app.machine.status() if getattr(app, "machine", None) else None,
        "public_url": machine.public_url_problem(cfg, machine.addresses_if_ready()) if machine.addresses_if_ready() else None,
        "placement": (app.placement.recent(app.service.now().date(), UPTIME_DAYS, cfg.recognition.min_face_px)
                      if getattr(app, "placement", None) else []),
        "min_face_px": cfg.recognition.min_face_px,
    }
