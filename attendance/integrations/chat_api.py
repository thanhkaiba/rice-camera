"""Google Chat API with a user's sign-in, for the late message with its picture attached.

An incoming webhook cannot attach a file: the picture could only be linked, and Google Chat shows
a linked picture only when this computer is reachable over https from the internet. The Chat API
can upload the picture and attach it, but only on behalf of a user (scope chat.messages.create),
so the late message is posted as that user (e.g. admin@company), who must be a member of the space.

Setup: an OAuth client of type "Desktop app" in a Google Cloud project with the Chat API enabled
and the Chat app configured; its JSON is uploaded on the Settings page, then the admin signs in
once (Connect). Google sends the browser back to http://127.0.0.1:<port>/...; when the admin page
is used from another computer that address does not open there, and the address it shows can be
pasted on the Settings page instead.

The client secret and the refresh token stay in the database (kv), never in config.yaml, the
configuration export or the diagnostics. Alerts keep using the webhook.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/chat.messages.create"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://chat.googleapis.com"
CLIENT_KEY = "google_oauth_client"  # kv: {"client_id", "client_secret", "project_id"}
TOKEN_KEY = "google_oauth_token"  # kv: {"refresh_token", "access_token", "expires_at", "connected_at"}
PENDING_KEY = "google_oauth_pending"  # kv: {"state", "verifier", "redirect_uri", "at"} while signing in
PENDING_S = 1800  # a sign-in started longer ago than this is not finished any more
CALLBACK = "/settings/google/callback"


class ChatApiError(Exception):
    """`permanent`: retrying will not help (signed out, no access to the space, bad request)."""

    def __init__(self, message: str, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


def space_of(webhook_url: str) -> str | None:
    """spaces/AAA from a webhook address https://chat.googleapis.com/v1/spaces/AAA/messages?key=…"""
    m = re.search(r"/v1/(spaces/[^/?#]+)/messages", webhook_url or "")
    return m.group(1) if m else None


def read_client(text: str) -> dict:
    """The OAuth client from the JSON Google Cloud gives for a Desktop app; ValueError otherwise."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("this is not a JSON file") from None
    if not isinstance(data, dict):
        raise ValueError("this is not an OAuth client file")
    if "web" in data:
        raise ValueError('this OAuth client is of type "Web application"; create one of type "Desktop app"')
    c = data.get("installed")
    if not isinstance(c, dict) or not c.get("client_id") or not c.get("client_secret"):
        raise ValueError("this is not an OAuth client file (Credentials → OAuth client ID → Desktop app → Download JSON)")
    return {"client_id": c["client_id"], "client_secret": c["client_secret"], "project_id": c.get("project_id", "")}


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
          timeout: float = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class ChatApi:
    def __init__(self, store, space: Callable[[], str | None], http: Callable = _http,
                 clock: Callable[[], float] = time.time):
        """`space`: the space to post in (spaces/AAA), read when needed."""
        self.store, self._space, self._http, self._clock = store, space, http, clock
        self._lock = threading.Lock()  # one token refresh at a time

    # -- stored state ---------------------------------------------------------------------------

    def _get(self, key: str) -> dict:
        try:
            return json.loads(self.store.get(key) or "{}")
        except ValueError:
            return {}

    def _put(self, key: str, value: dict | None) -> None:
        self.store.put(key, json.dumps(value) if value else "")

    @property
    def ready(self) -> bool:
        """Signed in, with somewhere to post."""
        return bool(self._get(CLIENT_KEY).get("client_id") and self._get(TOKEN_KEY).get("refresh_token") and self._space())

    def status(self) -> dict:
        client, token = self._get(CLIENT_KEY), self._get(TOKEN_KEY)
        return {"client": bool(client.get("client_id")), "project": client.get("project_id", ""),
                "connected": bool(token.get("refresh_token")), "connected_at": token.get("connected_at"),
                "space": self._space(), "error": token.get("error"), "ready": self.ready}

    def set_client(self, text: str) -> dict:
        """Keep the OAuth client from its JSON file; a different client signs out."""
        client = read_client(text)
        if client["client_id"] != self._get(CLIENT_KEY).get("client_id"):
            self._put(TOKEN_KEY, None)
        self._put(CLIENT_KEY, client)
        return client

    # -- signing in -----------------------------------------------------------------------------

    def start(self, redirect_uri: str) -> str:
        """The Google sign-in address; Google comes back to `redirect_uri` with a code (PKCE)."""
        client = self._get(CLIENT_KEY)
        if not client.get("client_id"):
            raise ValueError("upload the OAuth client JSON first")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        self._put(PENDING_KEY, {"state": state, "verifier": verifier, "redirect_uri": redirect_uri, "at": self._clock()})
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def finish(self, code: str, state: str) -> None:
        """Exchange the code Google sent back for a refresh token."""
        pending, client = self._get(PENDING_KEY), self._get(CLIENT_KEY)
        if not pending or not secrets.compare_digest(pending.get("state", ""), state or ""):
            raise ValueError("this sign-in was not started here, or was started again since; press Connect again")
        if self._clock() - pending.get("at", 0) > PENDING_S:
            raise ValueError("this sign-in took too long; press Connect again")
        if not code:
            raise ValueError("Google sent no code back (was access denied?)")
        token = self._token({"grant_type": "authorization_code", "code": code, "client_id": client["client_id"],
                             "client_secret": client["client_secret"], "redirect_uri": pending["redirect_uri"],
                             "code_verifier": pending["verifier"]})
        if not token.get("refresh_token"):
            raise ValueError("Google gave no lasting sign-in; press Connect again")
        if SCOPE not in token.get("scope", SCOPE).split():
            raise ValueError("the permission to post messages was not granted; tick it on Google's page")
        self._put(PENDING_KEY, None)
        self._put(TOKEN_KEY, {"refresh_token": token["refresh_token"], "access_token": token.get("access_token"),
                              "expires_at": self._clock() + token.get("expires_in", 0) - 60,
                              "connected_at": time.strftime("%Y-%m-%d %H:%M")})

    def finish_from_address(self, address: str) -> None:
        """Finish with the address the browser landed on (pasted when 127.0.0.1 did not open)."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(address.strip()).query)
        if "error" in q:
            raise ValueError(f"Google says: {q['error'][0]}")
        self.finish(q.get("code", [""])[0], q.get("state", [""])[0])

    def disconnect(self) -> None:
        token = self._get(TOKEN_KEY)
        if token.get("refresh_token"):
            try:
                self._http("POST", REVOKE_URL, urllib.parse.urlencode({"token": token["refresh_token"]}).encode(),
                           {"Content-Type": "application/x-www-form-urlencoded"}, 10)
            except OSError as exc:
                log.warning("Could not revoke the Google sign-in: %s", exc)
        self._put(TOKEN_KEY, None)

    def _token(self, form: dict) -> dict:
        try:
            status, body = self._http("POST", TOKEN_URL, urllib.parse.urlencode(form).encode(),
                                      {"Content-Type": "application/x-www-form-urlencoded"}, 20)
        except OSError as exc:
            raise ChatApiError(f"Google sign-in server unreachable: {exc}", permanent=False) from exc
        data = json.loads(body or b"{}") if body[:1] in (b"{", b"[") else {}
        if status >= 500:
            raise ChatApiError(f"Google sign-in server error {status}", permanent=False)
        if status != 200:
            reason = data.get("error_description") or data.get("error") or f"HTTP {status}"
            raise ChatApiError(f"Google sign-in refused: {reason}", permanent=True)
        return data

    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            token, client = self._get(TOKEN_KEY), self._get(CLIENT_KEY)
            if not token.get("refresh_token") or not client.get("client_id"):
                raise ChatApiError("not signed in to Google", permanent=True)
            if not force and token.get("access_token") and self._clock() < token.get("expires_at", 0):
                return token["access_token"]
            try:
                fresh = self._token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                                     "client_id": client["client_id"], "client_secret": client["client_secret"]})
            except ChatApiError as exc:
                if exc.permanent:  # revoked, expired, or the client was deleted: sign in again
                    token["error"] = str(exc)
                    self._put(TOKEN_KEY, token)
                raise
            token.update(access_token=fresh["access_token"], expires_at=self._clock() + fresh.get("expires_in", 0) - 60)
            token.pop("error", None)
            self._put(TOKEN_KEY, token)
            return token["access_token"]

    # -- posting --------------------------------------------------------------------------------

    def _call(self, method: str, url: str, body: bytes, content_type: str) -> dict:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._access_token(force=attempt == 1)}", "Content-Type": content_type}
            try:
                status, data = self._http(method, url, body, headers)
            except OSError as exc:
                raise ChatApiError(f"Google Chat unreachable: {exc}", permanent=False) from exc
            if status == 401 and attempt == 0:
                continue  # the access token expired early: refresh once
            result = json.loads(data or b"{}") if data[:1] == b"{" else {}
            if status == 200:
                return result
            message = (result.get("error") or {}).get("message") or f"HTTP {status}"
            raise ChatApiError(f"Google Chat: {message}", permanent=status < 500 and status != 429)
        raise ChatApiError("Google Chat refused the sign-in", permanent=True)

    def upload(self, space: str, data: bytes, filename: str, mime: str = "image/jpeg") -> dict:
        """Upload a file to the space; returns the attachmentDataRef to attach to a message."""
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps({'filename': filename})}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        result = self._call("POST", f"{API}/upload/v1/{space}/attachments:upload?uploadType=multipart", body,
                            f"multipart/related; boundary={boundary}")
        return result["attachmentDataRef"]

    def send(self, text: str, image: bytes | None = None, filename: str = "late.jpg") -> str:
        """Post `text` in the space as the signed-in user, with `image` attached; returns the message name."""
        space = self._space()
        if not space:
            raise ChatApiError("no Google Chat space (set the webhook address in Settings)", permanent=True)
        message: dict = {"text": text}
        if image:
            message["attachment"] = [{"attachmentDataRef": self.upload(space, image, filename)}]
        result = self._call("POST", f"{API}/v1/{space}/messages", json.dumps(message).encode(),
                            "application/json; charset=UTF-8")
        return result.get("name", "")
