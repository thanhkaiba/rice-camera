"""Google Chat incoming webhook with a persistent outbox, so messages survive network loss and restarts.

Delivery can run on its own thread (`start_background`): a slow or unreachable webhook then
never holds up the camera loop, which would otherwise miss people walking past.

A message may also carry an "api" part (the late message with its picture): when the Chat API
is signed in (chat_api.py) it is posted that way, the picture attached; when that fails for good
(signed out, no access to the space) the webhook version, with a link, is posted instead, and the
admin is told why.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from ..store import Store
from .chat_api import ChatApi, ChatApiError

log = logging.getLogger(__name__)

# Messages sharing a threadKey are grouped into one thread; we use one thread per day.
THREAD_OPTION = "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
MAX_AGE = dt.timedelta(hours=24)
RETRY_EVERY_S = 30.0  # the background worker retries undelivered messages this often


def _http_post(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


class GoogleChat:
    def __init__(self, webhook_url: str | Callable[[], str], store: Store,
                 post: Callable[[str, dict], None] = _http_post):
        """`webhook_url` may be a function so a URL changed in the settings is used right away."""
        self._url = webhook_url if callable(webhook_url) else (lambda: webhook_url)
        self.store = store
        self._post = post
        self._flushing = threading.Lock()  # one delivery pass at a time
        self._wake: threading.Event | None = None  # set once the background worker runs
        self.api: ChatApi | None = None  # set by the app: the Chat API, for messages with an "api" part
        self.data_dir: Path | None = None  # where an "api" part's picture is

    def start_background(self) -> None:
        """Deliver from a worker thread instead of the caller's thread."""
        if self._wake is None:
            self._wake = threading.Event()
            threading.Thread(target=self._worker, name="google-chat", daemon=True).start()

    def _worker(self) -> None:
        while True:
            self._wake.wait(RETRY_EVERY_S)
            self._wake.clear()
            try:
                self.flush(dt.datetime.now(dt.timezone.utc))
            except Exception:
                log.exception("Google Chat delivery crashed")

    @property
    def webhook_url(self) -> str:
        return (self._url() or "").strip()

    @property
    def api_ready(self) -> bool:
        return self.api is not None and self.api.ready

    def send(self, text: str, thread_key: str | None, now: dt.datetime, cards: list[dict] | None = None,
             api: dict | None = None) -> None:
        """`cards` are Google Chat cardsV2 shown under the text, e.g. a picture. `api`: {"text", "image"
        (under the data folder)} to post through the Chat API instead, when it is signed in."""
        log.info("Google Chat: %s", text.replace("\n", " | "))
        if not self.webhook_url and not (api and self.api_ready):
            return
        payload: dict = {"text": text}
        if cards:
            payload["cardsV2"] = cards
        if thread_key:
            payload["thread"] = {"threadKey": thread_key}
        if api:
            payload["api"] = api
        self.store.enqueue(payload, now)
        self.kick(now)

    def kick(self, now: dt.datetime) -> None:
        """Deliver what is queued: on the worker thread when it runs, otherwise right here."""
        if self._wake is not None:
            self._wake.set()
        else:
            self.flush(now)

    def send_now(self, text: str, url: str | None = None) -> None:
        """Send directly, raising on failure; used to test the webhook."""
        self._post(url or self.webhook_url, {"text": text})

    def _picture(self, rel: str | None) -> bytes | None:
        path = self.data_dir / rel if rel and self.data_dir else None
        return path.read_bytes() if path and path.exists() else None

    def _post_api(self, item_id: int, payload: dict) -> bool | None:
        """Post through the Chat API: True when done, False to retry later, None to use the webhook."""
        part = payload["api"]
        try:
            name = self.api.send(part["text"], self._picture(part.get("image")))
        except ChatApiError as exc:
            if not exc.permanent:
                log.warning("Google Chat API delivery failed, will retry: %s", exc)
                self.store.outbox_failed(item_id)
                return False
            log.error("Google Chat API cannot post (%s); posting with the webhook instead", exc)
            if self.webhook_url:
                self._post(self.webhook_url, {"text": f"⚠️ The late message could not be posted with its picture: {exc}. "
                                                      "Sign in again under Settings → Google Chat."})
            return None
        log.info("Posted with the Google Chat API: %s", name)
        self.store.outbox_done(item_id)
        return True

    def flush(self, now: dt.datetime) -> None:
        """Deliver queued messages in order; stop at the first failure and retry on the next call."""
        if not self.webhook_url and not self.api_ready:
            return
        with self._flushing:
            for item_id, payload, created_at in self.store.outbox():
                if now - created_at > MAX_AGE:
                    log.error("Dropping undelivered Google Chat message older than %s: %s", MAX_AGE, payload)
                    self.store.outbox_done(item_id)
                    continue
                if "api" in payload and self.api_ready:
                    try:
                        done = self._post_api(item_id, payload)
                    except Exception as exc:  # the warning itself could not be posted
                        log.warning("Google Chat delivery failed, will retry: %s", exc)
                        self.store.outbox_failed(item_id)
                        return
                    if done is False:
                        return
                    if done:
                        continue
                payload = {k: v for k, v in payload.items() if k != "api"}
                if not self.webhook_url:
                    log.error("Dropping a Google Chat message: no webhook to post it with: %s", payload)
                    self.store.outbox_done(item_id)
                    continue
                url = self.webhook_url
                if "thread" in payload:
                    url += ("&" if "?" in url else "?") + THREAD_OPTION
                try:
                    self._post(url, payload)
                except Exception as exc:  # network errors, HTTP 4xx/5xx
                    log.warning("Google Chat delivery failed, will retry: %s", exc)
                    self.store.outbox_failed(item_id)
                    return
                self.store.outbox_done(item_id)
