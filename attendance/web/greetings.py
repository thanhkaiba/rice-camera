"""The Greetings page: what the door said, day by day, for the office to rate (good, so-so, not good) and
remark on. Claude is shown the ratings when it writes the next days' sentences (greeting.office_feedback).
Today it also shows what the door is still going to say, to whom (sent by the Mac, see edge.send_upcoming)."""
from __future__ import annotations

import datetime as dt
import io
import time
import uuid
from typing import TYPE_CHECKING

from flask import Flask, jsonify, render_template, request, send_file, url_for

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
        from ..core.greeting import untagged
        rows = [{"id": emp, "name": people[emp].name if emp in people else emp,
                 "plain_text": untagged(g["text"]),
                 "boss": emp in people and (people[emp].not_counted or people[emp].honorific in {"sep", "shark"}),
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
            # (label, list, place in it, sentence): the list and place find the audio made ahead
            on = [("on_time", i, t) for i, t in enumerate(entry.get("on_time", []))]
            late = [("late", i, t) for i, t in enumerate(entry.get("late", []))]
            lines = ([("on time", *x) for x in on] + [("late", *x) for x in late] if entry.get("timed", True)
                     else [("any time", *x) for x in on + late])
            if entry.get("on_time") and entry.get("on_time") == entry.get("late"):
                lines = [("any arrival", *x) for x in on]
            lines += [("busy door", "short", i, t) for i, t in enumerate(entry.get("short", []))]
            rows.append({"id": emp, "name": person.name, "note": entry.get("note", ""), "lines": lines})
        rows.sort(key=lambda r: r["name"].lower())
        at = sent.get("at")
        return {"rows": rows, "together": sent.get("together", []), "at": dt.datetime.fromisoformat(at) if at else None}

    @app.post("/greetings/audio")
    def greeting_audio():
        """One of today's sentences still to come, as the door will say it: the audio made ahead on the office
        computer, never made again (no credits)."""
        day = service.now().date()
        emp, line = request.form.get("employee_id", ""), request.form.get("line", "")
        try:
            index = int(request.form.get("index", ""))
        except ValueError:
            index = -1
        if not emp or line not in ("on_time", "late", "short") or index < 0:
            return jsonify(ok=False, message="Unknown sentence."), 400
        if not core.photos_read_elsewhere:  # the camera runs here: its own files
            greeter = getattr(service, "greeter", None)
            if greeter is None:
                return jsonify(ok=False, message="No greeting audio is made on this computer."), 404
            try:
                data = greeter.made_audio(emp, line, index, day)
            except ValueError as exc:
                return jsonify(ok=False, message=str(exc)), 404
            response = send_file(io.BytesIO(data), mimetype="audio/wav", download_name="greeting.wav")
            response.headers["Cache-Control"] = "private, no-store"
            return response
        previews, lock = app.extensions["voice_previews"], app.extensions["voice_preview_lock"]
        with lock:
            for key, preview in list(previews.items()):
                if preview.get("kind") == "greeting_audio" and time.time() >= preview["expires"]:
                    store.edge_job_take(preview["job_id"])
                    previews.pop(key)
            if sum(p.get("kind") == "greeting_audio" for p in previews.values()) >= 3:
                return jsonify(ok=False, message="A few sentences are still on their way: wait a moment."), 409
            preview_id = uuid.uuid4().hex
            expires = time.time() + 120
            job_id = store.edge_job_add("greeting_audio", {
                "kind": "greeting_audio", "preview_id": preview_id, "day": day.isoformat(), "employee_id": emp,
                "line": line, "index": index, "expires": expires}, service.now())
            previews[preview_id] = {"job_id": job_id, "expires": expires, "kind": "greeting_audio"}
        return jsonify(ok=True, preview_url=url_for("greeting_preview_text_result", preview_id=preview_id)), 202

    @app.post("/greetings/rate")
    def rate_greeting():
        try:
            day = dt.date.fromisoformat(request.form.get("day", ""))
            kept = store.rate_greeting(day, request.form.get("employee_id", ""), request.form.get("rating", ""),
                                       request.form.get("comment", ""), current_user(cfg), service.now())
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        return jsonify(ok=True, **kept)
