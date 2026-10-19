"""A person's own page (/p/<id>/<token>): their arrivals of the last weeks, upcoming leave, and notes
they write about a day. Opened without the admin login; the link is signed with the web secret and
a per-person key, so it shows only that person, and a new link makes the old one stop working."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from ..store import LEAVE_KINDS
from . import current_user, done

if TYPE_CHECKING:
    from ..core.pipeline import App

DAYS_SHOWN = 30
NOTE_DAYS_AHEAD = 30
MAX_NOTE = 200


def _key_name(employee_id: str) -> str:
    return f"personal_link:{employee_id}"


def token(store, secret: str, employee_id: str, create: bool = False) -> str | None:
    """The signature in the person's link; None when no link was made yet (and `create` is false)."""
    key = store.get(_key_name(employee_id))
    if not key:
        if not create:
            return None
        key = secrets.token_hex(16)
        store.put(_key_name(employee_id), key)
    return hmac.new(secret.encode(), f"personal:{employee_id}:{key}".encode(), hashlib.sha256).hexdigest()[:32]


def renew(store, employee_id: str) -> None:
    """Give the person a new link; the old one stops working."""
    store.put(_key_name(employee_id), secrets.token_hex(16))


def register(app: Flask, core: "App") -> None:
    cfg, store, service = core.cfg, core.store, core.service

    def _person(employee_id: str, sig: str):
        expected = token(store, app.secret_key, employee_id)
        person = store.employee(employee_id)
        if person is None or expected is None or not hmac.compare_digest(sig, expected):
            abort(404)
        return person

    def _status(day: dt.date, employee_id: str, exempt: bool, now: dt.datetime) -> dict:
        workday, note = service.day_info(day)
        w = service.window(day)
        arrival = store.checkins(day).get(employee_id)
        leave = store.leaves_on(day).get(employee_id)
        row = {"day": day, "workday": workday, "time": None, "status": "", "kind": "info"}
        if arrival is not None:
            ts = service._local(arrival.first_seen)
            row["time"] = f"{ts:%H:%M}"
            if exempt or not workday or leave:
                row["status"], row["kind"] = "Arrived", "ok"
            elif ts >= w.late_after:
                row["status"], row["kind"] = f"Late {int((ts - w.late_after).total_seconds() // 60)} min", "late"
            else:
                row["status"], row["kind"] = "On time", "ok"
        if leave:
            row["status"] = (row["status"] + " · " if row["status"] else "") + LEAVE_KINDS.get(leave, leave)
        elif not workday:
            row["status"] = row["status"] or note or "Not a work day"
        elif arrival is None and not exempt:
            row["status"], row["kind"] = ("Not seen", "bad") if now >= w.late_after else ("", "info")
        return row

    @app.get("/p/<employee_id>/<sig>")
    def personal_page(employee_id: str, sig: str):
        person = _person(employee_id, sig)
        now = service.now()
        today = now.date()
        first = today - dt.timedelta(days=DAYS_SHOWN - 1)
        notes = store.notes_of(employee_id, first, today + dt.timedelta(days=NOTE_DAYS_AHEAD))
        days = []
        for i in range(DAYS_SHOWN):
            d = today - dt.timedelta(days=i)
            row = _status(d, employee_id, person.exempt, now)
            if row["workday"] or row["time"] or d in notes:
                days.append({**row, "note": notes.get(d, "")})
        upcoming = {d: n for d, n in notes.items() if d > today}
        return render_template("personal.html", person=person, days=days, today=today, sig=sig,
                               leaves=store.leaves(employee_id=employee_id, ending_after=today),
                               upcoming=sorted(upcoming.items()), leave_label=lambda k: LEAVE_KINDS.get(k, k),
                               last=today + dt.timedelta(days=NOTE_DAYS_AHEAD), first=first)

    @app.post("/p/<employee_id>/<sig>/note")
    def personal_note(employee_id: str, sig: str):
        person = _person(employee_id, sig)
        today = service.now().date()
        back = url_for("personal_page", employee_id=employee_id, sig=sig)
        try:
            day = dt.date.fromisoformat(request.form.get("day", ""))
        except ValueError:
            flash("Pick a day.", "error")
            return redirect(back)
        note = " ".join(request.form.get("note", "").split())[:MAX_NOTE]
        if not today - dt.timedelta(days=DAYS_SHOWN) < day <= today + dt.timedelta(days=NOTE_DAYS_AHEAD):
            flash(f"Notes can be written for the last {DAYS_SHOWN} and the next {NOTE_DAYS_AHEAD} days.", "error")
            return redirect(back)
        store.set_day_note(day, employee_id, note, service.now())
        store.log(service.now(), person.email or person.name, "personal note",
                  f"{day} {person.name}: {note or '(removed)'}")
        flash("Note saved. The office sees it on the attendance page." if note else "Note removed.", "ok")
        return redirect(back)

    @app.post("/employees/<employee_id>/personal-link")
    def renew_personal_link(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        renew(store, employee_id)
        store.log(service.now(), current_user(cfg), "new personal link", f"{person.email} {person.name}")
        return done("New personal link made; the old one no longer works.", "employee", employee_id=employee_id)

    app.jinja_env.globals["personal_link"] = lambda employee_id: service._link(
        url_for("personal_page", employee_id=employee_id, sig=token(store, app.secret_key, employee_id, create=True)))
