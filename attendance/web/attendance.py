"""Routes for daily attendance (/), arrival/sighting corrections, and reviewing unknown faces (/unknowns)."""
from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from ..core.service import SOURCES, minutes_late
from ..core import evidence as evidence_module
from ..store import body_photo
from . import (
    _date,
    _time,
    current_user,
    deadline_status,
    done,
    failed,
    first_setup,
    late_message_status,
    leave_status,
)

if TYPE_CHECKING:
    from ..core.pipeline import App


def _review_name(name: str, email: str, full: bool = False) -> str:
    local = email if full else email.partition("@")[0]
    return f"{name} ({local})" if name and local else name or local


def _review_labels(people) -> dict[str, str]:
    """Picker labels, one per person: the email name, or the whole email where two people share an
    email name (an@company and an@partner). The picker maps a typed label back to the first person
    with that label, so a repeated one would confirm the face as the wrong person."""
    locals_ = [p.email.partition("@")[0] for p in people]
    return {p.id: _review_name(p.name, p.email, full=locals_.count(local) > 1) for p, local in zip(people, locals_)}


def wants_json() -> bool:
    """The Review page sends its forms in the background and shows the answer in place."""
    return request.headers.get("X-Requested-With") == "fetch"


def register(app: Flask, core: "App") -> None:
    cfg, store, service = core.cfg, core.store, core.service

    def reply(ok: bool, message: str, **data):
        """The outcome of a Review action: JSON for the page's background requests (with what else
        it should show, e.g. the cards that look like the person just confirmed), else a flash and
        a redirect back to Review."""
        if wants_json():
            return jsonify({"ok": ok, "message": message, **data}), (200 if ok else 400)
        return (done if ok else failed)(message, "unknowns")

    def related(members, employee_id: str) -> dict:
        """Pending cards that look like the faces just confirmed as `employee_id`, for the page to
        offer "N more cards look like X · Confirm all"."""
        person = store.employee(employee_id)
        cards = [{"id": g[-1].id, "count": len(g), "score": round(score, 2)}
                 for g, score in service.similar_pending(members)]
        return {"employee_id": employee_id,
                "label": _review_name(person.name, person.email) if person else "", "cards": cards}

    @app.get("/")
    def index():
        if first_setup.first_run(core) and not request.args.get("day"):
            return redirect(url_for("setup_page"))
        today = service.now().date()
        day = _date(request.args.get("day"), today)
        rep = service.report(day)
        sightings: dict[str, list[dict]] = {}
        arrivals = store.checkins(day)
        for x in store.sightings(day):
            a = arrivals.get(x.employee_id)
            sightings.setdefault(x.employee_id, []).append({
                "id": x.id, "time": f"{service._local(x.ts):%H:%M:%S}", "ok": x.status == "ok",
                "arrival": bool(a and x.status == "ok" and a.first_seen == x.ts and a.source == x.source),
                "source": SOURCES.get(x.source, x.source), "score": round(x.score * 100) if x.score else None,
                "image": url_for("media", path=x.image) if x.image else None})
        return render_template("index.html", rep=rep, employees=store.employees(), sightings=sightings,
                               late=lambda a: minutes_late(a, rep.window), today=today,
                               prev_day=day - dt.timedelta(days=1), next_day=day + dt.timedelta(days=1),
                               leave=leave_status(core), started=service.now() >= rep.window.start,
                               late_msg=late_message_status(core, day), deadline=deadline_status(core, day),
                               notes=store.day_notes(day),
                               backup_warning=core.backup.warning(service.now()))

    @app.post("/day/<day>/<employee_id>/set")
    def set_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.set_arrival(d, employee_id, _time(request.form.get("time", "")), current_user(cfg))
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival saved.", "index", day=day)

    @app.post("/day/<day>/<employee_id>/reassign")
    def reassign_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            learned = service.reassign_arrival(d, employee_id, request.form.get("to", ""), current_user(cfg))
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival moved to the right person" + (", and their face learned." if learned else "."),
                    "index", day=day)

    @app.post("/day/<day>/<employee_id>/delete")
    def delete_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.delete_arrival(d, employee_id, current_user(cfg))
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival deleted.", "index", day=day)

    def _evidence(day: str, employee_id: str):
        d = _date(day, service.now().date())
        if store.employee(employee_id) is None:
            abort(404)
        return d, evidence_module.build(service, d, employee_id)

    @app.get("/day/<day>/<employee_id>/evidence")
    def evidence_page(day: str, employee_id: str):
        d, ev = _evidence(day, employee_id)
        return render_template("evidence.html", ev=ev, day=d, cfg=cfg)

    @app.get("/day/<day>/<employee_id>/evidence.zip")
    def evidence_zip(day: str, employee_id: str):
        d, ev = _evidence(day, employee_id)
        store.log(service.now(), current_user(cfg), "export evidence", f"{d} {ev.name}")
        return Response(evidence_module.zip_bytes(ev, core.data_dir), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="evidence-{ev.email}-{d}.zip"'})

    @app.post("/sighting/<int:sighting_id>/<action>")
    def change_sighting(sighting_id: int, action: str):
        s = store.sighting(sighting_id)
        day = s.day.isoformat() if s else None
        learned = False
        try:
            if action == "move":
                learned = service.move_sighting(sighting_id, request.form.get("to", ""), current_user(cfg))
            elif action == "reject":
                service.reject_sighting(sighting_id, current_user(cfg))
            elif action == "restore":
                service.restore_sighting(sighting_id, current_user(cfg))
            else:
                abort(404)
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done({"move": "Sighting moved to the right person" + (", and their face learned." if learned else "."),
                     "reject": "Sighting removed.", "restore": "Sighting restored."}[action], "index", day=day)

    @app.post("/day/<day>/late-message")
    def send_late_message(day: str):
        d = _date(day, service.now().date())
        if not cfg.late_message.enabled:
            return failed("Turn on the late message in Settings first.", "index", day=day)
        if service.send_late_message(d, current_user(cfg)):
            return done("Late message posted to Google Chat.", "index", day=day)
        return done("Nobody was late, so no message was sent.", "index", day=day)

    @app.get("/unknowns")
    def unknowns():
        labels = _review_labels(store.all_employees())
        blurry_below = cfg.recognition.blurry_score / 100
        arrivals: dict = {}  # day -> employee id -> arrival time, for "in at 08:52" next to a suggestion

        def arrived(day):
            if day not in arrivals:
                arrivals[day] = {e: f"{c.first_seen.astimezone(service.tz):%H:%M}" for e, c in store.checkins(day).items()}
            return arrivals[day]

        groups = []
        for members in store.pending_groups():
            best, maybe = service.review_suggestions(members)
            scores = [u.quality for u in members if u.quality is not None]
            # Every photo on the card is poor (photos from before the score was kept never count as poor).
            blurry = members[-1].source == "camera" and len(scores) == len(members) and max(scores) < blurry_below
            camera_day = members[-1].ts.astimezone(service.tz).date() if members[-1].source == "camera" else None
            email = store.get(f"seed_review_email:{members[-1].id}") if members[-1].source == "seed" else None
            person = store.employee_by_email(email) if email else None
            photos = [u.image for u in reversed(members) if u.image]
            groups.append({"id": members[-1].id, "members": members, "latest": members[-1], "photos": photos,
                           "source": members[-1].source,
                           "expected_id": person.id if person and person.active else None,
                           "expected_name": _review_name(person.name, person.email) if person else _review_name("", email or ""),
                           "photo_urls": [{"id": u.id, "url": url_for("media", path=u.image) if u.image else None,
                                           "when": f"{u.ts.astimezone(service.tz):%a %d %b %H:%M:%S}"}
                                          for u in reversed(members)],
                           "body": next((body_photo(u.image) for u in reversed(members) if u.image
                                         and (core.data_dir / body_photo(u.image)).exists()), None),
                           "guess": best[0] if best else None, "guess_score": best[1] if best else None,
                           "maybe": maybe, "blurry": blurry, "in_at": arrived(camera_day) if camera_day else {}})
        # Cards that only need "Confirm" first, then those with likely people, then the rest; the
        # most similar first within each, newest first among equals. Camera faces before seed samples;
        # blurry camera faces after everything else, in a section of their own.
        groups.sort(key=lambda g: (g["blurry"], g["source"] != "camera", 0 if g["guess"] else 1 if g["maybe"] else 2,
                                   -(g["guess_score"] or (g["maybe"][0][1] if g["maybe"] else 0.0)),
                                   -g["latest"].ts.timestamp()))
        auto_labelled = []
        for u in store.unknowns(status="assigned", source="seed"):
            if store.get(f"seed_review_manual:{u.id}"):
                continue
            saved = store.get(f"seed_review_auto:{u.id}")
            if saved:
                person = store.employee(u.employee_id)
                if person:
                    auto_labelled.append({"id": u.id, "image": u.image,
                                          "name": _review_name(person.name, person.email), **json.loads(saved)})
        ignored = [{"id": g[-1].id, "count": len(g), "latest": g[-1],
                    "photo": next((u.image for u in reversed(g) if u.image), None)}
                   for g in store.ignored_groups(service.now().date())]
        # ?review=first (the link in chat) or ?review=<id> opens that group's review window at once.
        review = request.args.get("review", "")
        open_id = (next((g["id"] for g in groups if g["source"] == "camera"), None) if review == "first"
                   else (int(review) if review.isdigit() else None))
        return render_template("unknowns.html", groups=groups, ignored=ignored, employees=store.employees(),
                               auto_labelled=auto_labelled, employee_labels=labels,
                               local=lambda ts: ts.astimezone(service.tz),
                               no_photos=not core.gallery.counts(), open_id=open_id,
                               blurry_score=cfg.recognition.blurry_score)

    @app.post("/unknowns/<int:unknown_id>/reopen-seed")
    def reopen_seed(unknown_id: int):
        u = store.unknown(unknown_id)
        if (u is None or u.source != "seed" or not store.get(f"seed_review_auto:{unknown_id}")
                or store.get(f"seed_review_manual:{unknown_id}")):
            abort(404)
        if not store.reopen_seed(unknown_id, u.employee_id, service.now(), current_user(cfg)):
            return failed("This sample has already been handled.", "unknowns")
        return done("Sample returned to manual review. Import will preserve your choice.", "unknowns")

    def selected_photos(unknown_id: int) -> list[int]:
        if "photo_selection" in request.form or "photo_id" in request.form:
            try:
                return [int(value) for value in request.form.getlist("photo_id")]
            except ValueError:
                raise ValueError("Invalid photo selection.") from None
        face = store.unknown(unknown_id)
        return [] if face and len(store.group(face.group_id)) > 1 else [unknown_id]

    @app.post("/unknowns/<int:unknown_id>/assign")
    def assign(unknown_id: int):
        face = store.unknown(unknown_id)
        employee_id = request.form.get("employee_id", "")
        try:
            members = service.resolve_unknown(unknown_id, employee_id, current_user(cfg),
                                              photo_ids=selected_photos(unknown_id))
        except ValueError as exc:
            return reply(False, str(exc))
        if face and face.source == "seed":
            return reply(True, "Sample labelled. No attendance recorded; usable faces are learned after confirmation.")
        person = store.employee(employee_id)
        label = _review_name(person.name, person.email) if person else "employee"
        if face and face.source == "collect":
            return reply(True, f"Face learned for {label}. Seen outside the camera hours, so no attendance recorded.",
                         related=related(members, employee_id))
        return reply(True, f"Confirmed as {label}; face learned.", related=related(members, employee_id))

    @app.post("/unknowns/assign-many")
    def assign_many():
        """The cards selected on Review, or those offered as the same person, confirmed at once."""
        try:
            ids = [int(value) for value in request.form.getlist("unknown_id")]
        except ValueError:
            return reply(False, "Invalid review selection.")
        employee_id = request.form.get("employee_id", "")
        try:
            members = service.resolve_unknowns(ids, employee_id, current_user(cfg))
        except ValueError as exc:
            return reply(False, str(exc))
        person = store.employee(employee_id)
        label = _review_name(person.name, person.email) if person else "employee"
        cards = len({u.group_id for u in members})
        return reply(True, f"{cards} card{'s' if cards != 1 else ''} confirmed as {label}; faces learned.",
                     related=related(members, employee_id))

    @app.post("/unknowns/<int:unknown_id>/new-employee")
    def new_employee_from_face(unknown_id: int):
        face = store.unknown(unknown_id)
        email, name = request.form.get("email", ""), request.form.get("name", "").strip()
        try:
            employee_id, members = service.add_employee_from_unknown(unknown_id, email, name, current_user(cfg),
                                                                     photo_ids=selected_photos(unknown_id))
        except ValueError as exc:
            return reply(False, str(exc))
        label = _review_name(name, email.strip().lower())
        if face and face.source == "seed":
            return reply(True, f"{label} added and sample labelled. No attendance recorded; add reference photos if needed.")
        return reply(True, f"{label} added as a new employee and their face learned.",
                     related=related(members, employee_id))

    @app.post("/unknowns/<int:unknown_id>/ignore")
    def ignore(unknown_id: int):
        try:
            service.ignore_unknown(unknown_id, current_user(cfg))
        except ValueError as exc:
            return reply(False, str(exc))
        return reply(True, "Ignored for today. If this person comes back on another day, they will be shown again.")

    @app.post("/unknowns/delete")
    def delete_unknowns():
        try:
            ids = [int(value) for value in request.form.getlist("unknown_id")]
        except ValueError:
            return reply(False, "Invalid review selection.")
        try:
            service.delete_unknowns(ids, current_user(cfg))
        except ValueError as exc:
            return reply(False, str(exc))
        return reply(True, "Selected cards removed from review. No attendance recorded or faces learned.")

    @app.post("/unknowns/<int:unknown_id>/delete")
    def delete_unknown(unknown_id: int):
        try:
            photo_ids = selected_photos(unknown_id) if "photo_selection" in request.form or "photo_id" in request.form else None
            service.delete_unknown(unknown_id, current_user(cfg), photo_ids=photo_ids)
        except ValueError as exc:
            return reply(False, str(exc))
        return reply(True, "Removed from review. No attendance recorded or faces learned.")

    @app.post("/unknowns/<int:unknown_id>/restore")
    def restore(unknown_id: int):
        try:
            service.restore_unknown(unknown_id, current_user(cfg))
        except ValueError as exc:
            return failed(str(exc), "unknowns")
        return done("Restored to the review list.", "unknowns")
