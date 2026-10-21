"""Daily backup of everything another computer would need, and the restore that uses it.

The backup goes to a folder of your choice: an external drive, a network share, or a folder that
Google Drive, OneDrive or Dropbox keeps in the cloud. Inside it:

    attendance-backup/
      config.yaml                  the settings file
      db/attendance-YYYY-MM-DD.db  a consistent copy of the database, one per day, the newest
                                   `keep_days` kept (records, sightings, settings, learned faces)
      data/faces/                  reference photos, mirrored (new and changed files copied,
      data/crops/                  deleted ones removed); crops/ only with include_photos
      data/review-seed/            offline review samples, always included
      latest.json                  when, what and how much

`python -m attendance restore <folder>` (with the program stopped) puts it back, on this or
another computer. The whole data folder is self-contained, so copying it by hand works too.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .config import Config, parse_clock

log = logging.getLogger(__name__)

SUBDIR = "attendance-backup"
STATUS_KEY = "backup_status"  # kv: the last result, shown on the Settings page
DAY_KEY = "backup_day"  # kv: the day of the last successful backup
RETRY = dt.timedelta(hours=1)  # after a failure, try again this much later
STALE = dt.timedelta(days=2)  # a successful backup older than this is worth a warning
MTIME_SLACK = 2.0  # seconds; FAT/exFAT drives keep file times in 2-second steps


@dataclass
class Result:
    ok: bool
    message: str
    at: str  # ISO time
    folder: str = ""
    copied: int = 0
    removed: int = 0
    bytes: int = 0
    seconds: float = 0.0


def backup_root(cfg: Config) -> Path:
    folder = Path(cfg.backup.folder).expanduser()
    return folder if folder.is_absolute() else cfg.path(str(folder))


def mirror(src: Path, dst: Path) -> tuple[int, int, int]:
    """Make `dst` hold the same files as `src`: copy new and changed files (size or time differ),
    remove files that are gone. Returns (copied, removed, bytes copied)."""
    copied = removed = size = 0
    if not src.exists():
        return 0, 0, 0
    dst.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        wanted.add(rel)
        target = dst / rel
        st = path.stat()
        try:
            ts = target.stat()
            same = ts.st_size == st.st_size and abs(ts.st_mtime - st.st_mtime) <= MTIME_SLACK
        except FileNotFoundError:
            same = False
        if not same:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
            size += st.st_size
    for path in sorted(dst.rglob("*"), reverse=True):  # children before their folders
        if path.is_file() and path.relative_to(dst) not in wanted:
            path.unlink()
            removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return copied, removed, size


def rotate(db_dir: Path, keep: int) -> int:
    """Delete the oldest database copies beyond `keep`."""
    copies = sorted(db_dir.glob("attendance-*.db"))
    for old in copies[:-keep] if keep > 0 else copies:
        old.unlink()
    return max(0, len(copies) - keep)


def run(store, cfg: Config, now: dt.datetime) -> Result:
    """One backup. Never raises: the result says what happened."""
    started = time.monotonic()
    at = now.isoformat(timespec="seconds")
    root = backup_root(cfg)
    try:
        if not cfg.backup.folder.strip():
            return Result(False, "No backup folder is set.", at)
        if not root.is_dir():
            return Result(False, f"The backup folder {root} does not exist (drive unplugged?).", at, str(root))
        data_dir = cfg.path(cfg.data_dir).resolve()
        dest = (root / SUBDIR).resolve()
        if dest == data_dir or data_dir in dest.parents:
            return Result(False, "The backup folder is inside the data folder; choose another place.", at, str(root))
        dest.mkdir(parents=True, exist_ok=True)

        db_dir = dest / "db"
        snapshot = db_dir / f"attendance-{now:%Y-%m-%d}.db"
        store.backup_to(snapshot)
        size = snapshot.stat().st_size
        rotate(db_dir, cfg.backup.keep_days)

        config_file = cfg.base_dir / "config.yaml"
        if config_file.exists():
            shutil.copy2(config_file, dest / "config.yaml")

        copied, removed, photo_bytes = mirror(data_dir / "faces", dest / "data" / "faces")
        c, r, b = mirror(data_dir / "review-seed", dest / "data" / "review-seed")
        copied, removed, photo_bytes = copied + c, removed + r, photo_bytes + b
        if cfg.backup.include_photos:
            c, r, b = mirror(data_dir / "crops", dest / "data" / "crops")
            copied, removed, photo_bytes = copied + c, removed + r, photo_bytes + b
        size += photo_bytes
        seconds = time.monotonic() - started
        result = Result(True, f"Database copy and {copied} new or changed photo file(s) written "
                              f"({size / 1e6:.1f} MB, {seconds:.0f} s); {removed} removed.",
                        at, str(dest), copied, removed, size, round(seconds, 1))
        (dest / "latest.json").write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        return result
    except Exception as exc:  # a full disk, a permission problem, a share that went away
        log.exception("Backup failed")
        return Result(False, f"{type(exc).__name__}: {exc}", at, str(root), seconds=time.monotonic() - started)


class Backup:
    """The daily schedule and the 'Back up now' button; the copy runs on its own thread so the
    camera is never held up."""

    def __init__(self, store, cfg: Config, now: Callable[[], dt.datetime], alert: Callable[[str], None]):
        self.store, self.cfg, self._now, self._alert = store, cfg, now, alert
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        try:
            return json.loads(self.store.get(STATUS_KEY) or "{}")
        except ValueError:
            return {}

    def due(self, now: dt.datetime) -> bool:
        if not self.cfg.backup.folder.strip() or self.running:
            return False
        if self.store.get(DAY_KEY) == now.date().isoformat():
            return False
        if now.time() < parse_clock(self.cfg.backup.time):
            return False
        st = self.status()
        if st.get("at") and not st.get("ok"):
            last = dt.datetime.fromisoformat(st["at"])
            if now - last < RETRY:
                return False
        return True

    def start(self, who: str = "schedule") -> bool:
        """Run a backup in the background. False if one is already running."""
        with self._lock:
            if self.running:
                return False
            self._thread = threading.Thread(target=self._run, args=(who,), name="backup", daemon=True)
            self._thread.start()
            return True

    def wait(self, timeout: float = 600) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def run_now(self, who: str = "command line") -> Result:
        return self._run(who)

    def _run(self, who: str) -> Result:
        now = self._now()
        log.info("Backup started (%s) to %s", who, self.cfg.backup.folder)
        result = run(self.store, self.cfg, now)
        self.store.put(STATUS_KEY, json.dumps(asdict(result)))
        if result.ok:
            self.store.put(DAY_KEY, now.date().isoformat())
            log.info("Backup done: %s", result.message)
        else:
            log.error("Backup failed: %s", result.message)
            if self.store.mark_notice(now.date(), "backup_failed", now):  # once a day in the chat
                self._alert(f"⚠️ Backup failed: {result.message} Check the backup folder in Settings.")
        return result

    def warning(self, now: dt.datetime) -> str | None:
        """For the Today page: a failed or stale backup."""
        if not self.cfg.backup.folder.strip():
            return None
        st = self.status()
        if not st.get("at"):
            return None
        if not st.get("ok"):
            return f"The last backup failed: {st.get('message', '')}"
        last = dt.datetime.fromisoformat(st["at"])
        if now - last > STALE:
            return f"The last backup was on {last:%d %b %H:%M}; none since."
        return None


def restore(cfg: Config, folder: Path, snapshot: str | None = None) -> str:
    """Put a backup back into this install's data folder. Run with the program stopped."""
    src = folder / SUBDIR if (folder / SUBDIR).is_dir() else folder
    db_dir = src / "db"
    copies = sorted(db_dir.glob("attendance-*.db")) if db_dir.is_dir() else []
    if not copies:
        raise SystemExit(f"No database copy found in {src}")
    chosen = next((c for c in copies if snapshot and c.stem.endswith(snapshot)), None) if snapshot else copies[-1]
    if chosen is None:
        raise SystemExit(f"No database copy for {snapshot}; available: "
                         + ", ".join(c.stem.split("-", 1)[1] for c in copies))
    if (cfg.base_dir / "running.pid").exists():
        raise SystemExit("Stop the program first (FaceAttendance stop), then restore.")
    # The copy must be a readable database before anything here is replaced.
    with sqlite3.connect(f"file:{chosen}?mode=ro", uri=True) as check:
        employees = check.execute("SELECT COUNT(*) FROM employees").fetchone()[0]

    data_dir = cfg.path(cfg.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("attendance.db", "attendance.db-wal", "attendance.db-shm"):
        (data_dir / stale).unlink(missing_ok=True)
    shutil.copy2(chosen, data_dir / "attendance.db")
    photos = 0
    for name in ("faces", "crops", "review-seed"):
        if (src / "data" / name).is_dir():
            photos += mirror(src / "data" / name, data_dir / name)[0]
    lines = [f"Database restored from {chosen.name} ({employees} employees), {photos} photo files copied."]
    config_file = cfg.base_dir / "config.yaml"
    if (src / "config.yaml").exists() and not config_file.exists():
        shutil.copy2(src / "config.yaml", config_file)
        lines.append("config.yaml restored too.")
    lines.append("Start the program and check the camera address and 'Address of this page' in Settings.")
    return "\n".join(lines)
