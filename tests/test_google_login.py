import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from attendance.store import Store
from attendance.web import create_app


def set_role(store, employee_id, role):
    store.web_set_role(employee_id, role)


def configured(store):
    set_role(store, "A", "admin")
    store.web_save_config({"enabled": True, "client_id": "client.apps.googleusercontent.com", "domain": "example.com"})


def test_employee_portal_defaults_and_persistence(core):
    app, _, _ = core
    store = app.store
    assert store.web_role("A") == "viewer"
    employee_id = store.add_employee("new@example.com", "New")
    assert store.web_role(employee_id) == "viewer"
    set_role(store, "A", "admin")
    reopened = Store(app.data_dir / "attendance.db")
    assert reopened.web_role("A") == "admin"
    reopened.close()
    with pytest.raises(ValueError, match="portal role"):
        set_role(store, "A", "owner")


def test_last_google_admin_is_protected_transactionally(core):
    app, _, _ = core
    store = app.store
    configured(store)
    with pytest.raises(ValueError, match="last Google admin"):
        set_role(store, "A", "viewer")
    with pytest.raises(ValueError, match="last Google admin"):
        store.update_employee("A", "Changed", False)
    assert store.employee("A").name == "Alice" and store.employee("A").active
    with pytest.raises(ValueError, match="last Google admin"):
        store.delete_employee("A")
    set_role(store, "B", "admin")
    set_role(store, "A", "viewer")
    assert store.web_role("A") == "viewer"
    assert store.web_role("B") == "admin"


def test_identity_binding_resets_on_lifecycle_changes(core):
    app, _, _ = core
    store = app.store
    first = store.web_bind("B", "bob-google-id", expected_email="bob@example.com")
    assert first["subject"] == "bob-google-id"
    with pytest.raises(ValueError, match="Google account"):
        store.web_bind("B", "different-google-id", expected_email="bob@example.com")
    store.update_employee("B", "Bob", False)
    store.update_employee("B", "Bob", True)
    second = store.web_bind("B", "bob-google-id", expected_email="bob@example.com")
    assert second["version"] != first["version"]
    store.update_employee("B", "Bob", True, email="bob2@example.com")
    third = store.web_bind("B", "new-google-id", expected_email="bob2@example.com")
    assert third["version"] != second["version"]
    store.delete_employee("B")
    assert not store._all("SELECT * FROM web_accounts WHERE employee_id = 'B'")


def test_google_sign_in_started_long_ago_expires(portal, monkeypatch):
    from attendance.web import auth
    client, _ = portal
    nonce, token = start(client)
    monkeypatch.setattr(auth, "verify", lambda *a: claims_for(nonce))
    later = time.time() + auth.STATE_SECONDS + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    expired = client.post("/auth/google", data={"credential": "token", "csrf_token": token})
    assert expired.status_code == 400 and "expired" in expired.get_data(as_text=True)
    assert client.get("/").status_code == 302


def test_portal_state_is_not_in_camera_sync_or_config_export(core):
    from attendance.core import transfer

    app, _, _ = core
    configured(app.store)
    app.store.web_bind("A", "private-google-subject", expected_email="alice@example.com")
    synced = json.dumps(app.store.edge_employee_rows())
    exported = json.dumps(transfer.export(app.cfg, app.store, app.cfg.base_dir / "config.yaml"))
    for text in (synced, exported):
        assert "private-google-subject" not in text
        assert "portal_role" not in text and "web_accounts" not in text


@pytest.fixture
def portal(core):
    core_app, _, _ = core
    core_app.cfg.web.password = "recovery-password"
    configured(core_app.store)
    app = create_app(core_app)
    app.testing = True
    return app.test_client(), core_app


def csrf(client):
    client.get("/login")
    with client.session_transaction() as session:
        return session["csrf_token"]


def start(client, next="/"):
    """Press "Sign in with Google" and come back: the nonce sent to Google and the return page's CSRF token."""
    client.get("/auth/google", query_string={"next": next})
    client.get("/auth/google/callback")  # Google adds "#id_token=…", which never reaches the server
    with client.session_transaction() as session:
        return session["google_login"]["nonce"], session["csrf_token"]


def claims_for(nonce, email="bob@example.com", subject="google-bob"):
    return {"email": email, "sub": subject, "email_verified": True, "hd": "example.com", "nonce": nonce}


def google_login(client, monkeypatch, email="bob@example.com", subject="google-bob", **overrides):
    """Google sends back an ID token (checked by auth.verify) and the return page posts it."""
    from attendance.web import auth

    nonce, token = start(client)
    monkeypatch.setattr(auth, "verify", lambda *a: {**claims_for(nonce, email, subject), **overrides})
    return client.post("/auth/google", data={"credential": "token", "csrf_token": token})


def test_google_viewer_is_enforced_on_direct_urls(portal, monkeypatch):
    client, core_app = portal
    assert client.get("/").status_code == 302
    assert client.get("/", headers={"Accept": "application/json"}).status_code == 401
    assert google_login(client, monkeypatch).status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/settings").status_code == 403
    assert client.get("/employees/B").status_code == 403
    assert client.post("/employees/B", data={"portal_role": "admin"}).status_code == 403
    assert core_app.store.web_role("B") == "viewer"


@pytest.mark.parametrize("claims", [
    {"email": "unknown@example.com"}, {"email_verified": False}, {"nonce": "wrong"},
    {"sub": ""}, {"hd": "", "email": "bob@example.com"}, {"hd": "other.example"}, {"email_verified": "true"},
])
def test_google_rejects_untrusted_claims(portal, monkeypatch, claims):
    client, _ = portal
    assert google_login(client, monkeypatch, **claims).status_code in (400, 403)
    assert client.get("/").status_code == 302


def test_google_rejects_inactive_employee_and_wrong_subject(portal, monkeypatch):
    client, core_app = portal
    core_app.store.update_employee("B", "Bob", False)
    assert google_login(client, monkeypatch).status_code == 403
    core_app.store.update_employee("B", "Bob", True)
    core_app.store.web_bind("B", "original-subject", expected_email="bob@example.com")
    assert google_login(client, monkeypatch).status_code == 403


def test_without_a_company_domain_any_workspace_or_gmail_may_sign_in(portal, monkeypatch):
    client, core_app = portal
    core_app.store.web_save_config({"enabled": True, "client_id": "client.apps.googleusercontent.com", "domain": ""})
    assert google_login(client, monkeypatch, hd="other.example").status_code == 302
    assert google_login(client.application.test_client(), monkeypatch, hd=None).status_code == 403  # no Workspace, not Gmail


def test_google_redirect_carries_this_browsers_nonce_and_comes_back_here(portal):
    client, core_app = portal
    assert 'href="/auth/google?next=/report"' in client.get("/login?next=/report").get_data(as_text=True)
    google = urlsplit(client.get("/auth/google?next=/report").location)
    args = {k: v[0] for k, v in parse_qs(google.query).items()}
    with client.session_transaction() as session:
        pending = session["google_login"]
    assert google.netloc == "accounts.google.com" and "client_secret" not in args
    assert (args["client_id"], args["response_type"], args["hd"]) == ("client.apps.googleusercontent.com", "id_token", "example.com")
    assert args["nonce"] == pending["nonce"] and pending["next"] == "/report"
    assert args["redirect_uri"] == "http://localhost/auth/google/callback"
    core_app.cfg.web.public_url = "https://localhost"  # HTTPS ends at a proxy in front of the server
    assert "redirect_uri=https%3A%2F%2Flocalhost%2Fauth%2Fgoogle%2Fcallback" in client.get("/auth/google").location
    page = client.get("/auth/google/callback").get_data(as_text=True)
    assert 'action="/auth/google"' in page and "id_token" in page and "history.replaceState" in page
    # no-referrer makes the browser POST Origin: null, which the CSRF guard rejects.
    assert '<meta name="referrer" content="same-origin">' in page


def test_google_token_is_single_use_and_bound_to_this_browser(portal, monkeypatch):
    from attendance.web import auth
    client, _ = portal
    nonce, token = start(client, next="//evil.example")
    calls = []
    monkeypatch.setattr(auth, "verify", lambda client_id, credential: calls.append((client_id, credential)) or claims_for(nonce))
    assert client.post("/auth/google", data={"credential": "token"}).status_code == 403  # no CSRF token
    other = client.application.test_client()  # another browser: its own sign-in, its own nonce
    assert other.post("/auth/google", data={"credential": "token", "csrf_token": start(other)[1]}).status_code == 400
    cookie = client.get_cookie("session").value
    form = {"credential": "token", "csrf_token": token}
    result = client.post("/auth/google", data=form)
    assert result.status_code == 302 and result.location == "/"
    client.set_cookie("session", cookie)  # the old cookie and the same token, replayed
    assert client.post("/auth/google", data=form).status_code == 400
    assert calls == [("client.apps.googleusercontent.com", "token")] * 2


def test_cancelled_google_sign_in_returns_to_the_login_page(portal):
    client, _ = portal
    _, token = start(client)
    result = client.post("/auth/google", data={"error": "access_denied", "csrf_token": token})
    assert result.status_code == 400 and "cancelled" in result.get_data(as_text=True)
    assert client.get("/").status_code == 302


def test_google_session_lifecycle_and_csrf(portal, monkeypatch):
    client, core_app = portal
    assert google_login(client, monkeypatch, "alice@example.com", "google-alice").status_code == 302
    assert client.get("/settings").status_code == 200
    assert client.post("/settings/google-login", data={"enabled": "on"}).status_code == 403
    token = csrf(client)
    assert client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    assert client.get("/").status_code == 302
    google_login(client, monkeypatch)
    core_app.store.update_employee("B", "Bob", False)
    assert client.get("/").status_code == 302
    core_app.store.update_employee("B", "Bob", True)
    google_login(client, monkeypatch)
    core_app.store.update_employee("B", "Bob", True, email="bob2@example.com")
    assert client.get("/").status_code == 302


def test_live_role_changes_and_expiry(portal, monkeypatch):
    from attendance.web import auth
    client, core_app = portal
    google_login(client, monkeypatch)
    set_role(core_app.store, "B", "admin")
    assert client.get("/settings").status_code == 200
    set_role(core_app.store, "B", "viewer")
    assert client.get("/settings").status_code == 403
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 8 * 3600 + 1)
    assert client.get("/").status_code == 302


def test_recovery_password_login_and_existing_basic(portal):
    client, core_app = portal
    token = csrf(client)
    assert client.post("/login/password", data={"username": "admin", "password": "wrong", "csrf_token": token}).status_code == 401
    result = client.post("/login/password", data={"username": "admin", "password": "recovery-password", "csrf_token": token})
    assert result.status_code == 302 and client.get("/settings").status_code == 200
    core_app.cfg.web.password = "changed-password"
    assert client.get("/settings").status_code == 302
    basic = "Basic " + base64.b64encode(b"admin:changed-password").decode()
    assert client.get("/settings", headers={"Authorization": basic}).status_code == 200


def test_csrf_protection_covers_admin_forms_and_fetch(portal, monkeypatch):
    client, core_app = portal
    google_login(client, monkeypatch, "alice@example.com", "google-alice")
    token = csrf(client)
    data, url = {"portal_role": "admin"}, "/employees/B/portal-access"
    assert client.post(url, data=data).status_code == 403
    assert client.post(url, data=data, headers={"X-CSRF-Token": token, "Origin": "https://evil.example"}).status_code == 403
    assert client.post(url, data={**data, "csrf_token": token}).status_code == 302
    assert core_app.store.web_role("B") == "admin"
    assert any(user == "alice@example.com" and "viewer -> admin" in details
               for _, user, _, details in core_app.store.audit())


def test_employee_access_ui_and_last_admin_error(portal, monkeypatch):
    client, core_app = portal
    google_login(client, monkeypatch, "alice@example.com", "google-alice")
    page = client.get("/employees/B").get_data(as_text=True)
    assert 'name="portal_role"' in page and "Portal access" in page
    token = csrf(client)
    result = client.post("/employees/A/portal-access", data={"portal_role": "viewer", "csrf_token": token},
                         follow_redirects=True)
    assert "last Google admin" in result.get_data(as_text=True)
    assert core_app.store.web_role("A") == "admin"
    result = client.post("/employees/A/delete", data={"confirm": "alice@example.com", "csrf_token": token}, follow_redirects=True)
    assert "last Google admin" in result.get_data(as_text=True)
    assert core_app.store.references(core_app.library.model_id, "A")


def test_people_list_shows_portal_roles_and_links_to_the_role_editor(portal, monkeypatch):
    import re

    client, core_app = portal
    google_login(client, monkeypatch, "alice@example.com", "google-alice")
    def access_links():
        page = client.get("/employees").get_data(as_text=True)
        return {href: re.sub(r"<[^>]+>", "", label).strip()
                for href, label in re.findall(r'<a[^>]*href="([^"]*#portal-access)"[^>]*>(.*?)</a>', page, re.S)}
    links = access_links()
    assert links.get("/employees/A#portal-access") == "Admin"
    assert links.get("/employees/B#portal-access") == "Viewer"
    set_role(core_app.store, "B", "admin")
    assert access_links()["/employees/B#portal-access"] == "Admin"
    assert 'id="portal-access"' in client.get("/employees/B").get_data(as_text=True)


def test_google_configuration_can_be_updated(portal, monkeypatch):
    client, core_app = portal
    google_login(client, monkeypatch, "alice@example.com", "google-alice")
    page = client.get("/settings").get_data(as_text=True)
    assert 'name="domain"' in page and "client_secret" not in page
    assert "Authorized redirect URIs</b>: <code>http://localhost/auth/google/callback</code>" in page
    result = client.post("/settings/google-login", data={"enabled": "on", "client_id": "updated.apps.googleusercontent.com",
                                                         "domain": " @Company.Example ", "csrf_token": csrf(client)})
    assert result.status_code == 302
    config = core_app.store.web_config()
    assert (config["client_id"], config["domain"]) == ("updated.apps.googleusercontent.com", "company.example")


@pytest.mark.parametrize("form", [{"domain": "not a domain"}, {"domain": "https://example.com"}, {"client_id": "client-id"}])
def test_google_configuration_rejects_bad_values(portal, monkeypatch, form):
    client, core_app = portal
    google_login(client, monkeypatch, "alice@example.com", "google-alice")
    data = {"enabled": "on", "client_id": "new.apps.googleusercontent.com", "domain": "new.example", **form,
            "csrf_token": csrf(client)}
    result = client.post("/settings/google-login", data=data, follow_redirects=True)
    assert "Not saved" in result.get_data(as_text=True)
    assert core_app.store.web_config()["domain"] == "example.com"


def test_google_enable_prerequisites_and_disabled_compatibility(core):
    core_app, _, _ = core
    core_app.cfg.web.password = "recovery-password"
    app = create_app(core_app)
    app.testing = True
    client = app.test_client()
    token = csrf(client)
    client.post("/login/password", data={"username": "admin", "password": "recovery-password", "csrf_token": token})
    data = {"enabled": "on", "client_id": "client.apps.googleusercontent.com", "domain": "example.com",
            "csrf_token": csrf(client)}
    result = client.post("/settings/google-login", data=data, follow_redirects=True)
    assert "at least one active employee" in result.get_data(as_text=True)
    set_role(core_app.store, "A", "admin")
    assert client.post("/settings/google-login", data=data).status_code == 302
    assert core_app.store.web_config()["enabled"]
    client.post("/settings/google-login", data={**data, "enabled": ""})
    assert not core_app.store.web_config()["enabled"]
    assert client.get("/").status_code == 200


def test_google_upstream_failure_preserves_safe_details(portal, monkeypatch):
    from attendance.web import auth
    client, _ = portal
    nonce, token = start(client)
    def fail(*args):
        raise auth.GoogleLoginError("Could not reach Google. Try again or use password login.", 502)
    monkeypatch.setattr(auth, "verify", fail)
    response = client.post("/auth/google", data={"credential": "secret-credential", "csrf_token": token})
    assert response.status_code == 502 and "Could not reach Google" in response.get_data(as_text=True)
    assert "secret-credential" not in response.get_data(as_text=True)


def test_incomplete_google_configuration_never_allows_empty_basic_password(portal):
    client, core_app = portal
    core_app.cfg.web.password = ""
    token = "Basic " + base64.b64encode(b"admin:").decode()
    assert client.get("/settings", headers={"Authorization": token}).status_code == 401
    assert client.get("/").status_code == 302


def test_edge_employee_update_preserves_portal_account_and_invalidates_old_binding(core):
    app, _, _ = core
    set_role(app.store, "B", "admin")
    before = app.store.web_bind("B", "google-bob", expected_email="bob@example.com")
    rows = app.store.edge_employee_rows()
    bob = next(row for row in rows if row["id"] == "B")
    bob["email"] = "newbob@example.com"
    app.store.edge_follow_gallery(rows, app.store.edge_embedding_keys())
    after = app.store.web_account("B")
    assert after["role"] == "admin" and after["subject"] == ""
    assert after["version"] != before["version"]



def test_edge_sync_moves_or_swaps_emails_between_people(core):
    app, _, _ = core
    store = app.store
    rows = store.edge_employee_rows()
    a, b = (next(r for r in rows if r["id"] == i) for i in "AB")
    a["email"], b["email"] = b["email"], a["email"]  # swapped on the Mac
    a["base_username"], b["base_username"] = "shared", ""
    store.edge_replace_state([], {}, rows, None, [])
    assert (store.employee("A").email, store.employee("B").email) == (a["email"], b["email"])
    moved = [dict(r, id="A2") if r["id"] == "A" else r for r in rows]  # A recreated with a new id, same email
    store.edge_follow_gallery(moved, store.edge_embedding_keys())
    assert store.employee("A2").email == a["email"] and store.employee("A2").base_username == "shared"


def test_edge_sync_may_remove_the_last_google_admin(core):
    app, _, _ = core
    store = app.store
    configured(store)
    rows = store.edge_employee_rows()
    next(r for r in rows if r["id"] == "A")["active"] = 0  # the Mac stops tracking the only Google admin
    store.edge_replace_state([], {}, rows, None, [])
    assert not store.employee("A").active
    store.edge_follow_gallery([r for r in rows if r["id"] != "A"], store.edge_embedding_keys())
    assert store.employee("A") is None
    set_role(store, "B", "admin")
    with pytest.raises(ValueError, match="last Google admin"):  # portal edits are still guarded
        set_role(store, "B", "viewer")

def test_oauth_log_parameters_are_redacted():
    from attendance.core.diagnostics import redact
    log = 'GET /auth/google/callback?code=private-code&state=private-state HTTP/1.1 client_secret=private-secret id_token=private-token'
    redacted = redact(log)
    assert all(value not in redacted for value in ("private-code", "private-state", "private-secret", "private-token"))


@pytest.mark.parametrize("defect", ["none", "audience", "issuer", "expired", "signature", "future"])
def test_google_token_cryptographic_verification(monkeypatch, defect):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from google.auth import crypt, jwt
    from google.auth.transport import requests as google_requests
    from attendance.web import auth
    from types import SimpleNamespace
    import requests

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
    public_pem = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    now = int(auth.time.time())
    claims = {"iss": "https://accounts.google.com", "aud": "client-id", "sub": "google-bob", "iat": now,
              "exp": now + 3600, "email": "bob@example.com", "nonce": "nonce"}
    if defect == "audience":
        claims["aud"] = "other-client"
    elif defect == "issuer":
        claims["iss"] = "https://evil.example"
    elif defect == "expired":
        claims.update(iat=now - 7200, exp=now - 3600)
    elif defect == "future":
        claims["iat"] = now + 3600
    token = jwt.encode(crypt.RSASigner.from_string(private_pem, key_id="test-key"), claims).decode()
    if defect == "signature":
        pieces = token.split(".")
        pieces[2] = ("A" if pieces[2][0] != "A" else "B") + pieces[2][1:]
        token = ".".join(pieces)

    class HTTP:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def certificates(url, method="GET", timeout=None):
        assert timeout == 10 and url.startswith("https://www.googleapis.com/")
        return SimpleNamespace(status=200, data=json.dumps({"test-key": public_pem.decode()}).encode())

    monkeypatch.setattr(requests, "Session", HTTP)
    monkeypatch.setattr(google_requests, "Request", lambda session: certificates)
    if defect == "none":
        assert auth.verify("client-id", token)["sub"] == "google-bob"
    else:
        with pytest.raises(auth.GoogleLoginError, match="verification failed"):
            auth.verify("client-id", token)


def test_machine_api_credentials_still_required_with_google_enabled(portal, monkeypatch):
    from attendance.web import PUBLIC, edge_api
    client, core_app = portal
    PUBLIC.update(edge_api.ENDPOINTS)
    edge_api.register(client.application, core_app, hashlib.sha256(b"camera-token").hexdigest())
    assert client.post("/api/edge/heartbeat", json={}).status_code == 401
    assert client.post("/api/edge/heartbeat", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/edge/heartbeat", json={}, headers={"Authorization": "Bearer camera-token"}).status_code == 200
    google_login(client, monkeypatch)
    assert client.post("/api/edge/heartbeat", json={}).status_code == 401
    assert client.post("/api/edge/heartbeat", json={}, headers={"Authorization": "Bearer camera-token"}).status_code == 200


def test_session_csrf_on_upload_handlers_respects_existing_limits(portal, monkeypatch):
    client, _ = portal
    google_login(client, monkeypatch, "alice@example.com", "google-alice")
    token = csrf(client)
    endpoints = ["/settings/greeting/create", "/settings/greeting/voice", "/settings/greeting/preview-text", "/employees/seed"]
    for path in endpoints:
        assert client.post(path, data={"csrf_token": "wrong"}).status_code == 403
    response = client.post("/settings/greeting/preview-text", data={"text": "x" * 9000, "csrf_token": token})
    assert response.status_code == 413


def test_parallel_workers_share_the_same_session_secret(core, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    core_app, _, _ = core
    barrier = Barrier(2)
    original_get = core_app.store.get
    def concurrent_get(key):
        value = original_get(key)
        if key == "web_secret" and value is None:
            barrier.wait(timeout=10)
        return value
    monkeypatch.setattr(core_app.store, "get", concurrent_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        apps = list(pool.map(lambda _: create_app(core_app), range(2)))
    assert apps[0].secret_key == apps[1].secret_key == original_get("web_secret")


def test_email_changed_before_binding_cannot_bind_the_old_google_identity(core):
    app, _, _ = core
    app.store.update_employee("B", "Bob", True, email="newbob@example.com")
    with pytest.raises(ValueError, match="portal access"):
        app.store.web_bind("B", "old-google-bob", expected_email="bob@example.com")
    assert app.store.web_account("B") is None


def test_google_enable_cannot_race_with_last_admin_demotion(core):
    app, _, _ = core
    set_role(app.store, "A", "admin")
    other = Store(app.data_dir / "attendance.db")
    changed = []
    def demote_between_check_and_write(sql):
        if sql.startswith("INSERT INTO kv") and not changed:
            set_role(other, "A", "viewer")
            changed.append(True)
    app.store._db.set_trace_callback(demote_between_check_and_write)
    try:
        try:
            app.store.web_save_config({"enabled": True, "client_id": "client-id", "client_secret": "secret"})
        except ValueError:
            pass
        assert changed
        assert not app.store.web_config()["enabled"] or app.store.web_role("A") == "admin"
    finally:
        app.store._db.set_trace_callback(None)
        other.close()


@pytest.mark.parametrize("error", ["certificates_unavailable", "timeout"])
def test_google_network_failures_are_safe_upstream_errors(monkeypatch, error):
    import requests
    from google.auth.exceptions import TransportError
    from google.oauth2 import id_token
    from attendance.web import auth

    failure = (TransportError("private certificate network details") if error == "certificates_unavailable"
               else requests.Timeout("private network details"))
    def verify(*args, **kwargs):
        raise failure
    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    with pytest.raises(auth.GoogleLoginError) as raised:
        auth.verify("client-id", "token")
    assert raised.value.status == 502 and "private" not in str(raised.value)


@pytest.mark.parametrize("scheme,host,public_url,secure", [
    ("https", "portal.example", "http://localhost:8000", True),
    ("http", "portal.example", "https://portal.example", True),
    ("http", "portal.example", "http://localhost:8000", False),
    ("http", "192.168.1.5:8000", "https://portal.example", False),  # LAN access must keep its cookie
])
def test_session_cookie_security_matches_https_or_configured_proxy(core, scheme, host, public_url, secure):
    core_app, _, _ = core
    core_app.cfg.web.public_url = public_url
    app = create_app(core_app)
    client = app.test_client()
    response = client.get("/login", base_url=f"{scheme}://{host}")
    cookie = response.headers["Set-Cookie"]
    assert ("; Secure" in cookie) == secure
    assert "; HttpOnly" in cookie and "; SameSite=Lax" in cookie


def test_google_enabled_prevents_reset_or_import_removing_recovery_password(portal):
    from attendance.core import settings, transfer
    client, core_app = portal
    with pytest.raises(ValueError, match="recovery password"):
        settings.reset(core_app.cfg, core_app.store, "web.password")
    assert core_app.cfg.web.password == "recovery-password"
    with pytest.raises(ValueError, match="recovery password"):
        settings.save(core_app.cfg, core_app.store, {"web": {"password": ""}})
    path = core_app.cfg.base_dir / "config.yaml"
    exported = transfer.export(core_app.cfg, core_app.store, path)
    with pytest.raises(ValueError, match="recovery password"):
        transfer.import_(core_app.cfg, core_app.store, path, exported)
    assert not path.exists()
    assert core_app.cfg.web.password == "recovery-password"


def test_shadow_keeps_session_login_logout_and_basic_import_credentials(tmp_path, monkeypatch):
    from attendance import server
    from conftest import at
    password = "long-recovery-password"
    app, core_app = server.build(tmp_path, password, hashlib.sha256(b"camera-token").hexdigest(),
                                 shadow=True, now=lambda: at(10, 0))
    employee_id = core_app.store.add_employee("alice@example.com", "Alice")
    set_role(core_app.store, employee_id, "admin")
    core_app.store.web_save_config({"enabled": True, "client_id": "client.apps.googleusercontent.com", "domain": "example.com"})
    client = app.test_client()
    assert google_login(client, monkeypatch, "alice@example.com", "google-alice").status_code == 302
    assert client.get("/api/edge/import/manifest").status_code == 401
    basic = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
    assert client.get("/api/edge/import/manifest", headers={"Authorization": basic}).status_code == 200
    assert client.post("/logout", data={"csrf_token": csrf(client)}).status_code == 302
    assert client.post("/login/password", data={"username": "admin", "password": password,
                                               "csrf_token": csrf(client)}).status_code == 302
    assert client.post("/settings", data={"csrf_token": csrf(client)}).status_code == 403


def test_shadow_admin_can_change_only_portal_access(core, monkeypatch):
    from attendance import server
    from dataclasses import asdict

    original, clock, _ = core
    password = "shadow-recovery-password"
    app, shadow = server.build(original.cfg.base_dir, password, hashlib.sha256(b"camera-token").hexdigest(),
                                shadow=True, now=clock)
    configured(shadow.store)
    account = shadow.store.web_bind("B", "google-bob", expected_email="bob@example.com")
    client = app.test_client()
    client.post("/login/password", data={"username": "admin", "password": password, "csrf_token": csrf(client)})
    before = asdict(shadow.store.employee("B"))
    data = {"portal_role": "admin", "csrf_token": csrf(client), "name": "Must not change", "active": ""}
    assert client.post("/employees/B/portal-access", data=data).status_code == 302
    assert shadow.store.web_role("B") == "admin"
    assert asdict(shadow.store.employee("B")) == before
    bound = shadow.store.web_account("B")
    assert (bound["subject"], bound["version"]) == (account["subject"], account["version"])
    assert client.post("/employees/B", data=data).status_code == 403
    assert client.post("/settings", data=data).status_code == 403
    assert client.post("/employees/B/portal-access", data={"portal_role": "viewer"}).status_code == 403
    result = client.post("/employees/B/portal-access", data={**data, "portal_role": "owner"}, follow_redirects=True)
    assert "portal role must be admin or viewer" in result.get_data(as_text=True)
    assert shadow.store.web_role("B") == "admin"
    client.post("/employees/B/portal-access", data={**data, "portal_role": "viewer"})
    result = client.post("/employees/A/portal-access", data={**data, "portal_role": "viewer"}, follow_redirects=True)
    assert "last Google admin" in result.get_data(as_text=True)
    assert shadow.store.web_role("A") == "admin"
    assert any(action == "change portal access" for _, _, action, _ in shadow.store.audit())
    viewer = app.test_client()
    google_login(viewer, monkeypatch)
    assert viewer.post("/employees/B/portal-access", data={"portal_role": "admin", "csrf_token": csrf(viewer)}).status_code == 403
    assert shadow.store.web_role("B") == "viewer"



def test_server_env_admin_password_is_pinned_so_reset_and_import_are_allowed(tmp_path):
    from attendance import server
    from attendance.core import settings, transfer
    from conftest import at
    password = "long-recovery-password"
    _, core_app = server.build(tmp_path, password, hashlib.sha256(b"camera-token").hexdigest(),
                               shadow=False, now=lambda: at(10, 0))
    store = core_app.store
    set_role(store, store.add_employee("boss@example.com", "Boss"), "admin")
    store.web_save_config({"enabled": True, "client_id": "client.apps.googleusercontent.com", "domain": "example.com"})
    settings.reset(core_app.cfg, store, "web.password")  # config.yaml has none; the env password stays
    assert core_app.cfg.web.password == password
    with pytest.raises(ValueError, match="CHECKIN_ADMIN_PASSWORD"):
        settings.save(core_app.cfg, store, {"web": {"password": "something-else-long"}})
    path = tmp_path / "config.yaml"
    transfer.import_(core_app.cfg, store, path, transfer.export(core_app.cfg, store, path))
    assert core_app.cfg.web.password == password


def test_responses_that_write_no_cookie_skip_the_secure_lookup(portal, monkeypatch):
    client, core_app = portal
    client.get("/login")  # sets the session (CSRF token) once
    calls = []
    original = core_app.store.web_config
    monkeypatch.setattr(core_app.store, "web_config", lambda: calls.append(1) or original())
    client.get("/static/app.css")
    assert calls == []


def test_pages_viewed_without_a_session_set_no_cookie(portal):
    client, core_app = portal
    basic = base64.b64encode(b"admin:recovery-password").decode()
    page = client.get("/", headers={"Authorization": f"Basic {basic}"})
    assert page.status_code == 200 and "Set-Cookie" not in page.headers
    assert "Set-Cookie" in client.get("/login").headers  # the password form still gets its token


def test_a_server_without_the_google_library_refuses_the_token_with_a_clear_message(monkeypatch):
    import builtins

    from flask import Flask

    from attendance.web import auth

    real = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "requests" or name.startswith("google"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with Flask(__name__).app_context():
        with pytest.raises(auth.GoogleLoginError, match="not installed") as caught:
            auth.verify("client-id", "some-token")
    assert caught.value.status == 503


def test_viewer_sees_only_today_without_pictures_or_sources(portal, monkeypatch):
    """The read-only login: the Today page and its own account page; no other page, no camera picture, and not
    how an arrival was recorded (camera, review or by hand)."""
    client, core_app = portal
    store, service = core_app.store, core_app.service
    now = service.now()
    store.record_checkin(now.date(), "A", now, "camera", 0.61, "crops/2026-10-14/090000_000000_A_x.jpg")
    assert google_login(client, monkeypatch).status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Read-only" in page and ">Source<" not in page and 'class="src"' not in page
    assert "/media/" not in page and 'class="avatar empty"' in page  # everyone has the plain initial
    assert "const SIGHTINGS = {}" in page
    for link in ("/report", "/unknowns", "/employees", "/greetings", "/status", "/settings", "/live"):
        assert f'href="{link}"' not in page, link
    assert 'href="/account"' in page
    for path in ("/report", "/accuracy", "/groups", "/audit", "/unknowns", "/greetings", "/status", "/employees",
                 "/media/crops/2026-10-14/090000_000000_A_x.jpg", "/evidence/2026-10-14/A"):
        assert client.get(path).status_code in (403, 404), path
    account = client.get("/account")
    text = account.get_data(as_text=True)
    assert account.status_code == 200 and "bob@example.com" in text and "Read-only: the Today page" in text and "Sign out" in text
    with client.session_transaction() as session:
        token = session["csrf_token"]
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    assert client.get("/").status_code == 302  # signed out
