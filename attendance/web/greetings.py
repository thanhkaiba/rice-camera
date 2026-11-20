"""The Greetings page: what the door said, day by day, for the office to rate (good, so-so, not good) and
remark on. Claude is shown the ratings when it writes the next days' sentences (greeting.office_feedback).
Today it also shows what the door is still going to say, to whom (sent by the Mac, see edge.send_upcoming)."""
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
        from .edge_api import upcoming

        coming_today = upcoming(store, today) is not None or getattr(service, "greeter", None) is not None
        latest = dt.date.fromisoformat(days[0]) if days and not coming_today else today  # today, once it is written
        day = _date(request.args.get("day"), latest)
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
                               today=today, learned=office_feedback(store, today + dt.timedelta(days=1)),
                               coming=still_to_come(today, said, people) if day == today else None)

    def still_to_come(today: dt.date, said: dict, people: dict) -> dict | None:
        """Today's written sentences of whoever the door has not greeted yet, by name."""
        from .edge_api import upcoming

        sent = upcoming(store, today)
        greeter = getattr(service, "greeter", None)
        if sent is None and greeter is not None:  # the camera runs here, on its own: its own sentences
            sent = greeter.upcoming(today)
        if sent is None:
            return None
        rows = []
        for emp, entry in sent.get("people", {}).items():
            person = people.get(emp)
            if person is None or not person.active or emp in said:
                continue
            lines = ([("on time", t) for t in entry.get("on_time", [])] + [("late", t) for t in entry.get("late", [])]
                     if entry.get("timed", True) else
                     [("any time", t) for t in entry.get("on_time", []) + entry.get("late", [])])
            rows.append({"name": person.name, "note": entry.get("note", ""), "lines": lines})
        rows.sort(key=lambda r: r["name"].lower())
        at = sent.get("at")
        return {"rows": rows, "together": sent.get("together", []), "at": dt.datetime.fromisoformat(at) if at else None}

    @app.post("/greetings/rate")
    def rate_greeting():
        try:
            day = dt.date.fromisoformat(request.form.get("day", ""))
            kept = store.rate_greeting(day, request.form.get("employee_id", ""), request.form.get("rating", ""),
                                       request.form.get("comment", ""), current_user(cfg), service.now())
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        return jsonify(ok=True, **kept)
