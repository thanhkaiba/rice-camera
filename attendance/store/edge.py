"""What the office Mac still has to deliver to the server, and which deliveries the server has already applied."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3


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

    # On the server -------------------------------------------------------------
    def edge_applied(self, event_id: str) -> bool:
        return bool(self._all("SELECT 1 FROM edge_events WHERE id = ?", (event_id,)))

    def edge_mark_applied(self, event_id: str, now: dt.datetime) -> None:
        self._exec("INSERT OR IGNORE INTO edge_events (id, received_at) VALUES (?, ?)", (event_id, now.isoformat()))
