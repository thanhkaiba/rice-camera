"""Sightings, check-ins, unknown faces, calendar, and leave persistence mixin."""
from __future__ import annotations

import datetime as dt

import numpy as np

from .schema import Checkin, Leave, Sighting, Unknown, _blob, _vec


class AttendanceStoreMixin:
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
        rows = self._all("SELECT day, COUNT(*) AS n FROM unknowns WHERE day BETWEEN ? AND ? AND source='camera' GROUP BY day",
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
                           "AND image IS NOT NULL AND source='camera'", (start.isoformat(), end.isoformat())):
            out.append((None, r["image"], "ignored"))
        return out

    # unknown faces ---------------------------------------------------------

    def add_unknown(self, day: dt.date, ts: dt.datetime, image: str | None, model: str | None,
                    vector: np.ndarray | None, guess_id: str | None, guess_score: float | None,
                    group_id: int | None = None, ignored: bool = False, sha256: str | None = None,
                    *, source: str = "camera") -> int:
        """Store an unidentified face; without a group it starts a new group of its own.
        `ignored` stores it as already ignored for the day (its group was ignored earlier today)."""
        if source not in {"camera", "seed"}:
            raise ValueError("unknown face source must be camera or seed")
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO unknowns (day, ts, image, model, vector, guess_id, guess_score, group_id, status, "
                "ignored_day, sha256, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day.isoformat(), ts.isoformat(), image, model, _blob(vector), guess_id, guess_score, group_id,
                 "ignored" if ignored else "pending", day.isoformat() if ignored else None, sha256, source),
            )
            if group_id is None:
                self._db.execute("UPDATE unknowns SET group_id = id WHERE id = ?", (cur.lastrowid,))
        return cur.lastrowid

    def _unknown(self, r) -> Unknown:
        return Unknown(r["id"], dt.datetime.fromisoformat(r["ts"]), r["image"], r["model"], _vec(r["vector"]),
                       r["guess_id"], r["guess_score"], r["status"], r["employee_id"], r["group_id"] or r["id"],
                       r["sha256"], r["source"])

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
            sql += " AND day = ? AND source='camera'"
            params.append(day.isoformat())
        return self._all(sql, params)[0]["n"]

    def unknown(self, unknown_id: int) -> Unknown | None:
        rows = self._all("SELECT * FROM unknowns WHERE id = ?", (unknown_id,))
        return self._unknown(rows[0]) if rows else None

    def refresh_seed_vector(self, unknown_id: int, model: str, vector: np.ndarray) -> bool:
        cur = self._exec("UPDATE unknowns SET model=?, vector=? WHERE id=? "
                         "AND source='seed' AND status='pending' AND vector IS NULL",
                         (model, _blob(vector), unknown_id))
        return bool(cur.rowcount)

    def label_seed(self, unknown_id: int, employee_id: str, model: str,
                   evidence: str, now: dt.datetime) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE unknowns SET status='assigned', employee_id=? WHERE id=? "
                         "AND source='seed' AND status='pending' AND employee_id IS NULL "
                         "AND model=? AND vector IS NOT NULL "
                         "AND EXISTS (SELECT 1 FROM employees WHERE id=? AND active=1) "
                         "AND NOT EXISTS (SELECT 1 FROM kv WHERE key=?)",
                         (employee_id, unknown_id, model, employee_id, f"seed_review_manual:{unknown_id}"))
            if not cur.rowcount:
                return False
            self._db.execute("INSERT OR REPLACE INTO kv (key,value) VALUES (?,?)",
                             (f"seed_review_auto:{unknown_id}", evidence))
            self._db.execute("INSERT INTO audit (ts,user,action,details) VALUES (?,?,?,?)",
                             (now.isoformat(), "seed", "auto-label seed", f"sample {unknown_id}; {evidence}"))
        return True

    def reopen_seed(self, unknown_id: int, employee_id: str, now: dt.datetime, user: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE unknowns SET status='pending', employee_id=NULL WHERE id=? "
                         "AND source='seed' AND status='assigned' AND employee_id=? "
                         "AND EXISTS (SELECT 1 FROM kv WHERE key=?) "
                         "AND NOT EXISTS (SELECT 1 FROM kv WHERE key=?)",
                         (unknown_id, employee_id, f"seed_review_auto:{unknown_id}", f"seed_review_manual:{unknown_id}"))
            if not cur.rowcount:
                return False
            self._db.execute("INSERT INTO kv (key,value) VALUES (?,?)", (f"seed_review_manual:{unknown_id}", "1"))
            self._db.execute("INSERT INTO audit (ts,user,action,details) VALUES (?,?,?,?)",
                             (now.isoformat(), user, "reopen seed", f"sample {unknown_id}; no attendance or learning"))
        return True

    def unknowns(self, day: dt.date | None = None, status: str | None = "pending",
                 *, source: str | None = None) -> list[Unknown]:
        sql, params = "SELECT * FROM unknowns WHERE 1 = 1", []
        if day is not None:
            sql += " AND day = ?"
            params.append(day.isoformat())
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        return [self._unknown(r) for r in self._all(sql + " ORDER BY ts", params)]

    def unnotified_unknowns(self, day: dt.date) -> list[Unknown]:
        rows = self._all(
            "SELECT * FROM unknowns WHERE day = ? AND status = 'pending' AND notified = 0 AND source='camera' ORDER BY ts", (day.isoformat(),)
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
