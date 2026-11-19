"""Google portal login; independent of the Google Chat integration and camera sync."""
from __future__ import annotations

import functools
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from flask import abort, current_app, g, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface

AUTH_PUBLIC = {"login", "password_login", "google_start", "google_return", "google_login", "logout"}
# These handlers set upload limits/streaming before reading the CSRF form field.
DEFER_CSRF = {"import_seed", "greeting_voice_create", "greeting_voice_upload", "greeting_preview_text"}
SESSION_SECONDS = 8 * 3600
STATE_SECONDS = 600  # from "Sign in with Google" to coming back from Google
MAX_USED_STATES = 10_000
CALLBACK = "/auth/google/callback"


class PortalSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        address = urlsplit(app.extensions["portal_core"].cfg.web.public_url or "")
        # Only trust the configured HTTPS origin for its own host; LAN/localhost HTTP must keep the cookie.
        return request.is_secure or (address.scheme == "https" and address.netloc.lower() == request.host.lower())


class GoogleLoginError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def callback_url() -> str:
    """Where Google sends the browser back: the address it is on, so the session cookie (and its nonce) comes
    back too. The configured address wins for its own host, as HTTPS may end at a proxy in front of us."""
    public = urlsplit(current_app.extensions["portal_core"].cfg.web.public_url or "")
    base = f"{public.scheme}://{public.netloc}" if public.netloc.lower() == request.host.lower() else request.host_url
    return base.rstrip("/") + CALLBACK


def verify(client_id: str, credential: str) -> dict:
    """The claims of an ID token from Google, checked here: Google's signature, issuer, expiry
    and that it was issued to `client_id`. Only Google's public keys are fetched."""
    try:
        import requests
        from google.auth.exceptions import GoogleAuthError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token
    except ImportError as exc:  # the server's libraries lack google-auth: say so instead of a bare error 500
        current_app.logger.error("Google login cannot verify tokens: %s", exc)
        raise GoogleLoginError("Google login is not installed on this server yet (google-auth is missing). "
                               "Use password login and tell the administrator.", 503) from None

    if not credential:
        raise GoogleLoginError("Google did not return a sign-in token. Try again.")
    try:
        with requests.Session() as http:
            return id_token.verify_oauth2_token(credential, functools.partial(Request(session=http), timeout=10),
                                               audience=client_id, clock_skew_in_seconds=10)
    except (requests.RequestException, TransportError):
        raise GoogleLoginError("Could not reach Google. Try again or use password login.", 502) from None
    except (GoogleAuthError, ValueError):
        raise GoogleLoginError("Google identity verification failed. Try signing in again.") from None


def safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value and not any(
        ord(c) < 32 for c in value) else "/"


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def check_csrf() -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if not identity() and request.endpoint not in {"password_login", "google_login", "logout"}:
        return
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        abort(403, "This form has expired. Reload the page and try again.")


def password_version(username: str, password: str) -> str:
    return hmac.new(current_app.secret_key.encode(), (username + "\0" + password).encode(), hashlib.sha256).hexdigest()


def portal_config() -> dict:
    """The Google login settings, read once per request (identity, role and the login redirect all ask)."""
    if "portal_config" not in g:
        g.portal_config = current_app.extensions["portal_core"].store.web_config()
    return g.portal_config


def identity() -> dict | None:
    if "portal_identity" in g:
        return g.portal_identity
    g.portal_identity = None
    saved = session.get("portal_login")
    if not saved:
        return None
    cfg, store = current_app.extensions["portal_core"].cfg, current_app.extensions["portal_core"].store
    valid_age = 0 <= time.time() - saved.get("at", 0) < SESSION_SECONDS
    if valid_age and saved.get("kind") == "google" and portal_config()["enabled"]:
        account = store.web_account(saved.get("employee_id", ""))
        if (account and account["active"] and account["subject"] and account["subject"] == saved.get("subject")
                and account["email"].lower() == saved.get("email") and account["version"] == saved.get("version")):
            g.portal_identity = {**account, "user": account["email"], "kind": "google"}
    elif valid_age and saved.get("kind") == "password":
        w = cfg.web
        for username, password, role in ((w.username, w.password, "admin"), (w.viewer_username, w.viewer_password, "viewer")):
            if w.password and password and saved.get("user") == username and hmac.compare_digest(
                    saved.get("version", ""), password_version(username, password)):
                g.portal_identity = {"role": role, "user": username, "kind": "password"}
                break
    if g.portal_identity is None:
        session.pop("portal_login", None)
    return g.portal_identity


def register(app, core) -> None:
    app.extensions["portal_core"] = core
    app.session_interface = PortalSessionInterface()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    def page_csrf_token() -> str:
        # Basic-auth scripts never post with it (check_csrf skips them): no session cookie just for a page.
        return "" if request.authorization else csrf_token()

    app.jinja_env.globals["csrf_token"] = page_csrf_token

    @app.before_request
    def limit_login_forms():
        if request.endpoint in {"password_login", "google_login", "save_google_login"}:
            request.max_content_length = 8192

    def login_page(message="", status=200):
        config = portal_config()
        return render_template("login.html", google_enabled=config["enabled"] and bool(config.get("client_id")),
                               message=message, next=safe_next(request.values.get("next", "/"))), status

    @app.get("/login")
    def login():
        return login_page()

    @app.post("/login/password")
    def password_login():
        check_csrf()
        from . import credential_role

        username, password = request.form.get("username", ""), request.form.get("password", "")
        if not credential_role(core.cfg, username, password):
            return login_page("Username or password is incorrect.", 401)
        session.clear()
        session["portal_login"] = {"kind": "password", "user": username, "at": time.time(),
                                  "version": password_version(username, password)}
        csrf_token()
        return redirect(safe_next(request.form.get("next", "/")))

    used, used_lock = {}, threading.Lock()  # nonce -> expiry, for sign-ins that were posted

    def used_once(nonce: str, expires: float) -> bool:
        """False when this nonce was already used once (a replayed copy of the old session cookie)."""
        # ponytail: in-memory, correct for the single worker the server runs; a shared store if that changes.
        # Bounded, oldest first: only 10,000 sign-ins within an hour could push one out early.
        with used_lock:
            now = time.time()
            while used and (len(used) >= MAX_USED_STATES or next(iter(used.values())) <= now):
                del used[next(iter(used))]
            if nonce in used:
                return False
            used[nonce] = expires
            return True

    # The whole page goes to Google and back (no popup: home-screen apps on iPhone cannot open one).
    @app.get("/auth/google")
    def google_start():
        config = portal_config()
        if not config["enabled"] or not config.get("client_id"):
            return login_page("Google login is not configured. Use password login.", 503)
        # The token Google returns must carry this nonce: it signs in only this browser, once.
        nonce = secrets.token_urlsafe(32)
        session["google_login"] = {"nonce": nonce, "expires": time.time() + STATE_SECONDS,
                                   "next": safe_next(request.args.get("next", "/"))}
        query = {"client_id": config["client_id"], "redirect_uri": callback_url(), "response_type": "id_token",
                 "scope": "openid email", "nonce": nonce, "prompt": "select_account"}
        if config.get("domain"):
            query["hd"] = config["domain"]
        return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(query))

    @app.get(CALLBACK)
    def google_return():
        """Google puts the ID token after '#', which only the page sees: it posts it to google_login."""
        return render_template("google_return.html")

    @app.post("/auth/google")
    def google_login():
        check_csrf()
        pending = session.pop("google_login", None)
        config = portal_config()
        if not config["enabled"] or not config.get("client_id"):
            return login_page("Google login is not configured. Use password login.", 503)
        if (not isinstance(pending, dict) or pending.get("expires", 0) <= time.time()
                or not used_once(pending["nonce"], pending["expires"])):
            return login_page("Google sign-in has expired. Start again.", 400)
        if request.form.get("error"):
            return login_page("Google sign-in was cancelled or denied. Try again.", 400)
        domain = config.get("domain", "")
        try:
            claims = verify(config["client_id"], request.form.get("credential", ""))
            email, subject, nonce = claims.get("email"), claims.get("sub"), claims.get("nonce")
            if (not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode(), pending["nonce"].encode())
                    or not isinstance(subject, str) or not subject or len(subject) > 255):
                raise GoogleLoginError("Google identity verification failed.")
            if claims.get("email_verified") is not True or not isinstance(email, str) or not (
                    claims.get("hd") == domain if domain else email.lower().endswith("@gmail.com") or claims.get("hd")):
                raise GoogleLoginError(f"Use your {domain} Google account." if domain
                                       else "Use a verified Gmail or Google Workspace account.", 403)
            person = core.store.employee_by_email(email)
            if person is None or not person.active:
                raise GoogleLoginError("This email is not an active employee. Contact an administrator.", 403)
            account = core.store.web_bind(person.id, subject, expected_email=email)
        except GoogleLoginError as exc:
            return login_page(str(exc), exc.status)
        except ValueError:
            return login_page("This Google account cannot access the employee portal. Contact an administrator.", 403)
        session.clear()
        session["portal_login"] = {"kind": "google", "employee_id": person.id, "subject": subject,
                                  "email": person.email.lower(), "version": account["version"], "at": time.time()}
        csrf_token()
        core.store.log(core.service.now(), person.email, "Google login", "")
        return redirect(pending["next"])

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.after_request
    def private_responses(response):
        if request.endpoint in AUTH_PUBLIC or session.get("portal_login"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "same-origin"
        return response
