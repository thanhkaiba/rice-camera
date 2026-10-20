"""SQLite schema, migrations, data models, and vector serialization helpers."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY,
    email TEXT,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    chat_user TEXT NOT NULL DEFAULT '',  -- Google Chat user ID, used to @mention the person
    exempt INTEGER NOT NULL DEFAULT 0  -- arrivals are recorded but never counted as late or absent (e.g. the boss)
);
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    source TEXT NOT NULL,
    image TEXT,
    created_at TEXT NOT NULL,
    quality_ok INTEGER,  -- 1 sharp and frontal, 0 blurred or turned away, NULL not checked yet
    quality_score REAL  -- 0–1 from the recognizer; NULL not scored yet
);
CREATE TABLE IF NOT EXISTS checkins (
    day TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL,
    image TEXT,
    PRIMARY KEY (day, employee_id)
);
-- Every time the camera (or an admin) placed someone, kept even after their arrival is known.
-- The arrival in `checkins` is derived from these, so correcting one sighting (wrong person,
-- not them) recomputes the arrival from what is left, e.g. their real later sighting.
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,  -- camera, admin (face confirmed on the review page), manual (time typed in)
    score REAL,
    image TEXT,
    status TEXT NOT NULL DEFAULT 'ok',  -- ok or rejected
    camera_id TEXT,  -- who the camera said it was (kept when an admin moves the sighting)
    model TEXT,  -- the face embedding, so a sighting moved to the right person can be learned
    vector BLOB,
    sha256 TEXT  -- of the snapshot files at capture time, to show later that they are unchanged
);
CREATE INDEX IF NOT EXISTS sightings_day ON sightings(day, employee_id);
CREATE TABLE IF NOT EXISTS unknowns (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    ts TEXT NOT NULL,
    image TEXT,
    model TEXT,
    vector BLOB,
    guess_id TEXT,
    guess_score REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    employee_id TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    group_id INTEGER,  -- unknown faces that look like the same person share a group
    ignored_day TEXT,  -- set when the admin ignored the face; ignoring only lasts that day
    sha256 TEXT,
    source TEXT NOT NULL DEFAULT 'camera'
);
CREATE TABLE IF NOT EXISTS calendar (
    day TEXT PRIMARY KEY,
    kind TEXT NOT NULL,  -- 'holiday' (nobody works) or 'workday' (extra working day)
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'hr'  -- hr (replaced by each HR sync) or manual (entered on the admin page)
);
CREATE TABLE IF NOT EXISTS leaves (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'hr'  -- hr or manual, as for calendar
);
-- A person's own remark about a day ("business trip"), written on their personal page.
CREATE TABLE IF NOT EXISTS day_notes (
    day TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (day, employee_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS outages (
    day TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Labels for leave types sent by the HR system; other types are shown as sent.
LEAVE_KINDS = {"leave": "Leave", "business_trip": "Business trip", "remote": "Working remotely",
               "sick": "Sick", "other": "Other"}


@dataclass
class Employee:
    id: str
    name: str
    active: bool
    chat_user: str = ""
    exempt: bool = False  # not counted as late or absent
    email: str = ""


@dataclass
class Checkin:
    employee_id: str
    first_seen: dt.datetime
    source: str
    score: float | None
    image: str | None


@dataclass
class Sighting:
    id: int
    day: dt.date
    employee_id: str
    ts: dt.datetime
    source: str
    score: float | None
    image: str | None
    status: str
    camera_id: str | None = None
    model: str | None = None
    vector: np.ndarray | None = None
    sha256: str | None = None


@dataclass
class Unknown:
    id: int
    ts: dt.datetime
    image: str | None
    model: str | None
    vector: np.ndarray | None
    guess_id: str | None
    guess_score: float | None
    status: str
    employee_id: str | None
    group_id: int = 0
    sha256: str | None = None
    source: str = "camera"


@dataclass
class Reference:
    id: int
    employee_id: str
    image: str | None
    source: str
    vector: np.ndarray
    created_at: dt.datetime
    quality_ok: bool | None = None  # sharp and frontal enough; None = not checked yet
    quality_score: float | None = None

    @property
    def image_score(self) -> int | None:
        """Displayed points, also used by deletion thresholds so a displayed 20 is kept at 20."""
        return None if self.quality_score is None else round(self.quality_score * 100)


@dataclass
class Leave:
    id: int
    employee_id: str
    start: dt.date
    end: dt.date
    kind: str
    note: str
    source: str = "hr"  # hr or manual


# Accepted CSV column names for the Google Chat user ID.
CHAT_COLUMNS = ("chat_user", "chat_id", "google_chat_id", "chat")


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    local = value.partition("@")[0]
    if (len(value) > 254 or len(local) > 64 or local.startswith(".") or local.endswith(".")
            or ".." in local or not re.fullmatch(
                r"[a-z0-9._%+\-]+@(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,63}", value)):
        raise ValueError("a valid employee email is required")
    return value


def normalize_chat_user(value: str | None) -> str:
    """A Google Chat user ID is a number. Accept it pasted as "users/123…" or "<users/123…>" too;
    anything else would silently turn into a mention that does not work."""
    value = (value or "").strip().strip("<>").strip()
    if value.lower().startswith("users/"):
        value = value[len("users/"):]
    if value and not value.isdigit():
        raise ValueError(f"the Google Chat user ID is a number (e.g. 112233445566778899000), not {value!r}")
    return value


def body_photo(image: str) -> str:
    """Where the photo of the whole person is kept, next to the camera snapshot of their face."""
    return image[:-len(".jpg")] + "_body.jpg"


def scene_photo(image: str) -> str:
    """Where the whole camera picture (time stamp burned in, face boxed) is kept: the evidence."""
    return image[:-len(".jpg")] + "_scene.jpg"


def _vec(blob) -> np.ndarray | None:
    return None if blob is None else np.frombuffer(blob, dtype=np.float32)


def _blob(vector) -> bytes | None:
    return None if vector is None else np.asarray(vector, dtype=np.float32).tobytes()


def migrate(db: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    added = {"embeddings": [("image", "TEXT"), ("quality_ok", "INTEGER"), ("quality_score", "REAL")], "unknowns": [("group_id", "INTEGER"), ("ignored_day", "TEXT"), ("sha256", "TEXT"), ("source", "TEXT NOT NULL DEFAULT 'camera'")],
             "employees": [("chat_user", "TEXT NOT NULL DEFAULT ''"), ("exempt", "INTEGER NOT NULL DEFAULT 0"), ("email", "TEXT")],
             "sightings": [("camera_id", "TEXT"), ("model", "TEXT"), ("vector", "BLOB"), ("sha256", "TEXT")],
             "calendar": [("source", "TEXT NOT NULL DEFAULT 'hr'")], "leaves": [("source", "TEXT NOT NULL DEFAULT 'hr'")]}
    for table, columns in added.items():
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                if (table, name) == ("sightings", "camera_id"):
                    db.execute("UPDATE sightings SET camera_id = employee_id WHERE source = 'camera'")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS employees_email ON employees(email COLLATE NOCASE)")
    # Databases from before sightings existed: each arrival becomes its first sighting.
    if not db.execute("SELECT 1 FROM sightings LIMIT 1").fetchone():
        db.execute("INSERT INTO sightings (day, employee_id, ts, source, score, image, camera_id) "
                         "SELECT day, employee_id, first_seen, source, score, image, "
                         "CASE WHEN source = 'camera' THEN employee_id END FROM checkins")
    db.commit()
