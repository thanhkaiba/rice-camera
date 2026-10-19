"""Tells Google Chat when the attendance program itself keeps crashing, and when it runs again.

The program reports camera problems, but it cannot report its own death: the launcher, which
restarts it (launcher.supervise), is the part still running to say so. A start after the computer
lost power or the program was killed is noted too. A computer that is off says nothing at all;
the healthcheck URL in Settings covers that.
"""
from __future__ import annotations

import datetime as dt
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

CRASHES = 3          # this many stops ...
WITHIN_S = 600       # ... within this many seconds is "keeps stopping"
STABLE_S = 300       # running this long after an alert counts as recovered
ALERT_EVERY_S = 3600  # at most one crash alert an hour


class CrashWatch:
    """Fed by the restart loop: `started()` when the program starts, `exited(code)` when it stops,
    `tick()` about once a second while it runs."""

    def __init__(self, post: Callable[[str], None], clock: Callable[[], float] = time.time, host: str | None = None):
        self.post, self.clock = post, clock
        self.host = host or socket.gethostname().split(".")[0]
        self.exits: list[float] = []
        self.alerted_at: float | None = None
        self.down = False  # an alert is out; waiting for the program to run steadily again
        self.started_at = clock()

    def started(self) -> None:
        self.started_at = self.clock()

    def exited(self, code: int | None) -> None:
        now = self.clock()
        self.exits = [t for t in self.exits if now - t < WITHIN_S] + [now]
        if len(self.exits) >= CRASHES and (self.alerted_at is None or now - self.alerted_at >= ALERT_EVERY_S):
            self.alerted_at, self.down = now, True
            self.post(f"⚠️ The attendance program on {self.host} keeps stopping ({len(self.exits)} times in "
                      f"{WITHIN_S // 60} minutes, last exit code {code}). It is restarted each time, but arrivals "
                      "may be missed meanwhile. Check service.log in the data folder, or Status → Download diagnostics.")

    def tick(self) -> None:
        if self.down and self.clock() - self.started_at >= STABLE_S:
            self.down = False
            self.exits.clear()
            self.post(f"✅ The attendance program on {self.host} is running again.")


def unclean_stop(home: Path) -> bool:
    """True when the last run ended without shutting down (power cut, killed, crash of the launcher):
    its pid file is still there but nothing in it runs. Call before launcher.Instance clears it."""
    from .launcher import pid_alive

    try:
        pids = [int(p) for p in (home / "running.pid").read_text().split()]
    except (OSError, ValueError):
        return False
    if not pids or any(pid_alive(p) for p in pids):
        return False
    # Versions before this one left the pid file behind when restarting into an update.
    try:
        with open(home / "data" / "service.log", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            if b"Restarting into the update" in f.read():
                return False
    except OSError:
        pass
    return True


def chat_poster(config: Path) -> Callable[[str], None]:
    """Post to the Google Chat webhook saved in the settings, in the background. Undelivered messages
    go to the program's outbox, which delivers them once it runs and the network is back."""

    def deliver(text: str) -> None:
        from ..integrations.notifier import _http_post
        from ..store import Store
        from .updater import load_settings

        try:
            cfg = load_settings(config)
        except Exception:
            log.exception("Could not read the settings for a Google Chat alert")
            return
        url = cfg.google_chat.webhook_url.strip()
        if not url:
            return
        if cfg.web.public_url:
            text += f" · {cfg.web.public_url.rstrip('/')}/status"
        try:
            _http_post(url, {"text": text})
        except Exception as exc:
            log.warning("Google Chat alert not delivered, queued: %s", exc)
            store = Store(cfg.path(cfg.data_dir) / "attendance.db")
            try:
                store.enqueue({"text": text}, dt.datetime.now(dt.timezone.utc))
            finally:
                store.close()

    def post(text: str) -> None:
        threading.Thread(target=deliver, args=(text,), name="crash-alert", daemon=True).start()

    return post

