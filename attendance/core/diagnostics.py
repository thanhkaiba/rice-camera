"""A zip to send when something is wrong: recent logs, configuration and the system's state, with
passwords, tokens, webhook keys and camera credentials removed. From the Status page or
`python -m attendance diagnostics out.zip`."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import platform
import re
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from ..store import Store

LOG_TAIL_BYTES = 2_000_000
DAYS = 30
HIDDEN = "***"
SECRET_FIELDS = {"password", "token", "webhook_url", "healthcheck_url"}

_SECRETS = [
    (re.compile(r"(?i)\b(key|token|secret|password|access_token)=([^&\s\"'<>]+)"), rf"\1={HIDDEN}"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@\"']+):([^@\s\"']+)@"), rf"\1\2:{HIDDEN}@"),  # rtsp://user:pass@
    (re.compile(r"(?i)(hc-ping\.com/)[\w-]+"), rf"\1{HIDDEN}"),
    (re.compile(r"(?i)(bearer\s+)[\w.~+/=-]+"), rf"\1{HIDDEN}"),
    (re.compile(r"(?im)^(\s*(?:password|token|webhook_url|healthcheck_url)\s*:\s*).+$"), rf"\1{HIDDEN}"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def redact_settings(value, name: str = ""):
    """Saved settings with secret fields blanked; addresses keep their host so they still help."""
    if isinstance(value, dict):
        return {k: redact_settings(v, k) for k, v in value.items()}
    if name in SECRET_FIELDS and value:
        return HIDDEN if name in ("password", "token") else redact(re.sub(r"\?.*", f"?{HIDDEN}", str(value)))
    return redact(value) if isinstance(value, str) else value


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - limit))
        return f.read().decode("utf-8", errors="replace")


def system_info() -> dict:
    from .. import __version__

    return {"version": __version__, "packaged": bool(getattr(sys, "frozen", False)), "executable": sys.executable,
            "python": sys.version.split()[0], "os": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "cpu_count": os.cpu_count(), "hostname": platform.node()}


def history(store: "Store", today: dt.date, days: int = DAYS) -> dict:
    """Camera outages and notices (what was posted when) for the last `days` days."""
    out = {"outages": [], "notices": []}
    for back in range(days):
        day = today - dt.timedelta(days=back)
        out["outages"] += [{"day": day.isoformat(), "start": a.isoformat(), "end": b.isoformat()}
                           for a, b in store.outages(day)]
    since = (today - dt.timedelta(days=days)).isoformat()
    out["notices"] = [dict(r) for r in store._all("SELECT day, kind, sent_at FROM notices WHERE day >= ? "
                                                  "ORDER BY day, sent_at", (since,))]
    return out


def build(cfg: "Config", store: "Store", config_path: Path | None = None, status: dict | None = None) -> bytes:
    """The zip as bytes. `status` is the Status page's data when the program is running."""
    from ..cli import updater
    from . import settings

    data = cfg.path(cfg.data_dir)
    now = dt.datetime.now(cfg.tz)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def add(name: str, text: str) -> None:
            z.writestr(name, text)

        def add_json(name: str, value) -> None:
            add(name, json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")

        add_json("system.json", {**system_info(), "created": now.isoformat(), "data_dir": str(data)})
        config_file = config_path or cfg.base_dir / "config.yaml"
        if config_file.exists():
            add("config.yaml", redact(config_file.read_text(encoding="utf-8", errors="replace")))
        try:
            saved = json.loads(store.get(settings.KEY) or "{}")
        except ValueError:
            saved = {}
        add_json("settings-saved.json", redact_settings(saved))
        add_json("settings-effective.json", redact_settings(settings.current(cfg)))
        add_json("update.json", updater.read_status(data))
        try:
            add_json("backup.json", json.loads(store.get("backup_status") or "{}"))
        except ValueError:
            pass
        add_json("history.json", history(store, now.date()))
        if status is not None:
            add_json("status.json", redact_settings(status))
        for name in ("service.log", "attendance.log"):
            if (data / name).exists():
                add(f"logs/{name}", redact(_tail(data / name)))
    return buf.getvalue()


def file_name(now: dt.datetime) -> str:
    return f"attendance-diagnostics-{platform.node().split('.')[0] or 'pc'}-{now:%Y%m%d-%H%M}.zip"
