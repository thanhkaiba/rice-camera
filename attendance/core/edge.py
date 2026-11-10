"""The office Mac's side of the link to the server: every camera event is saved to disk first, then
delivered over HTTPS until the server confirms it. The camera loop never waits for the network."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import numpy as np

from ..store import body_photo, scene_photo

log = logging.getLogger(__name__)

BATCH = 50
HEARTBEAT_S = 30.0
STATE_S = 60.0
STATE_DAYS = 30  # how far back a correction on the Mac still reaches the server
STATE_KEY = "edge_state_sent"
MAX_STATE_DAYS = 7  # per request, so a slow line can finish one within the timeout
MAX_STATE_EMBEDDINGS = 300
IDLE_S = 3.0
MAX_BACKOFF_S = 300.0
TIMEOUT_S = 30.0
# Answers that will never change by trying again: the item is dropped so it cannot block the queue.
PERMANENT = {400, 404, 413, 415, 422}


def _vector(vector) -> str | None:
    return None if vector is None else base64.b64encode(np.asarray(vector, dtype=np.float32).tobytes()).decode()


class EdgeClient:
    def __init__(self, cfg, store, data_dir: Path, status=lambda: {}, send=None, sleep=time.sleep):
        self.cfg, self.store, self.data_dir, self.status = cfg, store, Path(data_dir), status
        self._send = send or self._urlopen
        self._sleep = sleep

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.server_url and self.cfg.token)

    # called from the camera loop: must never raise --------------------------------

    def identified(self, employee_id: str, ts: dt.datetime, score, image, vector, sha256) -> None:
        self._enqueue({"kind": "identified", "ts": ts.isoformat(), "employee_id": employee_id, "score": score,
                       "image": image, "vector": _vector(vector), "sha256": sha256})

    def unknown(self, ts: dt.datetime, image, vector, guess_id, guess_score, sha256, quality,
                track=None, follows=None, frames=None, votes=None) -> None:
        self._enqueue({"kind": "unknown", "ts": ts.isoformat(), "image": image, "vector": _vector(vector),
                       "guess_id": guess_id, "guess_score": guess_score, "sha256": sha256, "quality": quality,
                       "track": track, "follows": follows, "frames": frames, "votes": votes})

    def _enqueue(self, event: dict) -> None:
        try:
            self.store.edge_enqueue({"id": uuid.uuid4().hex, **event}, dt.datetime.now(dt.timezone.utc))
        except Exception:
            log.exception("Could not queue a camera event for the server")

    # delivery -----------------------------------------------------------------------

    def _urlopen(self, method: str, path: str, body: bytes, content_type: str) -> tuple[int, bytes]:
        request = urllib.request.Request(self.cfg.server_url.rstrip("/") + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.cfg.token}", "Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _photos(self, event: dict) -> None:
        self._upload(event.get("image"))

    def _upload(self, image: str | None) -> None:
        if not image:
            return
        for rel in (image, body_photo(image), scene_photo(image)):
            path = self.data_dir / rel
            if not path.exists():
                continue
            status, _ = self._send("PUT", "/api/edge/photos/" + urllib.parse.quote(rel), path.read_bytes(), "image/jpeg")
            if status in PERMANENT:
                log.warning("The server refused the photo %s (HTTP %s)", rel, status)
            elif status != 200:
                raise ConnectionError(f"photo upload answered HTTP {status}")

    def deliver_batch(self) -> int:
        """Send the oldest queued events; returns how many left the queue. Raises when the server cannot be reached."""
        rows = self.store.edge_pending(BATCH)
        if not rows:
            return 0
        events = []
        for row in rows:
            event = json.loads(row["payload"])
            if not row["photos_sent"]:
                self._photos(event)
                self.store.edge_photos_sent(row["id"])
            events.append(event)
        status, body = self._send("POST", "/api/edge/events", json.dumps({"events": events}).encode(), "application/json")
        if status != 200:
            raise ConnectionError(f"events answered HTTP {status}")
        answer = json.loads(body)
        for item in answer.get("rejected", []):
            log.warning("The server refused event %s: %s", item.get("id"), item.get("reason"))
        done = list(answer.get("accepted", [])) + [item.get("id") for item in answer.get("rejected", [])]
        self.store.edge_delivered(done)
        return len(done)

    def sync_state(self) -> None:
        """What admins did on the Mac (named a face, corrected an arrival, edited someone) reaches the server as
        the resulting rows. Waits for the event queue, so every camera event is applied first."""
        if self.store.edge_backlog():
            return
        sent = json.loads(self.store.get(STATE_KEY) or "{}")
        since = (dt.date.today() - dt.timedelta(days=STATE_DAYS)).isoformat()
        hashes = self.store.edge_day_hashes(since)
        days = sorted((d for d, h in hashes.items() if sent.get("days", {}).get(d) != h), reverse=True)[:MAX_STATE_DAYS]
        employees = self.store.edge_employee_rows()
        people = hashlib.md5(json.dumps(employees, sort_keys=True).encode()).hexdigest()
        ids = self.store.edge_embedding_ids()
        gallery = hashlib.md5(json.dumps(ids).encode()).hexdigest()
        new = self.store.edge_embedding_rows(sent.get("after", 0), MAX_STATE_EMBEDDINGS)
        if not days and people == sent.get("people") and gallery == sent.get("gallery") and not new:
            return
        status, body = self._send("POST", "/api/edge/state", json.dumps({
            "days": days, "tables": self.store.edge_day_rows(days) if days else {},
            "employees": employees if people != sent.get("people") else None,
            "embedding_ids": ids if gallery != sent.get("gallery") else None, "embeddings": new}).encode(), "application/json")
        if status in PERMANENT:
            log.warning("The server refused the state update (HTTP %s)", status)
            return
        if status != 200:
            raise ConnectionError(f"state answered HTTP {status}")
        for image in json.loads(body).get("missing", []):
            self._upload(image)
        sent.setdefault("days", {}).update({d: hashes[d] for d in days})
        sent["days"] = {d: h for d, h in sent["days"].items() if d >= since}
        sent.update(people=people, gallery=gallery, after=new[-1]["id"] if new else sent.get("after", 0))
        self.store.put(STATE_KEY, json.dumps(sent))

    def heartbeat(self) -> None:
        body = json.dumps({**self.status(), "backlog": self.store.edge_backlog()}).encode()
        status, _ = self._send("POST", "/api/edge/heartbeat", body, "application/json")
        if status != 200:
            raise ConnectionError(f"heartbeat answered HTTP {status}")

    def _loop(self) -> None:
        backoff, last_beat, last_state = IDLE_S, 0.0, 0.0
        while True:
            try:
                if time.monotonic() - last_beat >= HEARTBEAT_S:
                    self.heartbeat()
                    last_beat = time.monotonic()
                sent = self.deliver_batch()
                if not sent and time.monotonic() - last_state >= STATE_S:
                    self.sync_state()
                    last_state = time.monotonic()
                backoff = IDLE_S
                self._sleep(0 if sent else IDLE_S)
            except Exception as exc:
                log.warning("Server link down (%s); retrying in %.0f s, %d events waiting",
                            type(exc).__name__, backoff, self.store.edge_backlog())
                self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._loop, name="edge-link", daemon=True).start()
            log.info("Mirroring camera events to %s", self.cfg.server_url)
