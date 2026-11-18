# Google Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Google login for active employees, viewer by default, with admin/viewer edited on Employee.
**Architecture:** Reuse Flask's shared guard, signed sessions, SQLite and existing route permissions. Keep portal accounts and Google configuration outside synced employee rows/settings; verify Google ID tokens with google-auth.
**Tech Stack:** Python, Flask, SQLite, google-auth with requests transport.
**Spec:** `docs/superpowers/specs/2026-11-18-google-login-design.md`

## Global Constraints

- Exactly admin/viewer; existing and new active employees default to viewer.
- Roles edited on Employee; Google client configuration edited in Settings.
- Eight-hour sessions; short-lived single-use OAuth state/nonce and PKCE.
- Preserve Basic recovery login, token-authenticated APIs and standalone mode.
- No Google secrets, bindings or portal roles in camera sync/config exports/diagnostics.
- No inference/model downloads on the server.

## Review Focus

- Email changes, deactivation/reactivation and deletion invalidate prior sessions.
- Last-admin checks apply to both employee updates and deletion, including concurrent writes.
- OAuth state copied from a browser cookie cannot be replayed after consumption.
- Browser authentication does not redirect machine API requests or weaken their token checks.
- Credentials cannot leak through settings serialization, diagnostics or camera sync.

### Task 1: Persistent employee portal access

**Files:** `attendance/store/schema.py`, new `attendance/store/web.py`, `attendance/store/__init__.py`, `attendance/store/employees.py`, `attendance/store/edge.py`, `tests/test_google_login.py`.
**Interfaces:** `web_role(employee_id) -> str`, `web_config() -> dict`, `web_save_config(config)`, `web_bind(employee_id, subject, *, expected_email) -> dict`, `web_take_state(state, now) -> dict | None`; optional `portal_role` in `update_employee`.
- [x] Write/run failing tests for viewer defaults, roles, last-admin protection, binding reset and state expiry/replay.
- [x] Add isolated portal table and helpers. Enforce lifecycle changes transactionally with SQLite triggers, including cleanup on deletion. Keep existing employee schema/sync payloads unchanged.
- [x] Run the store tests and existing employee/edge tests; baseline date-window exceptions are recorded below.

### Task 2: Google authentication and guarded sessions

**Files:** new `attendance/web/auth.py`, `attendance/web/__init__.py`, new `attendance/web/templates/login.html`, `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`.
**Interfaces:** `auth.register(app, core)`, `auth.identity() -> dict | None`, `auth.csrf_token() -> str`; routes `/login`, `/login/password`, `/auth/google`, `/auth/google/callback`, `/logout`.
- [x] Write/run failing flow tests covering OAuth params, state/nonce validation/replay/expiry, verified Google identity, subject mismatch, safe redirects, API behavior, viewer writes and invalidated/expired sessions.
- [x] Implement Google code exchange with ten-second network timeouts and library token verification; preserve HTTP status on upstream failure without exposing responses/tokens. Reuse shared authorization and audit identity.
- [x] Add CSRF validation for session writes and forms/fetch injection, eight-hour session expiry, HTTPS cookie security, and logout.
- [x] Run authentication and existing Basic/public/edge tests; baseline date-window exceptions are recorded below.

### Task 3: Employee and Settings UI, documentation and review

**Files:** `attendance/web/employees.py`, `attendance/web/settings.py`, `attendance/web/templates/employee.html`, `attendance/web/templates/settings.html`, `attendance/web/templates/base.html`, portal POST form templates, `attendance/web/static/app.js`, `attendance/web/static/app.css`, `attendance/core/settings.py`, `attendance/core/transfer.py`, `attendance/core/diagnostics.py`, `attendance/server.py`, `attendance/web/edge_import.py`, new `docs/google-login.md`, `README.md`, `tests/test_google_login.py`, existing form-control tests.
**Interfaces:** existing employee update accepts `portal_role`; admin-only `/settings/google-login` saves separate configuration and validates enable prerequisites.
- [x] Write/run failing tests for UI role changes/audits, last-admin errors, secret masking, sync/export exclusion and configuration prerequisites.
- [x] Add Employee role selector, Settings Google configuration, signed-in identity/logout and setup instructions. Preserve the independent Google Chat connection.
- [x] Run targeted tests and complete pytest suite; inspect final diff and request one independent security/correctness review. Fix material findings with regression tests.
- [x] Leave changes reviewable in the workspace; do not deploy or push.

## Execution record

User approved implementation. Execute inline in the current workspace; no additional approval round. Use a feature branch when permitted. No worktree relocation because the shared IDE is pointed at this checkout.

- Branch: `feat/google-login`; leave changes uncommitted and reviewable. No merge, push or deployment requested.
- Store tests: 5 failing before implementation, then 5 passing. Google/basic/UI tests: 67 passing before independent review; added regression coverage for every material review finding.
- Existing form-control tests were updated to include the new CSRF field while still submitting all rendered fields.
- Independent review: transaction races in identity binding/admin enablement, viewer logout CSS, recovery-password reset/import, per-response HTTPS cookies and shadow session actions. Fixed with regression tests; CSS reviewed against its selectors. Also fixed an existing concurrent first-start session-secret race exposed by the new login sessions.
- Preserve upload parsing policy: defer CSRF form parsing until the four handlers with custom size/streaming limits have installed those limits.
- Baseline comparison: an unmodified `git archive HEAD` run produced 877 passing / 6 failing tests. Five failures use fixed 2026-10-14 data outside the current 30-day window (four edge-sync tests and diagnostics history); the sixth needed permission to bind a temporary localhost socket and passes when run with that permission.
- The first final compatibility run passed 919 tests with precisely those five pre-existing date-sensitive tests deselected. After the remaining review fixes, the complete compatibility run is repeated with the same five exceptions. No production clocks or unrelated date tests were changed.
- Real Google OAuth and production reverse-proxy behavior require deployment credentials and URL; mocked exchanges and real cryptographic verification are covered locally.
- Final verification after all review fixes: **927 passed, 5 deselected in 98.03s**. The 49 Google-login tests are included. The five deselections are precisely the baseline date-window failures above. `git diff --check` and Python compilation checks passed. All actionable review findings were fixed; no product findings were deferred.
