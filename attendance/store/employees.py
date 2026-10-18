"""Employee and reference face embedding persistence mixin."""
from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3
import unicodedata
import uuid
from collections import Counter, defaultdict

import numpy as np

from .schema import CHAT_COLUMNS, Employee, Reference, _blob, _vec, normalize_chat_user, normalize_email


class EmployeeStoreMixin:
    def import_employees(self, csv_text: str) -> int:
        """Upsert by email; attach a legacy record only when its name maps to exactly one email."""
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames or not {"email", "name"} <= {f.strip().lower() for f in reader.fieldnames}:
            raise ValueError("the CSV needs a header row with the columns email,name")
        rows = []
        seen = set()
        for r in reader:
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if not any(r.values()):
                continue
            try:
                email = normalize_email(r.get("email", ""))
                if not r.get("name"):
                    raise ValueError("name is required")
                if email in seen:
                    raise ValueError(f"duplicate email {email}")
                chat_user = normalize_chat_user(next((r[c] for c in CHAT_COLUMNS if r.get(c)), ""))
            except ValueError as exc:
                raise ValueError(f"row {reader.line_num}: {exc}") from None
            seen.add(email)
            rows.append((email, r["name"], chat_user))
        name_counts = Counter(unicodedata.normalize("NFC", name).casefold() for _, name, _ in rows)
        with self._lock, self._db:
            people = self.all_employees()
            by_email = {p.email: p.id for p in people if p.email}
            legacy = defaultdict(list)
            for p in people:
                if not p.email:
                    legacy[unicodedata.normalize("NFC", p.name).casefold()].append(p.id)
            updates = []
            for email, name, chat_user in rows:
                employee_id = by_email.get(email)
                key = unicodedata.normalize("NFC", name).casefold()
                if employee_id is None and legacy[key]:
                    if len(legacy[key]) != 1 or name_counts[key] != 1:
                        raise ValueError(f"ambiguous legacy employee name {name}; set their emails individually first")
                    employee_id = legacy[key][0]
                updates.append((employee_id or uuid.uuid4().hex, email, name, chat_user))
            self._db.executemany(
                "INSERT INTO employees (id, email, name, active, chat_user) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(id) DO UPDATE SET email = excluded.email, name = excluded.name, active = 1, "
                "chat_user = CASE WHEN excluded.chat_user != '' THEN excluded.chat_user ELSE employees.chat_user END",
                updates,
            )
        return len(rows)

    def add_employee(self, email: str, name: str) -> str:
        email, name = normalize_email(email), name.strip()
        if not name:
            raise ValueError("name is required")
        employee_id = uuid.uuid4().hex
        try:
            self._exec("INSERT INTO employees (id, email, name, active) VALUES (?, ?, ?, 1)", (employee_id, email, name))
        except sqlite3.IntegrityError:
            raise ValueError(f"employee email {email} already exists") from None
        return employee_id

    def update_employee(self, employee_id: str, name: str, active: bool, chat_user: str | None = None,
                        exempt: bool | None = None, *, email: str | None = None) -> None:
        if not name.strip():
            raise ValueError("name is required")
        person = self.employee(employee_id)
        if person is None:
            raise ValueError("employee not found")
        email = normalize_email(email if email is not None else person.email)
        chat_user = normalize_chat_user(chat_user) if chat_user is not None else person.chat_user
        try:
            self._exec("UPDATE employees SET email = ?, name = ?, active = ?, chat_user = ?, exempt = ? WHERE id = ?",
                       (email, name.strip(), int(active), chat_user,
                        int(exempt if exempt is not None else person.exempt), employee_id))
        except sqlite3.IntegrityError:
            raise ValueError(f"employee email {email} already exists") from None

    def employee_by_email(self, email: str) -> Employee | None:
        rows = self._all("SELECT * FROM employees WHERE email = ? COLLATE NOCASE", (normalize_email(email),))
        return self._employee(rows[0]) if rows else None

    def employee_emails(self) -> dict[str, str]:
        return {r["id"]: r["email"] or "" for r in self._all("SELECT id, email FROM employees")}

    def exempt_ids(self) -> set[str]:
        """Employees whose arrivals are not counted as late or absent."""
        return {r["id"] for r in self._all("SELECT id FROM employees WHERE exempt = 1")}

    def delete_employee(self, employee_id: str) -> list[str]:
        """Remove the employee and their face data; returns reference image paths to delete.
        Past attendance records are kept."""
        images = [r["image"] for r in self._all("SELECT DISTINCT image FROM embeddings WHERE employee_id = ? "
                                                "AND image IS NOT NULL", (employee_id,))]
        with self._lock, self._db:
            self._db.execute("DELETE FROM embeddings WHERE employee_id = ?", (employee_id,))
            self._db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        return images

    def employee(self, employee_id: str) -> Employee | None:
        rows = self._all("SELECT * FROM employees WHERE id = ?", (employee_id,))
        return self._employee(rows[0]) if rows else None

    @staticmethod
    def _employee(r) -> Employee:
        return Employee(r["id"], r["name"], bool(r["active"]), r["chat_user"] or "", bool(r["exempt"]), r["email"] or "")

    def all_employees(self) -> list[Employee]:
        return [self._employee(r) for r in self._all("SELECT * FROM employees ORDER BY active DESC, name")]

    def employees(self) -> dict[str, str]:
        """Active employees: id -> name."""
        return {r["id"]: r["name"] for r in self._all("SELECT id, name FROM employees WHERE active = 1 ORDER BY name")}

    def employee_count(self) -> int:
        return self._all("SELECT COUNT(*) AS n FROM employees")[0]["n"]

    # reference faces -------------------------------------------------------

    def add_embedding(self, model: str, employee_id: str, vector: np.ndarray, source: str, now: dt.datetime,
                      image: str | None = None) -> None:
        self._exec(
            "INSERT INTO embeddings (model, employee_id, vector, source, image, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (model, employee_id, _blob(vector), source, image, now.isoformat()),
        )

    def embeddings(self, model: str) -> list[tuple[str, np.ndarray]]:
        rows = self._all(
            "SELECT e.employee_id, e.vector FROM embeddings e JOIN employees p ON p.id = e.employee_id "
            "WHERE p.active = 1 AND e.model = ?",
            (model,),
        )
        return [(r["employee_id"], _vec(r["vector"])) for r in rows]

    def references(self, model: str, employee_id: str | None = None) -> list[Reference]:
        sql, params = "SELECT * FROM embeddings WHERE model = ?", [model]
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        return [Reference(r["id"], r["employee_id"], r["image"], r["source"], _vec(r["vector"]))
                for r in self._all(sql + " ORDER BY id", params)]

    def reference_images(self) -> list[tuple[str, str, str]]:
        """(employee_id, image, source) of every stored reference photo, from any model."""
        rows = self._all("SELECT employee_id, image, MIN(source) AS source FROM embeddings "
                         "WHERE image IS NOT NULL GROUP BY employee_id, image")
        return [(r["employee_id"], r["image"], r["source"]) for r in rows]

    def delete_reference(self, ref_id: int) -> None:
        self._exec("DELETE FROM embeddings WHERE id = ?", (ref_id,))

    def delete_reference_image(self, image: str) -> None:
        """Delete a reference photo's embeddings for all models."""
        self._exec("DELETE FROM embeddings WHERE image = ?", (image,))

    def delete_embeddings(self, model: str, employee_id: str, source_prefix: str) -> list[str]:
        images = [r["image"] for r in self._all(
            "SELECT image FROM embeddings WHERE model = ? AND employee_id = ? AND source LIKE ? AND image IS NOT NULL",
            (model, employee_id, source_prefix + "%"))]
        self._exec("DELETE FROM embeddings WHERE model = ? AND employee_id = ? AND source LIKE ?",
                   (model, employee_id, source_prefix + "%"))
        return images
