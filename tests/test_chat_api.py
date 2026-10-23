"""The late message through the Google Chat API, signed in as a user, with its picture attached."""
import datetime as dt
import json
import urllib.parse

import pytest

from attendance.integrations import chat_api
from attendance.integrations.chat_api import ChatApi, ChatApiError, read_client, space_of
from attendance.web import create_app
from conftest import DAY, at

CLIENT = json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "s3cret",
                                   "project_id": "offspring-check-in", "redirect_uris": ["http://localhost"]}})
WEBHOOK = "https://chat.googleapis.com/v1/spaces/AAQAkVxjsFc/messages?key=k&token=t"


class Google:
    """Pretends to be Google's sign-in and Chat servers."""

    def __init__(self):
        self.calls = []
        self.chat_status = 200
        self.refresh_ok = True
        self.messages = []  # posted successfully

    def __call__(self, method, url, body=None, headers=None, timeout=30):
        self.calls.append((method, url, body, headers or {}))
        if url == chat_api.TOKEN_URL:
            form = urllib.parse.parse_qs(body.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"][0] and form["client_secret"] == ["s3cret"]
                return 200, json.dumps({"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
                                        "scope": chat_api.SCOPE}).encode()
            if not self.refresh_ok:
                return 400, b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'
            return 200, json.dumps({"access_token": "at2", "expires_in": 3600}).encode()
        if url == chat_api.REVOKE_URL:
            return 200, b""
        if self.chat_status != 200:
            return self.chat_status, json.dumps({"error": {"message": "The caller does not have permission"}}).encode()
        if "/attachments:upload" in url:
            return 200, b'{"attachmentDataRef": {"resourceName": "r1", "attachmentUploadToken": "u1"}}'
        self.messages.append(json.loads(body))
        return 200, b'{"name": "spaces/AAQAkVxjsFc/messages/m1"}'

    def posted(self):
        return self.messages


def signed_in(store, google):
    api = ChatApi(store, lambda: space_of(WEBHOOK), http=google)
    api.set_client(CLIENT)
    url = api.start("http://127.0.0.1:8000/settings/google/callback")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["scope"] == [chat_api.SCOPE] and q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    api.finish("code1", q["state"][0])
    return api


def test_client_file_and_space():
    assert read_client(CLIENT)["client_id"].startswith("123-")
    with pytest.raises(ValueError, match="Desktop app"):
        read_client(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(ValueError):
        read_client("not json")
    assert space_of(WEBHOOK) == "spaces/AAQAkVxjsFc" and space_of("") is None


def test_sign_in_then_post_with_the_picture_attached(core):
    app, clock, _ = core
    google = Google()
    api = signed_in(app.store, google)
    assert api.ready and api.status()["connected"]
    assert api.send("Morning, coming late today: <users/1> (09:12)", b"\xff\xd8jpeg") == "spaces/AAQAkVxjsFc/messages/m1"
    upload = next(c for c in google.calls if "/attachments:upload" in c[1])
    assert upload[1].startswith("https://chat.googleapis.com/upload/v1/spaces/AAQAkVxjsFc/") and b"\xff\xd8jpeg" in upload[2]
    assert upload[3]["Authorization"] == "Bearer at1"
    assert google.posted() == [{"text": "Morning, coming late today: <users/1> (09:12)",
                                "attachment": [{"attachmentDataRef": {"resourceName": "r1", "attachmentUploadToken": "u1"}}]}]


def test_a_sign_in_not_started_here_is_refused(core):
    app, clock, _ = core
    api = ChatApi(app.store, lambda: "spaces/X", http=Google())
    api.set_client(CLIENT)
    api.start("http://127.0.0.1:8000/cb")
    with pytest.raises(ValueError, match="not started here"):
        api.finish("code1", "forged")
    assert not api.ready


def test_an_expired_access_token_is_refreshed_and_a_revoked_sign_in_is_permanent(core):
    app, clock, _ = core
    google = Google()
    api = signed_in(app.store, google)
    token = json.loads(app.store.get(chat_api.TOKEN_KEY))
    token["expires_at"] = 0
    app.store.put(chat_api.TOKEN_KEY, json.dumps(token))
    api.send("hello")
    assert google.calls[-1][3]["Authorization"] == "Bearer at2"
    google.refresh_ok = False
    token["expires_at"] = 0
    token["access_token"] = "old"
    app.store.put(chat_api.TOKEN_KEY, json.dumps(token))
    with pytest.raises(ChatApiError) as exc:
        api.send("hello")
    assert exc.value.permanent and "expired or revoked" in api.status()["error"]


def late_day(app, clock):
    app.cfg.google_chat.webhook_url = WEBHOOK
    clock.t = at(10, 30)
    app.service.on_identified("B", at(10, 15), 0.8, None)


def test_the_late_message_goes_through_the_api_and_alerts_through_the_webhook(core):
    app, clock, sent = core
    google = Google()
    app.chat.api = signed_in(app.store, google)
    late_day(app, clock)
    app.service.send_late_message(DAY)
    app.service.alert("⚠️ The camera is not working")
    app.chat.flush(clock.t)
    assert [p["text"].startswith("Morning, coming late today:") for p in google.posted()] == [True]
    assert any("camera is not working" in m for m in sent)  # the webhook
    assert not any(m.startswith("Morning") for m in sent)


def test_without_access_the_webhook_posts_it_with_a_link_and_says_why(core):
    app, clock, sent = core
    google = Google()
    app.chat.api = signed_in(app.store, google)
    google.chat_status = 403
    late_day(app, clock)
    app.service.send_late_message(DAY)
    app.chat.flush(clock.t)
    assert any("could not be posted with its picture" in m and "permission" in m for m in sent)
    assert any(m.startswith("Morning, coming late today:") for m in sent)
    assert app.store.outbox() == []


def test_a_passing_outage_is_retried_not_sent_twice(core):
    app, clock, sent = core
    google = Google()
    app.chat.api = signed_in(app.store, google)
    google.chat_status = 503
    late_day(app, clock)
    app.service.send_late_message(DAY)
    app.chat.flush(clock.t)
    assert len(app.store.outbox()) == 1 and not any(m.startswith("Morning") for m in sent)
    google.chat_status = 200
    app.chat.flush(clock.t + dt.timedelta(minutes=1))
    assert len(google.posted()) == 1 and app.store.outbox() == []


def test_settings_page_upload_connect_and_paste(core):
    app, clock, _ = core
    google = Google()
    app.chat.api = ChatApi(app.store, lambda: space_of(WEBHOOK), http=google)
    web = create_app(app)
    c = web.test_client()
    page = c.get("/settings").get_data(as_text=True)
    assert "Late message with its picture attached" in page and "Upload JSON" in page
    import io
    r = c.post("/settings/google/client", data={"client": (io.BytesIO(CLIENT.encode()), "client_secret.json")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and app.chat.api.status()["client"]
    r = c.post("/settings/google/connect")
    assert r.status_code == 302 and r.headers["Location"].startswith(chat_api.AUTH_URL)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query)["state"][0]
    address = f"http://127.0.0.1:8000/settings/google/callback?state={state}&code=code1&scope=x"
    c.post("/settings/google/paste", data={"address": address})
    assert app.chat.api.ready
    assert "s3cret" not in c.get("/settings").get_data(as_text=True) and "rt1" not in c.get("/settings").get_data(as_text=True)
    c.post("/settings/google/disconnect")
    assert not app.chat.api.ready
