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
        if source not in {"camera", "seed", "collect"}:
            raise ValueError("unknown face source must be camera, seed or collect")
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

    def resolve_group(self, group_id: int, status: str, employee_id: str | None, *,
                      photo_ids: list[int] | None = None) -> None:
        sql = "UPDATE unknowns SET status = ?, employee_id = ? WHERE COALESCE(group_id, id) = ? AND status = 'pending'"
        params = [status, employee_id, group_id]
        if photo_ids is not None:
            sql += " AND id IN (" + ",".join("?" for _ in photo_ids) + ")"
            params.extend(photo_ids)
        with self._lock, self._db:
            changed = self._db.execute(sql, params).rowcount
            if photo_ids is not None and changed != len(set(photo_ids)):
                raise ValueError("Some selected photos have already been handled. Reload and try again.")

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

    # holidays and leave (mirrored from the HR system, or entered by an admin) ----

    def calendar_day(self, day: dt.date) -> tuple[str, str] | None:
        rows = self._all("SELECT kind, note FROM calendar WHERE day = ?", (day.isoformat(),))
        return (rows[0]["kind"], rows[0]["note"]) if rows else None

    def calendar_days(self, start: dt.date | None = None) -> list[tuple[dt.date, str, str, str]]:
        """(day, kind, note, source) from `start` on, oldest first."""
        rows = self._all("SELECT * FROM calendar WHERE day >= ? ORDER BY day", ((start or dt.date.min).isoformat(),))
        return [(dt.date.fromisoformat(r["day"]), r["kind"], r["note"], r["source"]) for r in rows]

    def set_calendar_day(self, day: dt.date, kind: str, note: str) -> None:
        """A holiday or extra work day entered by hand; it wins over the HR system's entry for that day."""
        if kind not in ("holiday", "workday"):
            raise ValueError("choose holiday or extra work day")
        self._exec("INSERT INTO calendar (day, kind, note, source) VALUES (?, ?, ?, 'manual') "
                   "ON CONFLICT(day) DO UPDATE SET kind = excluded.kind, note = excluded.note, source = 'manual'",
                   (day.isoformat(), kind, note.strip()))

    def delete_calendar_day(self, day: dt.date) -> bool:
        """Remove a hand-entered day (days from the HR system come back with the next update)."""
        return self._exec("DELETE FROM calendar WHERE day = ? AND source = 'manual'", (day.isoformat(),)).rowcount > 0

    def replace_external(self, start: dt.date, end: dt.date, days: list[tuple[dt.date, str, str]],
                         leaves: list[tuple[str, dt.date, dt.date, str, str]]) -> None:
        """Replace holidays/work days and leave between start and end with a fresh copy from the HR system.
        days: (day, 'holiday' | 'workday', note); leaves: (employee_id, start, end, kind, note).
        Entries made on the admin page are kept, and a hand-entered day wins over the HR system's."""
        s, e = start.isoformat(), end.isoformat()
        with self._lock, self._db:
            self._db.execute("DELETE FROM calendar WHERE day BETWEEN ? AND ? AND source = 'hr'", (s, e))
            self._db.execute("DELETE FROM leaves WHERE end_day >= ? AND start_day <= ? AND source = 'hr'", (s, e))
            self._db.executemany(
                "INSERT INTO calendar (day, kind, note) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET kind = excluded.kind, note = excluded.note WHERE source = 'hr'",
                [(d.isoformat(), kind, note) for d, kind, note in days])
            self._db.executemany(
                "INSERT INTO leaves (employee_id, start_day, end_day, kind, note) VALUES (?, ?, ?, ?, ?)",
                [(emp, a.isoformat(), b.isoformat(), kind, note) for emp, a, b, kind, note in leaves])

    def add_leave(self, employee_id: str, start: dt.date, end: dt.date, kind: str, note: str = "") -> int:
        """Leave entered on the admin page; counts exactly like leave from the HR system."""
        if end < start:
            raise ValueError("the leave ends before it starts")
        if not kind.strip():
            raise ValueError("choose the kind of leave")
        return self._exec("INSERT INTO leaves (employee_id, start_day, end_day, kind, note, source) "
                          "VALUES (?, ?, ?, ?, ?, 'manual')",
                          (employee_id, start.isoformat(), end.isoformat(), kind.strip(), note.strip())).lastrowid

    def leave(self, leave_id: int) -> Leave | None:
        rows = self._all("SELECT * FROM leaves WHERE id = ?", (leave_id,))
        return self._leave(rows[0]) if rows else None

    def delete_leave(self, leave_id: int) -> bool:
        """Remove hand-entered leave (leave from the HR system is changed there)."""
        return self._exec("DELETE FROM leaves WHERE id = ? AND source = 'manual'", (leave_id,)).rowcount > 0

    @staticmethod
    def _leave(r) -> Leave:
        return Leave(r["id"], r["employee_id"], dt.date.fromisoformat(r["start_day"]),
                     dt.date.fromisoformat(r["end_day"]), r["kind"], r["note"], r["source"])

    def leaves(self, ending_after: dt.date | None = None, employee_id: str | None = None) -> list[Leave]:
        sql, params = "SELECT * FROM leaves WHERE 1 = 1", []
        if ending_after is not None:
            sql += " AND end_day >= ?"
            params.append(ending_after.isoformat())
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        return [self._leave(r) for r in self._all(sql + " ORDER BY start_day", params)]

    def leaves_on(self, day: dt.date) -> dict[str, str]:
        """employee_id -> leave kind for everyone on leave that day."""
        rows = self._all("SELECT employee_id, kind FROM leaves WHERE start_day <= ? AND end_day >= ?",
                         (day.isoformat(), day.isoformat()))
        return {r["employee_id"]: r["kind"] for r in rows}

    # notes people write about their own day ---------------------------------------

    def set_day_note(self, day: dt.date, employee_id: str, note: str, now: dt.datetime) -> None:
        """Save a person's note for a day; an empty note removes it."""
        if note.strip():
            self._exec("INSERT INTO day_notes (day, employee_id, note, created_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(day, employee_id) DO UPDATE SET note = excluded.note, created_at = excluded.created_at",
                       (day.isoformat(), employee_id, note.strip(), now.isoformat()))
        else:
            self._exec("DELETE FROM day_notes WHERE day = ? AND employee_id = ?", (day.isoformat(), employee_id))

    def day_notes(self, day: dt.date) -> dict[str, str]:
        """employee_id -> note for that day."""
        rows = self._all("SELECT employee_id, note FROM day_notes WHERE day = ?", (day.isoformat(),))
        return {r["employee_id"]: r["note"] for r in rows}

    def notes_of(self, employee_id: str, start: dt.date, end: dt.date) -> dict[dt.date, str]:
        rows = self._all("SELECT day, note FROM day_notes WHERE employee_id = ? AND day BETWEEN ? AND ?",
                         (employee_id, start.isoformat(), end.isoformat()))
        return {dt.date.fromisoformat(r["day"]): r["note"] for r in rows}
