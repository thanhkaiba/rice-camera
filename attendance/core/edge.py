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
FOLLOWING_KEY = "edge_following"  # set while the server is the one in charge
FOLLOW_KEY = "edge_follow_versions"
# Results the server has not acknowledged yet, by job id, sent again until it has; a delivered one is
# forgotten. ("edge_jobs_done", before, kept every result, and an id the server gave again got an old answer.)
JOBS_KEY = "edge_jobs_unsent"
FOLLOW_S = 30.0
FETCH_EMBEDDINGS = 200
JOBS_REMEMBERED = 50
UPCOMING_KEY = "edge_upcoming_sent"  # digest of the day's written greetings the server last took


def _vector(vector) -> str | None:
    return None if vector is None else base64.b64encode(np.asarray(vector, dtype=np.float32).tobytes()).decode()


class EdgeClient:
    def __init__(self, cfg, store, data_dir: Path, status=lambda: {}, send=None, sleep=time.sleep, today=dt.date.today):
        self.cfg, self.store, self.data_dir, self.status = cfg, store, Path(data_dir), status
        self._send = send or self._urlopen
        self._sleep, self._today = sleep, today
        self.following = store.get(FOLLOWING_KEY) == "1"
        # Set by the app: what to do with what the server sends.
        self.on_gallery = lambda: None
        self.on_config = lambda config: None
        self.add_photo = None
        self.add_voice = None
        self.camera_address = lambda: {}
        self.on_rules = lambda rules: None
        self.on_arrived = lambda arrived: None
        self.on_say = lambda: {"ok": False, "message": "no greetings on this computer"}
        self.on_speaker = None  # plays a test sound and tells what it played on
        self.render_text = None
        self.made_audio = None  # (employee id, on_time/late/short, index, day) -> the WAV made ahead, for the Greetings page
        self.on_create_voice = None
        self.upcoming = None  # day -> the day's written greetings, shown on the server's Greetings page
        self._preview_results = {}
        self._creation_jobs = {}
        # Faces from a .zip of photos for Review (core/photobatch.py): (archive, folder, stem) -> (samples, counts,
        # model); never started while faces_busy() (the camera hours), and run beside the camera, not in its way.
        self.faces_from_photos = None
        self.faces_busy = lambda: False
        self.seed_vectors = None  # (seed zip, folder) -> the face vectors of its review samples, for the server
        self._face_jobs = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.server_url and self.cfg.token)

    # called from the camera loop: must never raise --------------------------------

    def identified(self, employee_id: str, ts: dt.datetime, score, image, vector, sha256) -> None:
        self._enqueue({"kind": "identified", "ts": ts.isoformat(), "employee_id": employee_id, "score": score,
                       "image": image, "vector": _vector(vector), "sha256": sha256})

    def unknown(self, ts: dt.datetime, image, vector, guess_id, guess_score, sha256, quality,
                track=None, follows=None, frames=None, votes=None, source=None) -> None:
        self._enqueue({"kind": "unknown", "ts": ts.isoformat(), "image": image, "vector": _vector(vector),
                       "guess_id": guess_id, "guess_score": guess_score, "sha256": sha256, "quality": quality,
                       "track": track, "follows": follows, "frames": frames, "votes": votes, "source": source})

    def reference(self, employee_id: str, vector, source: str, ts: dt.datetime, image, quality_ok, quality, model: str) -> None:
        """A reference photo made here (door enrolment, collection, a photo sent by the server)."""
        self._enqueue({"kind": "reference", "ts": ts.isoformat(), "employee_id": employee_id, "image": image, "vector": _vector(vector),
                       "source": source, "quality_ok": quality_ok, "quality": quality, "model": model})

    def alert(self, text: str) -> None:
        self._enqueue({"kind": "alert", "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "text": text})

    def greeted(self, employee_ids: list[str], ts: dt.datetime, text: str, said: str) -> None:
        """What the door said to these people, for the server's Today page."""
        self._enqueue({"kind": "greeted", "ts": ts.isoformat(), "employee_ids": employee_ids, "text": text, "said": said})

    def crowd(self, row: dict) -> None:
        """A group at the door and how many of it were named (crowd.py), for the server's Groups page."""
        self._enqueue({"kind": "crowd", "ts": row["start"], "until": row["end"], "people": row["people"],
                       "faces": row["faces"], "named": row["named"], "names": row["names"], "bodies": row["bodies"],
                       "image": row["image"]})

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
        since = (self._today() - dt.timedelta(days=STATE_DAYS)).isoformat()
        hashes = self.store.edge_day_hashes(since)
        days = sorted((d for d, h in hashes.items() if sent.get("days", {}).get(d) != h), reverse=True)[:MAX_STATE_DAYS]
        employees = self.store.edge_employee_rows()
        people = hashlib.md5(json.dumps(employees, sort_keys=True).encode()).hexdigest()
        ids = self.store.edge_embedding_ids()
        gallery = hashlib.md5(json.dumps(ids).encode()).hexdigest()
        new = self.store.edge_embedding_rows(sent.get("after", 0), MAX_STATE_EMBEDDINGS)
        camera = self.camera_address()
        address = hashlib.md5(json.dumps(camera, sort_keys=True).encode()).hexdigest()
        if not days and people == sent.get("people") and gallery == sent.get("gallery") and not new and address == sent.get("camera"):
            return
        status, body = self._send("POST", "/api/edge/state", json.dumps({
            "days": days, "tables": self.store.edge_day_rows(days) if days else {},
            "employees": employees if people != sent.get("people") else None,
            "embedding_ids": ids if gallery != sent.get("gallery") else None, "embeddings": new,
            "camera": camera if address != sent.get("camera") else None}).encode(), "application/json")
        if status in PERMANENT:
            log.warning("The server refused the state update (HTTP %s)", status)
            return
        if status != 200:
            raise ConnectionError(f"state answered HTTP {status}")
        for image in json.loads(body).get("missing", []):
            self._upload(image)
        sent.setdefault("days", {}).update({d: hashes[d] for d in days})
        sent["days"] = {d: h for d, h in sent["days"].items() if d >= since}
        sent.update(people=people, gallery=gallery, camera=address, after=new[-1]["id"] if new else sent.get("after", 0))
        self.store.put(STATE_KEY, json.dumps(sent))

    def send_upcoming(self) -> None:
        """The day's written greetings reach the server's Greetings page, each time they change."""
        if self.upcoming is None:
            return
        try:
            body = json.dumps(self.upcoming(self._today()), ensure_ascii=False, sort_keys=True).encode()
        except Exception:  # a fault here must not hold up the camera's events
            log.exception("Could not read today's greetings for the server")
            return
        digest = hashlib.md5(body).hexdigest()
        if digest == self.store.get(UPCOMING_KEY):
            return
        status, _ = self._send("POST", "/api/edge/greetings/upcoming", body, "application/json")
        if status in PERMANENT:  # an older server has no page for them: tried again next time, quietly
            log.debug("The server did not take today's greetings (HTTP %s)", status)
            return
        if status != 200:
            raise ConnectionError(f"upcoming greetings answered HTTP {status}")
        self.store.put(UPCOMING_KEY, digest)

    def heartbeat(self) -> None:
        body = json.dumps({**self.status(), "backlog": self.store.edge_backlog()}).encode()
        status, answer = self._send("POST", "/api/edge/heartbeat", body, "application/json")
        if status != 200:
            raise ConnectionError(f"heartbeat answered HTTP {status}")
        mode = json.loads(answer or b"{}").get("mode")
        if mode and (mode == "live") != self.following:
            self.following = mode == "live"
            self.store.put(FOLLOWING_KEY, "1" if self.following else "0")
            log.info("The server is %s", "in charge now; this computer only runs the camera" if self.following else "no longer in charge")

    def follow(self) -> None:
        """Bring the people, their faces and the settings from the server, then do any work it left for this computer."""
        if self.store.edge_backlog():  # what this computer made must reach the server before its list replaces ours
            return
        versions = json.loads(self.store.get(FOLLOW_KEY) or "{}")
        query = urllib.parse.urlencode({"gallery": versions.get("gallery", ""), "config": versions.get("config", "")})
        status, body = self._send("GET", "/api/edge/sync?" + query, b"", "application/json")
        if status != 200:
            raise ConnectionError(f"sync answered HTTP {status}")
        answer = json.loads(body)
        if "embedding_keys" in answer:
            missing = self.store.edge_follow_gallery(answer["employees"], answer["embedding_keys"])
            for start in range(0, len(missing), FETCH_EMBEDDINGS):
                status, rows = self._send("POST", "/api/edge/sync/embeddings",
                                          json.dumps({"ids": missing[start:start + FETCH_EMBEDDINGS]}).encode(), "application/json")
                if status != 200:
                    raise ConnectionError(f"embeddings answered HTTP {status}")
                self.store.edge_store_embeddings(json.loads(rows))
            self.on_gallery()
            versions["gallery"] = answer["gallery"]
        if "config" in answer:
            self.on_config(answer["config"])
            versions["config"] = answer["config_version"]
        self.store.put(FOLLOW_KEY, json.dumps(versions))
        if "greeting" in answer:
            self.on_rules(answer["greeting"])
        if "arrived" in answer:
            self.on_arrived(answer["arrived"])
        jobs = answer.get("jobs", [])
        active = {str(job["id"]) for job in jobs}
        self._preview_results = {key: item for key, item in self._preview_results.items()
                                 if key in active and time.time() < item["expires"]}
        self._creation_jobs = {key: item for key, item in self._creation_jobs.items()
                               if item["thread"].is_alive() or key in active and time.time() < item["expires"]}
        self._face_jobs = {key: item for key, item in self._face_jobs.items() if item["thread"].is_alive() or key in active}
        for job in jobs:
            self._do_job(job)

    def _upload_preview(self, token: str, audio: bytes) -> None:
        if not 12 <= len(audio) <= 4 * 1024 * 1024:
            raise ValueError("Preview audio must be a WAV of at most 4 MB.")
        for offset in range(0, len(audio), 512 * 1024):
            path = f"/api/edge/previews/{urllib.parse.quote(token)}?offset={offset}&total={len(audio)}"
            status, body = self._send("PUT", path, audio[offset:offset + 512 * 1024], "audio/wav")
            if status != 200:
                raise ConnectionError(f"preview upload answered HTTP {status}: {body[:200].decode(errors='replace')}")

    def _do_preview(self, job: dict) -> None:
        key, payload = str(job["id"]), job["payload"]
        cached = self._preview_results.get(key)
        if cached is None:
            try:
                if time.time() >= payload["expires"]:
                    raise ValueError("Text preview expired. Generate audio again.")
                if job.get("kind") == "greeting_audio":  # a sentence of the day, from the file made ahead
                    if self.made_audio is None:
                        raise ValueError("Listening to the day's audio needs a newer app on the camera.")
                    audio = self.made_audio(payload["employee_id"], payload["line"], int(payload["index"]),
                                            dt.date.fromisoformat(payload["day"]))
                else:
                    if self.render_text is None:
                        raise ValueError("Text previews are not available on this camera. Update the camera app.")
                    cloud = payload.get("cloud")  # the ElevenLabs values being tried on the Settings page
                    audio = (self.render_text(payload["text"], payload["voice"]) if cloud is None
                             else self.render_text(payload["text"], payload["voice"], cloud))
                if not 12 <= len(audio) <= 4 * 1024 * 1024:
                    raise ValueError("Preview audio must be a WAV of at most 4 MB.")
                result = {"ok": True}
            except Exception as exc:
                log.exception("Text voice preview failed on camera")
                audio = None
                result = {"ok": False, "message": f"Camera: {type(exc).__name__}: {exc}"[:400]}
            cached = {"expires": payload["expires"], "result": result, "audio": audio}
            self._preview_results[key] = cached
        if cached["audio"] is not None and not cached.get("uploaded"):
            self._upload_preview(payload["preview_id"], cached["audio"])
            cached["uploaded"] = True
        status, _ = self._send("POST", f"/api/edge/jobs/{job['id']}/result",
                               json.dumps(cached["result"]).encode(), "application/json")
        if status != 200 and status not in PERMANENT:
            raise ConnectionError(f"job result answered HTTP {status}")
        self._preview_results.pop(key, None)

    def _run_creation(self, payload: dict, pending: dict) -> None:
        try:
            if time.time() >= payload["expires"]:
                raise ValueError("Voice creation expired. Upload the recording again.")
            if self.on_create_voice is None:
                raise ValueError("Voice creation is not available on this camera. Update the app.")
            pending["result"] = self.on_create_voice(payload)
        except Exception as exc:
            log.exception("Voice creation failed on camera")
            pending["result"] = {"ok": False, "message": f"Camera: {type(exc).__name__}: {exc}"[:400]}

    def _do_job(self, job: dict) -> None:
        if job.get("kind") in ("preview_text", "greeting_audio"):
            self._do_preview(job)
            return
        key = str(job["id"])
        unsent = json.loads(self.store.get(JOBS_KEY) or "{}")
        result = unsent.get(key)
        if result is None:  # a result the server did not get is sent again, the photo is not added twice
            if job.get("kind") in ("faces", "seed_vectors"):  # face models on the photos: outside the camera hours
                pending = self._face_jobs.get(key)
                if pending is None:
                    if self.faces_busy():
                        return  # the camera hours: asked again until they are over
                    pending = {}
                    run = self._run_faces if job.get("kind") == "faces" else self._run_seed_vectors
                    pending["thread"] = threading.Thread(target=run, args=(key, job["payload"], pending),
                                                         name="edge-faces", daemon=True)
                    self._face_jobs[key] = pending
                    pending["thread"].start()
                    return
                if pending["thread"].is_alive():
                    return
                result = pending["result"]
            elif job.get("kind") == "create_voice":
                pending = self._creation_jobs.get(key)
                if pending is None and time.time() >= job["payload"]["expires"]:
                    result = {"ok": False, "message": "Voice creation expired. Upload the recording again."}
                elif pending is None:
                    pending = {"expires": job["payload"]["expires"]}
                    pending["thread"] = threading.Thread(target=self._run_creation, args=(job["payload"], pending),
                                                         name="edge-create-voice", daemon=True)
                    self._creation_jobs[key] = pending
                    pending["thread"].start()
                    return
                elif pending["thread"].is_alive():
                    return
                else:
                    result = pending["result"]
            else:
                if job.get("kind") == "moment":
                    result = self._moment(job["payload"])
                elif job.get("kind") == "speaker":
                    result = (self.on_speaker() if self.on_speaker is not None else
                              {"ok": False, "message": "The speaker test is not available on this camera. Update the app."})
                else:
                    result = self.on_say() if job.get("kind") == "say" else self._add_uploaded(job["payload"])
            unsent[key] = result
            self.store.put(JOBS_KEY, json.dumps(dict(list(unsent.items())[-JOBS_REMEMBERED:])))
            self._creation_jobs.pop(key, None)
        status, _ = self._send("POST", f"/api/edge/jobs/{job['id']}/result", json.dumps(result).encode(), "application/json")
        if status != 200 and status not in PERMANENT:
            raise ConnectionError(f"job result answered HTTP {status}")
        unsent = json.loads(self.store.get(JOBS_KEY) or "{}")
        unsent.pop(key, None)  # the server has it and has closed the job: a later job with this id is a new one
        self.store.put(JOBS_KEY, json.dumps(unsent))

    def _run_faces(self, key: str, payload: dict, pending: dict) -> None:
        """The faces worth reviewing in an uploaded .zip of photos: found here, their pictures sent to the server,
        their face vectors in the result (the server has no face models), for it to put them on Review."""
        import shutil

        folder = self.data_dir / "photo-batches" / key
        try:
            if self.faces_from_photos is None:
                raise ValueError("Faces from photos are not available on this camera computer. Update the app.")
            status, data = self._send("GET", "/api/edge/files/" + urllib.parse.quote(payload["image"]), b"",
                                      "application/octet-stream")
            if status != 200:
                raise ConnectionError(f"the photos answered HTTP {status}")
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder / "photos.zip"
            archive.write_bytes(data)
            del data
            samples, counts, model = self.faces_from_photos(archive, folder / "faces", f"faces_{payload.get('batch', key)}")
            day = dt.date.today().isoformat()
            sent = []
            for sample in samples:
                rel = f"crops/{day}/{sample['file']}"
                for path, name in ((rel, sample["file"]), (scene_photo(rel), sample["scene"])):
                    status, _ = self._send("PUT", "/api/edge/photos/" + urllib.parse.quote(path),
                                           (folder / "faces" / name).read_bytes(), "image/jpeg")
                    if status != 200:
                        raise ConnectionError(f"face picture upload answered HTTP {status}")
                sent.append({"image": rel, "photo": sample.get("photo", ""),
                             "vector": base64.b64encode(np.asarray(sample["vector"], np.float32).tobytes()).decode()})
            pending["result"] = {"ok": True, "samples": sent, "model": model, "counts": counts,
                                 "message": f"{len(sent)} faces of {counts.get('people', 0)} people from "
                                            f"{counts.get('photos', 0)} photos"}
        except Exception as exc:
            log.exception("Faces from photos failed")
            pending["result"] = {"ok": False, "message": f"Camera: {type(exc).__name__}: {exc}"[:400]}
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _run_seed_vectors(self, key: str, payload: dict, pending: dict) -> None:
        """The face vectors of the review samples of a seed uploaded on the server, which has no face models."""
        import shutil

        folder = self.data_dir / "seed-vectors" / key
        try:
            if self.seed_vectors is None:
                raise ValueError("A seed with review samples needs a newer app on the camera computer.")
            status, data = self._send("GET", "/api/edge/files/" + urllib.parse.quote(payload["image"]), b"",
                                      "application/octet-stream")
            if status != 200:
                raise ConnectionError(f"the seed answered HTTP {status}")
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder / "seed.zip"
            archive.write_bytes(data)
            del data
            pending["result"] = self.seed_vectors(archive, folder / "seed")
        except Exception as exc:
            log.exception("Reading the faces of a seed failed")
            pending["result"] = {"ok": False, "message": f"Camera: {type(exc).__name__}: {exc}"[:400]}
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _moment(self, payload: dict) -> dict:
        """The pictures recorded (recorder.py) for a time span, sent to the server for its Moments page."""
        from .recorder import MAX_SPAN_S, recorded

        try:
            day = dt.date.fromisoformat(payload["day"])
            start, end = (dt.time.fromisoformat(payload[k]) for k in ("start", "end"))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "message": "bad moment"}
        span = (dt.datetime.combine(day, end) - dt.datetime.combine(day, start)).total_seconds()
        if not 0 <= span <= MAX_SPAN_S:
            return {"ok": False, "message": "moment too long"}
        frames = recorded(self.data_dir, day, start, end)
        for t, path in frames:
            rel = f"crops/{day.isoformat()}/rec_{t.replace(':', '')}.jpg"
            status, _ = self._send("PUT", "/api/edge/photos/" + urllib.parse.quote(rel), path.read_bytes(), "image/jpeg")
            if status != 200:
                raise ConnectionError(f"recorded picture upload answered HTTP {status}")
        return {"ok": True, "day": day.isoformat(), "start": payload["start"], "end": payload["end"], "frames": len(frames),
                "message": f"{len(frames)} recorded pictures" if frames else "nothing recorded then (only the camera hours of the last days are kept)"}

    def _add_uploaded(self, payload: dict) -> dict:
        status, data = self._send("GET", "/api/edge/files/" + urllib.parse.quote(payload["image"]), b"", "application/octet-stream")
        if status != 200:
            raise ConnectionError(f"file answered HTTP {status}")
        try:
            if payload.get("voice"):
                if self.add_voice is None:
                    raise ValueError("Voice installation is not available on this camera computer.")
                self.add_voice(payload["voice"], data)
                return {"ok": True, "message": f"Voice {payload['voice']} installed"}
            added = self.add_photo(payload["employee_id"], data, payload.get("source", "upload"))
            return {"ok": added.ok, "message": added.message, "warnings": list(added.warnings)}
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc), "warnings": []}

    def _loop(self) -> None:
        backoff, last_beat, last_state = IDLE_S, 0.0, 0.0
        while True:
            try:
                if time.monotonic() - last_beat >= HEARTBEAT_S:
                    self.heartbeat()
                    last_beat = time.monotonic()
                sent = self.deliver_batch()
                if not sent and time.monotonic() - last_state >= (FOLLOW_S if self.following else STATE_S):
                    (self.follow if self.following else self.sync_state)()
                    self.send_upcoming()
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
