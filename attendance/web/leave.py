"""Routes for leave, holidays and extra work days entered by hand (/calendar), next to the HR system's."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from flask import Flask, abort, render_template, request

from ..store import LEAVE_KINDS
from . import _date, current_user, done, failed, leave_status

if TYPE_CHECKING:
    from ..core.pipeline import App

CALENDAR_KINDS = {"holiday": "Holiday", "workday": "Extra work day"}


def _span(start: dt.date, end: dt.date) -> str:
    return start.isoformat() if start == end else f"{start} – {end}"


def register(app: Flask, core: "App") -> None:
    cfg, store, service = core.cfg, core.store, core.service
    app.jinja_env.globals["leave_kinds"] = LEAVE_KINDS  # the leave forms on the person and Today pages

    def _form_date(name: str, default: dt.date | None = None) -> dt.date:
        value = request.form.get(name, "").strip()
        if not value and default is not None:
            return default
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError("dates must be YYYY-MM-DD") from None

    @app.get("/calendar")
    def calendar_page():
        today = service.now().date()
        start = _date(request.args.get("from"), today - dt.timedelta(days=7))
        names = store.employees()
        return render_template("calendar.html", days=store.calendar_days(start),
                               leaves=store.leaves(ending_after=start), names=names, today=today,
                               kinds=CALENDAR_KINDS, leave_kinds=LEAVE_KINDS, leave=leave_status(core))

    @app.post("/calendar/day")
    def add_calendar_day():
        try:
            day, kind = _form_date("day"), request.form.get("kind", "")
            note = request.form.get("note", "")
            store.set_calendar_day(day, kind, note)
        except ValueError as exc:
            return failed(str(exc), "calendar_page")
        store.log(service.now(), current_user(cfg), "add calendar day", f"{day} {CALENDAR_KINDS[kind]} {note.strip()}".strip())
        return done(f"{CALENDAR_KINDS[kind]} saved for {day:%d %b %Y}.", "calendar_page")

    @app.post("/calendar/day/<day>/delete")
    def delete_calendar_day(day: str):
        d = _date(day, service.now().date())
        if not store.delete_calendar_day(d):
            return failed("Only days entered here can be removed; change the HR system's days there.", "calendar_page")
        store.log(service.now(), current_user(cfg), "delete calendar day", d.isoformat())
        return done(f"{d:%d %b %Y} removed.", "calendar_page")

    @app.post("/leave")
    def add_leave():
        employee_id = request.form.get("employee_id", "")
        person = store.employee(employee_id)
        if person is None:
            return failed("Choose a person.", "calendar_page")
        try:
            start = _form_date("start")
            end = _form_date("end", start)
            kind = request.form.get("kind", "leave")
            store.add_leave(employee_id, start, end, kind, request.form.get("note", ""))
        except ValueError as exc:
            return failed(str(exc), "calendar_page")
        store.log(service.now(), current_user(cfg), "add leave",
                  f"{_span(start, end)} {person.name} {LEAVE_KINDS.get(kind, kind)}")
        return done(f"{LEAVE_KINDS.get(kind, kind)} saved for {person.name}: never counted late or missing then.",
                    "calendar_page")

    @app.post("/leave/<int:leave_id>/delete")
    def delete_leave(leave_id: int):
        entry = store.leave(leave_id) or abort(404)
        if not store.delete_leave(leave_id):
            return failed("Leave from the HR system is changed there.", "calendar_page")
        store.log(service.now(), current_user(cfg), "delete leave",
                  f"{_span(entry.start, entry.end)} {store.employees().get(entry.employee_id, entry.employee_id)} "
                  f"{LEAVE_KINDS.get(entry.kind, entry.kind)}")
        return done("Leave removed.", "calendar_page")
