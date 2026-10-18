"""Audit log, retention, notices, outages, outbox, and key-value persistence mixin."""
from __future__ import annotations

import datetime as dt
import json
import re


class SystemStoreMixin:
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
                "SELECT image FROM unknowns WHERE day < ? AND image IS NOT NULL AND source='camera'", (day,))}
            self._db.execute("DELETE FROM unknowns WHERE day < ? AND source='camera'", (day,))
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
