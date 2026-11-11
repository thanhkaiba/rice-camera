"""What the office Mac still has to deliver to the server, and which deliveries the server has already applied."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import sqlite3

# What the Mac keeps in step with the server until the cutover: every row of a day, by day.
DAY_TABLES = {  # columns whose change means the day must be sent again
    "unknowns": "id, status, employee_id, group_id, ignored_day, image, source",
    "sightings": "id, employee_id, ts, source, status, image",
    "checkins": "employee_id, first_seen, source, image",
    "day_notes": "employee_id, note",
    "outages": "start, end",
}
BLOB = "$b"
EDGE_JOB_LAST_KEY = "edge_job_last"  # the last job id given, so none is given again


def _plain(row: sqlite3.Row) -> dict:
    return {key: {BLOB: base64.b64encode(row[key]).decode()} if isinstance(row[key], bytes) else row[key] for key in row.keys()}


def _stored(value):
    return base64.b64decode(value[BLOB]) if isinstance(value, dict) else value


class EdgeStoreMixin:
    # On the Mac ----------------------------------------------------------------
    def edge_enqueue(self, event: dict, now: dt.datetime) -> None:
        self._exec("INSERT OR IGNORE INTO edge_outbox (id, created_at, payload) VALUES (?, ?, ?)",
                   (event["id"], now.isoformat(), json.dumps(event)))

    def edge_pending(self, limit: int) -> list[sqlite3.Row]:
        return self._all("SELECT id, payload, photos_sent FROM edge_outbox ORDER BY rowid LIMIT ?", (limit,))

    def edge_photos_sent(self, event_id: str) -> None:
        self._exec("UPDATE edge_outbox SET photos_sent = 1 WHERE id = ?", (event_id,))

    def edge_delivered(self, event_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM edge_outbox WHERE id = ?", [(i,) for i in event_ids])

    def edge_backlog(self) -> int:
        return self._all("SELECT COUNT(*) AS n FROM edge_outbox")[0]["n"]

    def edge_day_hashes(self, since: str) -> dict[str, str]:
        digests: dict[str, "hashlib._Hash"] = {}
        for table, columns in DAY_TABLES.items():
            for row in self._all(f"SELECT day, {columns} FROM {table} WHERE day >= ? ORDER BY day, rowid", (since,)):
                digests.setdefault(row["day"], hashlib.md5()).update(repr((table, *tuple(row)[1:])).encode())
        return {day: digest.hexdigest() for day, digest in digests.items()}

    def edge_day_rows(self, days: list[str]) -> dict[str, list[dict]]:
        marks = ",".join("?" * len(days))
        return {table: [_plain(r) for r in self._all(f"SELECT * FROM {table} WHERE day IN ({marks})", tuple(days))]
                for table in DAY_TABLES}

    def edge_employee_rows(self) -> list[dict]:
        return [_plain(r) for r in self._all("SELECT * FROM employees ORDER BY id")]

    def edge_embedding_ids(self) -> list[int]:
        return [r["id"] for r in self._all("SELECT id FROM embeddings ORDER BY id")]

    def edge_embedding_rows(self, after: int, limit: int) -> list[dict]:
        return [_plain(r) for r in self._all("SELECT * FROM embeddings WHERE id > ? ORDER BY id LIMIT ?", (after, limit))]

    def edge_embedding_keys(self) -> list[str]:
        """One text per embedding that changes if the row is replaced, so two sides can be compared cheaply."""
        return [f"{r['id']}:{r['employee_id']}:{r['model']}:{r['created_at']}" for r in
                self._all("SELECT id, employee_id, model, created_at FROM embeddings ORDER BY id")]

    def edge_gallery_version(self) -> str:
        people = json.dumps(self.edge_employee_rows(), sort_keys=True)
        return hashlib.md5((people + "|".join(self.edge_embedding_keys())).encode()).hexdigest()

    def edge_embeddings_by_id(self, ids: list[int]) -> list[dict]:
        marks = ",".join("?" * len(ids))
        return [_plain(r) for r in self._all(f"SELECT * FROM embeddings WHERE id IN ({marks})", tuple(ids))]

    def edge_follow_gallery(self, employees: list[dict], keys: list[str]) -> list[int]:
        """The server's list wins: what it no longer has goes; returns the embeddings still to fetch."""
        wanted, have = set(keys), set(self.edge_embedding_keys())
        with self._lock, self._db:
            self._db.execute(f"DELETE FROM employees WHERE id NOT IN ({','.join('?' * len(employees))})",
                             [row["id"] for row in employees])
            for row in employees:
                self._edge_write("employees", row)
            for key in have - wanted:
                self._db.execute("DELETE FROM embeddings WHERE id = ?", (int(key.split(":", 1)[0]),))
        return sorted(int(key.split(":", 1)[0]) for key in wanted - have)

    def edge_store_embeddings(self, rows: list[dict]) -> None:
        with self._lock, self._db:
            for row in rows:
                self._edge_write("embeddings", row)

    # On the server -------------------------------------------------------------
    def edge_job_add(self, kind: str, payload: dict, now: dt.datetime) -> None:
        """A job for the Mac. Ids are never given twice (SQLite would start again at 1 once the table is
        empty), so the Mac can tell a new job from one it already did."""
        with self._lock, self._db:
            last = self._db.execute("SELECT value FROM kv WHERE key = ?", (EDGE_JOB_LAST_KEY,)).fetchone()
            top = self._db.execute("SELECT COALESCE(MAX(id), 0) FROM edge_jobs").fetchone()[0]
            job_id = max(int(last[0]) if last else 0, top) + 1
            self._db.execute("INSERT INTO edge_jobs (id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                             (job_id, kind, json.dumps(payload), now.isoformat()))
            self._db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             (EDGE_JOB_LAST_KEY, str(job_id)))

    def edge_jobs(self, limit: int = 20) -> list[dict]:
        return [{"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"])}
                for r in self._all("SELECT * FROM edge_jobs ORDER BY id LIMIT ?", (limit,))]

    def edge_job_take(self, job_id: int) -> dict | None:
        rows = self._all("SELECT payload FROM edge_jobs WHERE id = ?", (job_id,))
        self._exec("DELETE FROM edge_jobs WHERE id = ?", (job_id,))
        return json.loads(rows[0]["payload"]) if rows else None

    def _edge_write(self, table: str, row: dict) -> None:
        columns = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
        kept = [c for c in row if c in columns]
        marks = ",".join("?" * len(kept))
        self._db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(kept)}) VALUES ({marks})", [_stored(row[c]) for c in kept])

    def edge_camera_times(self, day: str) -> set[str]:
        return {r["ts"] for r in self._all("SELECT ts FROM sightings WHERE day = ? AND source = 'camera'", (day,))}

    def edge_replace_state(self, days: list[str], tables: dict, employees: list | None, embedding_ids: list | None,
                           embeddings: list) -> None:
        """The Mac's rows replace the server's for those days, all or nothing."""
        allowed = set(days)
        marks = ",".join("?" * len(days))
        with self._lock, self._db:
            for table in DAY_TABLES:
                self._db.execute(f"DELETE FROM {table} WHERE day IN ({marks})", tuple(days))
                for row in tables.get(table, []):
                    if row["day"] not in allowed:
                        raise ValueError("a row is outside the days sent")
                    self._edge_write(table, row)
            if employees is not None:
                self._db.execute(f"DELETE FROM employees WHERE id NOT IN ({','.join('?' * len(employees))})",
                                 [row["id"] for row in employees])
                for row in employees:
                    self._edge_write("employees", row)
            if embedding_ids is not None:
                keep = set(embedding_ids)
                for (rowid,) in self._db.execute("SELECT id FROM embeddings").fetchall():
                    if rowid not in keep:
                        self._db.execute("DELETE FROM embeddings WHERE id = ?", (rowid,))
            for row in embeddings:
                self._edge_write("embeddings", row)

    def edge_applied(self, event_id: str) -> bool:
        return bool(self._all("SELECT 1 FROM edge_events WHERE id = ?", (event_id,)))

    def edge_mark_applied(self, event_id: str, now: dt.datetime) -> None:
        self._exec("INSERT OR IGNORE INTO edge_events (id, received_at) VALUES (?, ?)", (event_id, now.isoformat()))
