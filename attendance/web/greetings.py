"""The Greetings page: what the door said, day by day, for the office to rate (good, so-so, not good) and
remark on. Claude is shown the ratings when it writes the next days' sentences (greeting.office_feedback)."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from flask import Flask, jsonify, render_template, request

from . import _date, current_user

if TYPE_CHECKING:
    from ..core.pipeline import App

RATINGS = {"good": "Good", "ok": "So-so", "bad": "Not good"}
KINDS = {"on_time": "on time", "late": "late", "pending": "request waiting", "together": "said to a group"}


def register(app: Flask, core: "App") -> None:
    cfg, store, service = core.cfg, core.store, core.service

    @app.get("/greetings")
    def greetings_page():
        today = service.now().date()
        days = store.greeting_days()
        day = _date(request.args.get("day"), dt.date.fromisoformat(days[0]) if days else today)
        said, feedback = store.greetings(day), store.greeting_feedback(day)
        people = {p.id: p for p in store.all_employees()}
        rows = [{"id": emp, "name": people[emp].name if emp in people else emp,
                 "time": dt.datetime.fromisoformat(g["said_at"]), "text": g["text"], "kind": KINDS.get(g["kind"], g["kind"]),
                 "rating": (feedback.get(emp) or {}).get("rating", ""), "comment": (feedback.get(emp) or {}).get("comment", "")}
                for emp, g in said.items()]
        rows.sort(key=lambda r: r["time"])
        counts = {key: sum(r["rating"] == key for r in rows) for key in RATINGS}
        from ..core.greeting import office_feedback

        return render_template("greetings.html", day=day, days=days, rows=rows, ratings=RATINGS, counts=counts,
                               today=today, learned=office_feedback(store, today + dt.timedelta(days=1)))

    @app.post("/greetings/rate")
    def rate_greeting():
        try:
            day = dt.date.fromisoformat(request.form.get("day", ""))
            kept = store.rate_greeting(day, request.form.get("employee_id", ""), request.form.get("rating", ""),
                                       request.form.get("comment", ""), current_user(cfg), service.now())
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        return jsonify(ok=True, **kept)
