"""Portal-only Google configuration and employee access."""
from __future__ import annotations

import json
import sqlite3

from .schema import LAST_ADMIN_ERROR, PORTAL_ROLES, normalize_email


class WebStoreMixin:
    def web_config(self) -> dict:
        return {**json.loads(self.get("google_login_config") or "{}"),
                "enabled": self.get("google_login_enabled") == "1"}

    def web_save_config(self, config: dict) -> None:
        with self._lock, self._db:
            for key, value in (("google_login_config", json.dumps({k: v for k, v in config.items() if k != "enabled"})),
                               ("google_login_enabled", "1" if config.get("enabled") else "0")):
                self._db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) "
                                 "DO UPDATE SET value = excluded.value", (key, value))
            # Check after the write acquires SQLite's lock; a concurrent demotion must not race enabling.
            if config.get("enabled") and not self._db.execute(
                    "SELECT 1 FROM web_accounts a JOIN employees e ON e.id = a.employee_id "
                    "WHERE a.role = 'admin' AND e.active = 1").fetchone():
                raise ValueError("Choose at least one active employee as a Google admin first.")

    def web_role(self, employee_id: str) -> str:
        rows = self._all("SELECT role FROM web_accounts WHERE employee_id = ?", (employee_id,))
        return rows[0]["role"] if rows else "viewer"

    def web_roles(self) -> dict[str, str]:
        return {r["employee_id"]: r["role"] for r in self._all("SELECT employee_id, role FROM web_accounts")}

    def web_set_role(self, employee_id: str, role: str) -> None:
        if role not in PORTAL_ROLES:
            raise ValueError("portal role must be admin or viewer")
        with self._lock, self._db:
            if role != "admin":
                self._web_keep_an_admin(employee_id)
            changed = self._db.execute("INSERT INTO web_accounts (employee_id, role) SELECT id, ? FROM employees WHERE id = ? "
                                       "ON CONFLICT(employee_id) DO UPDATE SET role = excluded.role", (role, employee_id)).rowcount
        if not changed:
            raise ValueError("employee not found")

    def _web_keep_an_admin(self, employee_id: str) -> None:
        """Inside the caller's transaction, before a portal edit takes `employee_id` out of the Google admins.
        Camera sync does not ask: it is authoritative, and the password admin can always pick a new one."""
        if self._db.execute(
                "SELECT (SELECT value FROM kv WHERE key = 'google_login_enabled') = '1' "
                "AND EXISTS (SELECT 1 FROM web_accounts a JOIN employees e ON e.id = a.employee_id "
                "            WHERE e.id = ? AND a.role = 'admin' AND e.active = 1) "
                "AND NOT EXISTS (SELECT 1 FROM web_accounts a JOIN employees e ON e.id = a.employee_id "
                "                WHERE e.id != ? AND a.role = 'admin' AND e.active = 1)",
                (employee_id, employee_id)).fetchone()[0]:
            raise ValueError(LAST_ADMIN_ERROR)

    def web_account(self, employee_id: str) -> dict | None:
        rows = self._all("SELECT e.id, e.email, e.active, a.role, a.subject, a.version "
                         "FROM employees e JOIN web_accounts a ON a.employee_id = e.id WHERE e.id = ?", (employee_id,))
        return dict(rows[0]) if rows else None

    def web_bind(self, employee_id: str, subject: str, *, expected_email: str) -> dict:
        expected_email = normalize_email(expected_email)
        if not subject or len(subject) > 255:
            raise ValueError("Invalid Google account identity.")
        try:
            with self._lock, self._db:
                self._db.execute("INSERT OR IGNORE INTO web_accounts (employee_id) "
                                 "SELECT id FROM employees WHERE id = ? AND email = ? COLLATE NOCASE AND active = 1",
                                 (employee_id, expected_email))
                account = self.web_account(employee_id)
                if not account or not account["active"] or account["email"].lower() != expected_email:
                    raise ValueError("This employee does not have portal access.")
                if account["subject"] and account["subject"] != subject:
                    raise ValueError("This employee is linked to a different Google account.")
                self._db.execute("UPDATE web_accounts SET subject = ? WHERE employee_id = ?", (subject, employee_id))
                return self.web_account(employee_id)
        except sqlite3.IntegrityError:
            raise ValueError("This Google account is already linked to another employee.") from None
