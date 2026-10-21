"""SQLite persistence. All state survives a restart, so a reboot mid-morning loses nothing.

Image paths stored here are relative to the data directory: "crops/..." for camera
snapshots (deleted after the retention period) and "faces/..." for reference photos.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .attendance import AttendanceStoreMixin
from .employees import EmployeeStoreMixin
from .schema import (
    CHAT_COLUMNS,
    LEAVE_KINDS,
    SCHEMA,
    Checkin,
    Employee,
    Leave,
    Reference,
    Sighting,
    Unknown,
    _blob,
    _vec,
    body_photo,
    scene_photo,
    migrate,
    normalize_chat_user,
    normalize_email,
)
from .system import SystemStoreMixin


class Store(EmployeeStoreMixin, AttendanceStoreMixin, SystemStoreMixin):
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._migrate()

    def close(self) -> None:
        self._db.close()

    def backup_to(self, path: Path) -> None:
        """A consistent copy of the database (SQLite online backup), safe while the program runs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        dest = sqlite3.connect(str(partial))
        try:
            with self._lock:
                self._db.backup(dest)
        finally:
            dest.close()
        partial.replace(path)

    def _migrate(self) -> None:
        """Add columns introduced after a database was created."""
        migrate(self._db)

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()


__all__ = [
    "CHAT_COLUMNS",
    "LEAVE_KINDS",
    "SCHEMA",
    "Checkin",
    "Employee",
    "Leave",
    "Reference",
    "Sighting",
    "Store",
    "Unknown",
    "_blob",
    "_vec",
    "body_photo",
    "scene_photo",
    "normalize_chat_user",
    "normalize_email",
]
