"""SQLite persistence. All state survives a restart, so a reboot mid-morning loses nothing.

Image paths stored here are relative to the data directory: "crops/..." for camera
snapshots (deleted after the retention period) and "faces/..." for reference photos.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY,
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
    created_at TEXT NOT NULL
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
    sha256 TEXT
);
CREATE TABLE IF NOT EXISTS calendar (
    day TEXT PRIMARY KEY,
    kind TEXT NOT NULL,  -- 'holiday' (nobody works) or 'workday' (extra working day)
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS leaves (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
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


@dataclass
class Reference:
    id: int
    employee_id: str
    image: str | None
    source: str
    vector: np.ndarray


@dataclass
class Leave:
    id: int
    employee_id: str
    start: dt.date
    end: dt.date
    kind: str
    note: str


# Accepted CSV column names for the Google Chat user ID.
CHAT_COLUMNS = ("chat_user", "chat_id", "google_chat_id", "chat")


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


class Store:
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
        added = {"embeddings": [("image", "TEXT")], "unknowns": [("group_id", "INTEGER"), ("ignored_day", "TEXT"), ("sha256", "TEXT")],
                 "employees": [("chat_user", "TEXT NOT NULL DEFAULT ''"), ("exempt", "INTEGER NOT NULL DEFAULT 0")],
                 "sightings": [("camera_id", "TEXT"), ("model", "TEXT"), ("vector", "BLOB"), ("sha256", "TEXT")]}
        for table, columns in added.items():
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                    if (table, name) == ("sightings", "camera_id"):
                        self._db.execute("UPDATE sightings SET camera_id = employee_id WHERE source = 'camera'")
        # Databases from before sightings existed: each arrival becomes its first sighting.
        if not self._db.execute("SELECT 1 FROM sightings LIMIT 1").fetchone():
            self._db.execute("INSERT INTO sightings (day, employee_id, ts, source, score, image, camera_id) "
                             "SELECT day, employee_id, first_seen, source, score, image, "
                             "CASE WHEN source = 'camera' THEN employee_id END FROM checkins")
        self._db.commit()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # employees -------------------------------------------------------------

    def import_employees(self, csv_text: str) -> int:
        """Add or rename employees from CSV text with columns id,name and optionally chat_user
        (Google Chat user ID, for @mentions). Nobody is removed, and someone switched off on the
        People page stays off (switch them on there)."""
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames or not {"id", "name"} <= {f.strip().lower() for f in reader.fieldnames}:
            raise ValueError("the CSV needs a header row with the columns id,name")
        rows = []
        for r in reader:
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if r.get("id") and r.get("name"):
                try:
                    chat_user = normalize_chat_user(next((r[c] for c in CHAT_COLUMNS if r.get(c)), ""))
                except ValueError as exc:
                    raise ValueError(f"row {r['id']}: {exc}") from None
                rows.append((r["id"], r["name"], chat_user))
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO employees (id, name, active, chat_user) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "chat_user = CASE WHEN excluded.chat_user != '' THEN excluded.chat_user ELSE employees.chat_user END",
                rows,
            )
        return len(rows)

    def add_employee(self, employee_id: str, name: str) -> None:
        employee_id, name = employee_id.strip(), name.strip()
        if not employee_id or not name:
            raise ValueError("employee id and name are required")
        if self.employee(employee_id):
            raise ValueError(f"employee id {employee_id} already exists")
        self._exec("INSERT INTO employees (id, name, active) VALUES (?, ?, 1)", (employee_id, name))

    def update_employee(self, employee_id: str, name: str, active: bool, chat_user: str | None = None,
                        exempt: bool | None = None) -> None:
        if not name.strip():
            raise ValueError("name is required")
        chat_user = normalize_chat_user(chat_user) if chat_user is not None else None  # validate before writing
        self._exec("UPDATE employees SET name = ?, active = ? WHERE id = ?", (name.strip(), int(active), employee_id))
        if chat_user is not None:
            self._exec("UPDATE employees SET chat_user = ? WHERE id = ?", (chat_user, employee_id))
        if exempt is not None:
            self._exec("UPDATE employees SET exempt = ? WHERE id = ?", (int(exempt), employee_id))

    def exempt_ids(self) -> set[str]:
        """Employees whose arrivals are not counted as late or absent."""
        return {r["id"] for r in self._all("SELECT id FROM employees WHERE exempt = 1")}

    def delete_employee(self, employee_id: str) -> list[str]:
        """Remove the employee and their face data; returns reference image paths to delete.
        Past attendance records are kept."""
        images = [r["image"] for r in self._all("SELECT DISTINCT image FROM embeddings WHERE employee_id = ? "
                                                "AND image IS NOT NULL", (employee_id,))]
        with self._lock, self._db:
            self._db.execute("DELETE FROM embeddings WHERE employee_id = ?", (employee_id,))
            self._db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        return images

    def employee(self, employee_id: str) -> Employee | None:
        rows = self._all("SELECT * FROM employees WHERE id = ?", (employee_id,))
        return self._employee(rows[0]) if rows else None

    @staticmethod
    def _employee(r) -> Employee:
        return Employee(r["id"], r["name"], bool(r["active"]), r["chat_user"] or "", bool(r["exempt"]))

    def all_employees(self) -> list[Employee]:
        return [self._employee(r) for r in self._all("SELECT * FROM employees ORDER BY active DESC, name")]

    def employees(self) -> dict[str, str]:
        """Active employees: id -> name."""
        return {r["id"]: r["name"] for r in self._all("SELECT id, name FROM employees WHERE active = 1 ORDER BY name")}

    def employee_count(self) -> int:
        return self._all("SELECT COUNT(*) AS n FROM employees")[0]["n"]

    # reference faces -------------------------------------------------------

    def add_embedding(self, model: str, employee_id: str, vector: np.ndarray, source: str, now: dt.datetime,
                      image: str | None = None) -> None:
        self._exec(
            "INSERT INTO embeddings (model, employee_id, vector, source, image, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (model, employee_id, _blob(vector), source, image, now.isoformat()),
        )

    def embeddings(self, model: str) -> list[tuple[str, np.ndarray]]:
        rows = self._all(
            "SELECT e.employee_id, e.vector FROM embeddings e JOIN employees p ON p.id = e.employee_id "
            "WHERE p.active = 1 AND e.model = ?",
            (model,),
        )
        return [(r["employee_id"], _vec(r["vector"])) for r in rows]

    def references(self, model: str, employee_id: str | None = None) -> list[Reference]:
        sql, params = "SELECT * FROM embeddings WHERE model = ?", [model]
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        return [Reference(r["id"], r["employee_id"], r["image"], r["source"], _vec(r["vector"]))
                for r in self._all(sql + " ORDER BY id", params)]

    def reference_images(self) -> list[tuple[str, str, str]]:
        """(employee_id, image, source) of every stored reference photo, from any model."""
        rows = self._all("SELECT employee_id, image, MIN(source) AS source FROM embeddings "
                         "WHERE image IS NOT NULL GROUP BY employee_id, image")
        return [(r["employee_id"], r["image"], r["source"]) for r in rows]

    def delete_reference(self, ref_id: int) -> None:
        self._exec("DELETE FROM embeddings WHERE id = ?", (ref_id,))

    def delete_reference_image(self, image: str) -> None:
        """Delete a reference photo's embeddings for all models."""
        self._exec("DELETE FROM embeddings WHERE image = ?", (image,))

    def delete_embeddings(self, model: str, employee_id: str, source_prefix: str) -> list[str]:
        images = [r["image"] for r in self._all(
            "SELECT image FROM embeddings WHERE model = ? AND employee_id = ? AND source LIKE ? AND image IS NOT NULL",
            (model, employee_id, source_prefix + "%"))]
        self._exec("DELETE FROM embeddings WHERE model = ? AND employee_id = ? AND source LIKE ?",
                   (model, employee_id, source_prefix + "%"))
        return images

    # sightings and arrivals -------------------------------------------------

    def _sighting(self, r) -> Sighting:
        return Sighting(r["id"], dt.date.fromisoformat(r["day"]), r["employee_id"], dt.datetime.fromisoformat(r["ts"]),
                        r["source"], r["score"], r["image"], r["status"], r["camera_id"], r["model"],
                        _vec(r["vector"]), r["sha256"])

    def _recompute(self, day: dt.date, employee_id: str) -> None:
        """The arrival is the earliest valid sighting, unless an admin typed a time in: the latest
        typed time wins over the camera. Call with the lock held, inside a transaction."""
        rows = self._db.execute(
            "SELECT * FROM sightings WHERE day = ? AND employee_id = ? AND status = 'ok' "
            "ORDER BY source = 'manual' DESC, CASE WHEN source = 'manual' THEN -id ELSE 0 END, ts LIMIT 1",
            (day.isoformat(), employee_id)).fetchall()
        if not rows:
            self._db.execute("DELETE FROM checkins WHERE day = ? AND employee_id = ?", (day.isoformat(), employee_id))
            return
        r = rows[0]
        self._db.execute(
            "INSERT INTO checkins (day, employee_id, first_seen, source, score, image) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, employee_id) DO UPDATE SET first_seen = excluded.first_seen, source = excluded.source, "
            "score = excluded.score, image = excluded.image",
            (day.isoformat(), employee_id, r["ts"], r["source"], r["score"], r["image"]))

    def _arrival_ts(self, day: dt.date, employee_id: str) -> str | None:
        row = self._db.execute("SELECT first_seen FROM checkins WHERE day = ? AND employee_id = ?",
                               (day.isoformat(), employee_id)).fetchone()
        return row["first_seen"] if row else None

    def record_checkin(self, day: dt.date, employee_id: str, ts: dt.datetime, source: str,
                       score: float | None, image: str | None, model: str | None = None,
                       vector: np.ndarray | None = None, sha256: str | None = None) -> str:
        """Add a sighting. Returns "new" (first arrival of the day), "earlier" (the arrival moved
        earlier) or "unchanged"."""
        with self._lock, self._db:
            before = self._arrival_ts(day, employee_id)
            self._db.execute("INSERT INTO sightings (day, employee_id, ts, source, score, image, camera_id, model, "
                             "vector, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (day.isoformat(), employee_id, ts.isoformat(), source, score, image,
                              employee_id if source == "camera" else None, model, _blob(vector), sha256))
            self._recompute(day, employee_id)
            after = self._arrival_ts(day, employee_id)
        if before is None:
            return "new"
        return "unchanged" if after == before else "earlier"

    def set_checkin(self, day: dt.date, employee_id: str, ts: dt.datetime, source: str,
                    score: float | None, image: str | None) -> None:
        """An arrival time typed in by an admin; it overrides the camera's sightings."""
        with self._lock, self._db:
            self._db.execute("INSERT INTO sightings (day, employee_id, ts, source, score, image) VALUES (?, ?, ?, ?, ?, ?)",
                             (day.isoformat(), employee_id, ts.isoformat(), "manual", score, image))
            self._recompute(day, employee_id)

    def delete_checkin(self, day: dt.date, employee_id: str) -> Checkin | None:
        """The arrival was wrong: reject every sighting of the day so far. If the person really
        comes later, the camera records them again."""
        c = self.checkins(day).get(employee_id)
        if c is not None:
            with self._lock, self._db:
                self._db.execute("UPDATE sightings SET status = 'rejected' WHERE day = ? AND employee_id = ?",
                                 (day.isoformat(), employee_id))
                self._recompute(day, employee_id)
        return c

    def arrival_sighting(self, day: dt.date, employee_id: str) -> Sighting | None:
        """The sighting the arrival comes from."""
        c = self.checkins(day).get(employee_id)
        if c is None:
            return None
        for s in self.sightings(day, employee_id):
            if s.status == "ok" and s.ts == c.first_seen and s.source == c.source:
                return s
        return None

    def sightings(self, day: dt.date, employee_id: str | None = None) -> list[Sighting]:
        if employee_id is None:
            rows = self._all("SELECT * FROM sightings WHERE day = ? ORDER BY ts", (day.isoformat(),))
        else:
            rows = self._all("SELECT * FROM sightings WHERE day = ? AND employee_id = ? ORDER BY ts",
                             (day.isoformat(), employee_id))
        return [self._sighting(r) for r in rows]

    def sightings_between(self, start: dt.date, end: dt.date) -> list[Sighting]:
        rows = self._all("SELECT * FROM sightings WHERE day BETWEEN ? AND ? ORDER BY ts", (start.isoformat(), end.isoformat()))
        return [self._sighting(r) for r in rows]

    def unknown_counts(self, start: dt.date, end: dt.date) -> dict[dt.date, int]:
        """Unidentified faces per day, whatever became of them."""
        rows = self._all("SELECT day, COUNT(*) AS n FROM unknowns WHERE day BETWEEN ? AND ? GROUP BY day",
                         (start.isoformat(), end.isoformat()))
        return {dt.date.fromisoformat(r["day"]): r["n"] for r in rows}

    def sighting(self, sighting_id: int) -> Sighting | None:
        rows = self._all("SELECT * FROM sightings WHERE id = ?", (sighting_id,))
        return self._sighting(rows[0]) if rows else None

    def update_sighting(self, sighting_id: int, employee_id: str | None = None, status: str | None = None) -> None:
        """Move a sighting to another person and/or reject or restore it; both people's arrivals
        are recomputed."""
        with self._lock, self._db:
            r = self._db.execute("SELECT * FROM sightings WHERE id = ?", (sighting_id,)).fetchone()
            if r is None:
                raise ValueError("this sighting no longer exists")
            old = self._sighting(r)
            moved = bool(employee_id) and employee_id != old.employee_id
            self._db.execute("UPDATE sightings SET employee_id = ?, status = ?, source = ? WHERE id = ?",
                             (employee_id or old.employee_id, status or old.status,
                              "corrected" if moved and old.source == "camera" else old.source, sighting_id))
            self._recompute(old.day, old.employee_id)
            if moved:
                self._recompute(old.day, employee_id)

    def checkins(self, day: dt.date) -> dict[str, Checkin]:
        rows = self._all("SELECT * FROM checkins WHERE day = ?", (day.isoformat(),))
        return {
            r["employee_id"]: Checkin(r["employee_id"], dt.datetime.fromisoformat(r["first_seen"]),
                                      r["source"], r["score"], r["image"])
            for r in rows
        }

    def checkins_between(self, start: dt.date, end: dt.date) -> list[tuple[dt.date, Checkin]]:
        rows = self._all("SELECT * FROM checkins WHERE day BETWEEN ? AND ? ORDER BY day",
                         (start.isoformat(), end.isoformat()))
        return [(dt.date.fromisoformat(r["day"]),
                 Checkin(r["employee_id"], dt.datetime.fromisoformat(r["first_seen"]), r["source"], r["score"],
                         r["image"])) for r in rows]

    def labelled_faces(self, start: dt.date, end: dt.date) -> list[tuple[str | None, str, str]]:
        """Camera snapshots between two days with what admins made of them: (employee or None for
        a stranger, image, kind). Kinds: camera (never corrected), corrected (moved to the right
        person), admin (unidentified face named on the review page), rejected ("not them") and
        ignored (unidentified face ignored)."""
        out = []
        for r in self._all("SELECT employee_id, image, source, status FROM sightings WHERE day BETWEEN ? AND ? "
                           "AND image LIKE 'crops/%' AND source != 'manual'", (start.isoformat(), end.isoformat())):
            if r["status"] == "rejected":
                out.append((None, r["image"], "rejected"))
            else:
                out.append((r["employee_id"], r["image"], r["source"]))
        for r in self._all("SELECT image FROM unknowns WHERE day BETWEEN ? AND ? AND status = 'ignored' "
                           "AND image IS NOT NULL", (start.isoformat(), end.isoformat())):
            out.append((None, r["image"], "ignored"))
        return out

    # unknown faces ---------------------------------------------------------

    def add_unknown(self, day: dt.date, ts: dt.datetime, image: str | None, model: str | None,
                    vector: np.ndarray | None, guess_id: str | None, guess_score: float | None,
                    group_id: int | None = None, ignored: bool = False, sha256: str | None = None) -> int:
        """Store an unidentified face; without a group it starts a new group of its own.
        `ignored` stores it as already ignored for the day (its group was ignored earlier today)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO unknowns (day, ts, image, model, vector, guess_id, guess_score, group_id, status, "
                "ignored_day, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day.isoformat(), ts.isoformat(), image, model, _blob(vector), guess_id, guess_score, group_id,
                 "ignored" if ignored else "pending", day.isoformat() if ignored else None, sha256),
            )
            if group_id is None:
                self._db.execute("UPDATE unknowns SET group_id = id WHERE id = ?", (cur.lastrowid,))
        return cur.lastrowid

    def _unknown(self, r) -> Unknown:
        return Unknown(r["id"], dt.datetime.fromisoformat(r["ts"]), r["image"], r["model"], _vec(r["vector"]),
                       r["guess_id"], r["guess_score"], r["status"], r["employee_id"], r["group_id"] or r["id"],
                       r["sha256"])

    def pending_groups(self) -> list[list[Unknown]]:
        """Pending unknown faces grouped by person, most recently seen group first."""
        groups: dict[int, list[Unknown]] = {}
        for u in self.unknowns():
            groups.setdefault(u.group_id, []).append(u)
        return sorted(groups.values(), key=lambda g: g[-1].ts, reverse=True)

    def group(self, group_id: int) -> list[Unknown]:
        rows = self._all("SELECT * FROM unknowns WHERE COALESCE(group_id, id) = ? AND status = 'pending' ORDER BY ts",
                         (group_id,))
        return [self._unknown(r) for r in rows]

    def ignore_group(self, group_id: int, day: dt.date) -> None:
        self._exec("UPDATE unknowns SET status = 'ignored', ignored_day = ? "
                   "WHERE COALESCE(group_id, id) = ? AND status = 'pending'", (day.isoformat(), group_id))

    def restore_group(self, group_id: int, day: dt.date) -> int:
        """Undo today's ignore of a group; returns how many faces came back."""
        cur = self._exec("UPDATE unknowns SET status = 'pending', ignored_day = NULL, notified = 1 "
                         "WHERE COALESCE(group_id, id) = ? AND status = 'ignored' AND ignored_day = ?",
                         (group_id, day.isoformat()))
        return cur.rowcount

    def ignored_groups(self, day: dt.date) -> list[list[Unknown]]:
        """Groups ignored on `day`, most recently seen first."""
        rows = self._all("SELECT * FROM unknowns WHERE status = 'ignored' AND ignored_day = ? ORDER BY ts",
                         (day.isoformat(),))
        groups: dict[int, list[Unknown]] = {}
        for r in rows:
            u = self._unknown(r)
            groups.setdefault(u.group_id, []).append(u)
        return sorted(groups.values(), key=lambda g: g[-1].ts, reverse=True)

    def resolve_group(self, group_id: int, status: str, employee_id: str | None) -> None:
        self._exec("UPDATE unknowns SET status = ?, employee_id = ? WHERE COALESCE(group_id, id) = ? AND status = 'pending'",
                   (status, employee_id, group_id))

    def pending_group_count(self, day: dt.date | None = None) -> int:
        sql, params = "SELECT COUNT(DISTINCT COALESCE(group_id, id)) AS n FROM unknowns WHERE status = 'pending'", []
        if day is not None:
            sql += " AND day = ?"
            params.append(day.isoformat())
        return self._all(sql, params)[0]["n"]

    def unknown(self, unknown_id: int) -> Unknown | None:
        rows = self._all("SELECT * FROM unknowns WHERE id = ?", (unknown_id,))
        return self._unknown(rows[0]) if rows else None

    def unknowns(self, day: dt.date | None = None, status: str | None = "pending",
                 since: dt.date | None = None) -> list[Unknown]:
        """`since`: only faces from that day on (grouping looks back a limited number of days)."""
        sql, params = "SELECT * FROM unknowns WHERE 1 = 1", []
        if day is not None:
            sql += " AND day = ?"
            params.append(day.isoformat())
        if since is not None:
            sql += " AND day >= ?"
            params.append(since.isoformat())
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        return [self._unknown(r) for r in self._all(sql + " ORDER BY ts", params)]

    def unnotified_unknowns(self, day: dt.date) -> list[Unknown]:
        rows = self._all(
            "SELECT * FROM unknowns WHERE day = ? AND status = 'pending' AND notified = 0 ORDER BY ts", (day.isoformat(),)
        )
        return [self._unknown(r) for r in rows]

    def mark_unknowns_notified(self, ids: list[int]) -> None:
        with self._lock, self._db:
            self._db.executemany("UPDATE unknowns SET notified = 1 WHERE id = ?", [(i,) for i in ids])

    def resolve_unknown(self, unknown_id: int, status: str, employee_id: str | None) -> None:
        self._exec("UPDATE unknowns SET status = ?, employee_id = ? WHERE id = ?", (status, employee_id, unknown_id))

    # holidays and leave (mirrored from the HR system) ---------------------------

    def calendar_day(self, day: dt.date) -> tuple[str, str] | None:
        rows = self._all("SELECT kind, note FROM calendar WHERE day = ?", (day.isoformat(),))
        return (rows[0]["kind"], rows[0]["note"]) if rows else None

    def replace_external(self, start: dt.date, end: dt.date, days: list[tuple[dt.date, str, str]],
                         leaves: list[tuple[str, dt.date, dt.date, str, str]]) -> None:
        """Replace holidays/work days and leave between start and end with a fresh copy from the HR system.
        days: (day, 'holiday' | 'workday', note); leaves: (employee_id, start, end, kind, note)."""
        s, e = start.isoformat(), end.isoformat()
        with self._lock, self._db:
            self._db.execute("DELETE FROM calendar WHERE day BETWEEN ? AND ?", (s, e))
            self._db.execute("DELETE FROM leaves WHERE end_day >= ? AND start_day <= ?", (s, e))
            self._db.executemany(
                "INSERT INTO calendar (day, kind, note) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET kind = excluded.kind, note = excluded.note",
                [(d.isoformat(), kind, note) for d, kind, note in days])
            self._db.executemany(
                "INSERT INTO leaves (employee_id, start_day, end_day, kind, note) VALUES (?, ?, ?, ?, ?)",
                [(emp, a.isoformat(), b.isoformat(), kind, note) for emp, a, b, kind, note in leaves])

    def leaves(self, ending_after: dt.date | None = None, employee_id: str | None = None) -> list[Leave]:
        sql, params = "SELECT * FROM leaves WHERE 1 = 1", []
        if ending_after is not None:
            sql += " AND end_day >= ?"
            params.append(ending_after.isoformat())
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        return [Leave(r["id"], r["employee_id"], dt.date.fromisoformat(r["start_day"]),
                      dt.date.fromisoformat(r["end_day"]), r["kind"], r["note"])
                for r in self._all(sql + " ORDER BY start_day", params)]

    def leaves_on(self, day: dt.date) -> dict[str, str]:
        """employee_id -> leave kind for everyone on leave that day."""
        rows = self._all("SELECT employee_id, kind FROM leaves WHERE start_day <= ? AND end_day >= ?",
                         (day.isoformat(), day.isoformat()))
        return {r["employee_id"]: r["kind"] for r in rows}

    # audit -----------------------------------------------------------------

    def log(self, now: dt.datetime, user: str, action: str, details: str) -> None:
        self._exec("INSERT INTO audit (ts, user, action, details) VALUES (?, ?, ?, ?)",
                   (now.isoformat(), user, action, details))

    def audit_about(self, day: dt.date, *names: str, limit: int = 200) -> list[tuple[dt.datetime, str, str, str]]:
        """Change log entries that mention this day and one of `names` (a person's name or id)."""
        rows = self._all("SELECT * FROM audit WHERE details LIKE ? ORDER BY id DESC LIMIT ?",
                         (f"%{day.isoformat()}%", limit))
        words = [re.compile(rf"(?<!\w){re.escape(n)}(?!\w)") for n in names if n]
        return [(dt.datetime.fromisoformat(r["ts"]), r["user"], r["action"], r["details"]) for r in rows
                if any(w.search(r["details"]) for w in words)]

    def audit(self, limit: int = 300) -> list[tuple[dt.datetime, str, str, str]]:
        rows = self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [(dt.datetime.fromisoformat(r["ts"]), r["user"], r["action"], r["details"]) for r in rows]

    # retention ---------------------------------------------------------------

    def expire_snapshots(self, before: dt.date, keep: set[tuple[dt.date, str]] = frozenset()) -> list[str]:
        """Forget camera snapshots taken before `before`, except those of the (day, employee) pairs in
        `keep` (days someone was late are kept longer, as evidence). Unknown faces from that period
        are removed entirely. Returns the image paths nothing refers to any more, for deletion."""
        with self._lock, self._db:
            day = before.isoformat()
            images: set[str] = set()
            rows = self._db.execute("SELECT id, day, employee_id, image FROM sightings WHERE day < ? "
                                    "AND image LIKE 'crops/%'", (day,)).fetchall()
            expired = [r for r in rows if (dt.date.fromisoformat(r["day"]), r["employee_id"]) not in keep]
            images |= {r["image"] for r in expired}
            self._db.executemany("UPDATE sightings SET image = NULL WHERE id = ?", [(r["id"],) for r in expired])
            rows = self._db.execute("SELECT day, employee_id, image FROM checkins WHERE day < ? "
                                    "AND image LIKE 'crops/%'", (day,)).fetchall()
            expired = [r for r in rows if (dt.date.fromisoformat(r["day"]), r["employee_id"]) not in keep]
            images |= {r["image"] for r in expired}
            self._db.executemany("UPDATE checkins SET image = NULL WHERE day = ? AND employee_id = ?",
                                 [(r["day"], r["employee_id"]) for r in expired])
            images |= {r["image"] for r in self._db.execute(
                "SELECT image FROM unknowns WHERE day < ? AND image IS NOT NULL", (day,))}
            self._db.execute("DELETE FROM unknowns WHERE day < ?", (day,))
            # A file still used by a kept sighting (a confirmed face that became an arrival) stays.
            used = {r["image"] for r in self._db.execute(
                "SELECT image FROM sightings WHERE image IS NOT NULL UNION SELECT image FROM checkins "
                "WHERE image IS NOT NULL UNION SELECT image FROM unknowns WHERE image IS NOT NULL")}
        return sorted(images - used)

    # notices, outages, outbox, kv -----------------------------------------

    def notice_sent(self, day: dt.date, kind: str) -> bool:
        return bool(self._all("SELECT 1 FROM notices WHERE day = ? AND kind = ?", (day.isoformat(), kind)))

    def notice_time(self, day: dt.date, kind: str) -> dt.datetime | None:
        rows = self._all("SELECT sent_at FROM notices WHERE day = ? AND kind = ?", (day.isoformat(), kind))
        return dt.datetime.fromisoformat(rows[0]["sent_at"]) if rows else None

    def mark_notice(self, day: dt.date, kind: str, now: dt.datetime) -> bool:
        """Returns False when the notice was already marked (by another thread, say)."""
        cur = self._exec("INSERT OR IGNORE INTO notices (day, kind, sent_at) VALUES (?, ?, ?)",
                         (day.isoformat(), kind, now.isoformat()))
        return cur.rowcount == 1

    def add_outage(self, day: dt.date, start: dt.datetime, end: dt.datetime) -> None:
        self._exec("INSERT INTO outages (day, start, end) VALUES (?, ?, ?)", (day.isoformat(), start.isoformat(), end.isoformat()))

    def outages(self, day: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
        rows = self._all("SELECT start, end FROM outages WHERE day = ? ORDER BY start", (day.isoformat(),))
        return [(dt.datetime.fromisoformat(r["start"]), dt.datetime.fromisoformat(r["end"])) for r in rows]

    def enqueue(self, payload: dict, now: dt.datetime) -> None:
        self._exec("INSERT INTO outbox (payload, created_at) VALUES (?, ?)", (json.dumps(payload, ensure_ascii=False), now.isoformat()))

    def outbox(self) -> list[tuple[int, dict, dt.datetime]]:
        rows = self._all("SELECT id, payload, created_at FROM outbox ORDER BY id")
        return [(r["id"], json.loads(r["payload"]), dt.datetime.fromisoformat(r["created_at"])) for r in rows]

    def outbox_done(self, item_id: int) -> None:
        self._exec("DELETE FROM outbox WHERE id = ?", (item_id,))

    def outbox_failed(self, item_id: int) -> None:
        self._exec("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (item_id,))

    def get(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def put(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))
