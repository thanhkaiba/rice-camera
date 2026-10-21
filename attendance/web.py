"""Admin web page: attendance and corrections, late/absence report, employees and photos, live camera, settings."""
from __future__ import annotations

import csv
import datetime as dt
import hmac
import io
import re
import secrets
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from . import __version__, leave_api, settings, updater
from . import evidence as evidence_module
from . import setup as first_setup
from .icons import icon
from .service import SOURCES, minutes_late
from .store import LEAVE_KINDS, body_photo

if TYPE_CHECKING:
    from .pipeline import App

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_UPLOAD_MB = 50


def _date(value: str | None, default: dt.date) -> dt.date:
    if not value:
        return default
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        abort(400, "dates must be YYYY-MM-DD")


def _time(value: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        raise ValueError("enter the time as HH:MM") from None


def _next_id(people) -> str:
    """Suggest the next employee id, following the existing ones (NV007 -> NV008)."""
    best = None
    for p in people:
        m = re.fullmatch(r"(.*?)(\d+)", p.id)
        if m and (best is None or int(m.group(2)) > int(best.group(2))):
            best = m
    if best is None:
        return ""
    digits = best.group(2)
    return f"{best.group(1)}{int(digits) + 1:0{len(digits)}d}"


def create_app(core: "App") -> Flask:
    cfg, store, service, library, live = core.cfg, core.store, core.service, core.library, core.live
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    secret = store.get("web_secret")
    if not secret:
        secret = secrets.token_hex(32)
        store.put("web_secret", secret)
    app.secret_key = secret
    app.jinja_env.globals["icon"] = icon
    # Browsers keep the stylesheet and script until they change.
    static_dir = Path(app.static_folder or "")
    app.jinja_env.globals["asset_version"] = str(int(max((f.stat().st_mtime for f in static_dir.glob("*")), default=0)))

    def user() -> str:
        auth = request.authorization
        return auth.username if cfg.web.password and auth and auth.username else "admin"

    @app.before_request
    def guard():
        if cfg.web.password and request.endpoint != "late_photo":  # that one is guarded by its token
            auth = request.authorization
            if not (auth and hmac.compare_digest(auth.username or "", cfg.web.username)
                    and hmac.compare_digest(auth.password or "", cfg.web.password)):
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="attendance"'})
        if request.method == "POST":
            # Reject form posts coming from other websites (cross-site request forgery).
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin and urlparse(origin).netloc != request.host:
                abort(403)
        return None

    @app.context_processor
    def common():
        return {"pending": store.pending_group_count(), "nav": request.endpoint, "SOURCES": SOURCES,
                "setup_open": first_setup.open_count(core), "models_status": core.live.models,
                "translocated": sys.platform == "darwin" and "/AppTranslocation/" in sys.executable,
                "leave_label": lambda kind: LEAVE_KINDS.get(kind, kind), "WEEKDAYS": WEEKDAYS}

    def leave_status() -> dict:
        status = leave_api.read_status(store)
        for key in ("at", "last_success"):
            if status.get(key):
                status[key] = dt.datetime.fromisoformat(status[key]).astimezone(service.tz)
        return {"enabled": core.leave_sync.enabled, "warning": leave_api.warning(cfg, store, service.now()),
                **status}

    def late_message_status(day: dt.date) -> dict:
        lm = cfg.late_message
        if not lm.enabled:
            return {"enabled": False}
        sent = store.notice_time(day, "late_message")
        skipped = store.notice_time(day, "late_message_skipped")
        no_id = [a.name for a in service.late_people(day)
                 if not getattr(store.employee(a.employee_id), "chat_user", "")]
        # The picture is made (a slow step) for today and for days whose message went out, not for
        # every past day someone browses to.
        photo = service.late_photo_url(day) if day == service.now().date() or sent else None
        return {"enabled": True, "preview": service.late_text(day, preview=True),
                "photo": photo, "photo_public": bool(photo and photo.startswith("https://")),
                "sent": sent.astimezone(service.tz) if sent else None,
                "skipped": skipped.astimezone(service.tz) if skipped else None,
                "missing_chat": no_id, "send_at": lm.send_at or cfg.schedule.window_end, "auto": lm.auto_send}

    def deadline_status(day: dt.date) -> dict:
        """What happened at the late time: the not-seen list was posted, or skipped because everyone was in."""
        sent, skipped = store.notice_time(day, "deadline"), store.notice_time(day, "deadline_skipped")
        return {"sent": sent.astimezone(service.tz) if sent else None,
                "skipped": skipped.astimezone(service.tz) if skipped else None}

    def _back(endpoint: str, **kwargs):
        """Where to go after a form: the page it came from when it says so (setup forms), else `endpoint`."""
        back = request.form.get("back", "")
        if back.startswith("/") and not back.startswith("//"):
            return redirect(back)
        return redirect(url_for(endpoint, **kwargs))

    def done(message: str, endpoint: str, **kwargs):
        flash(message, "ok")
        return _back(endpoint, **kwargs)

    def failed(message: str, endpoint: str, **kwargs):
        flash(message, "error")
        return _back(endpoint, **kwargs)

    # attendance --------------------------------------------------------------------

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
                               leave=leave_status(), started=service.now() >= rep.window.start,
                               late_msg=late_message_status(day), deadline=deadline_status(day),
                               backup_warning=core.backup.warning(service.now()))

    @app.post("/day/<day>/<employee_id>/set")
    def set_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.set_arrival(d, employee_id, _time(request.form.get("time", "")), user())
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival saved.", "index", day=day)

    @app.post("/day/<day>/<employee_id>/reassign")
    def reassign_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            learned = service.reassign_arrival(d, employee_id, request.form.get("to", ""), user())
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival moved to the right person" + (", and their face learned." if learned else "."),
                    "index", day=day)

    @app.post("/day/<day>/<employee_id>/delete")
    def delete_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.delete_arrival(d, employee_id, user())
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
        store.log(service.now(), user(), "export evidence", f"{d} {ev.name}")
        return Response(evidence_module.zip_bytes(ev, core.data_dir), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="evidence-{employee_id}-{d}.zip"'})

    @app.post("/sighting/<int:sighting_id>/<action>")
    def change_sighting(sighting_id: int, action: str):
        s = store.sighting(sighting_id)
        day = s.day.isoformat() if s else None
        learned = False
        try:
            if action == "move":
                learned = service.move_sighting(sighting_id, request.form.get("to", ""), user())
            elif action == "reject":
                service.reject_sighting(sighting_id, user())
            elif action == "restore":
                service.restore_sighting(sighting_id, user())
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
        if service.send_late_message(d, user()):
            return done("Late message posted to Google Chat.", "index", day=day)
        return done("Nobody was late, so no message was sent.", "index", day=day)

    @app.get("/unknowns")
    def unknowns():
        groups = []
        for members in store.pending_groups():
            guesses = [u for u in members if u.guess_id in store.employees()]
            best = max(guesses, key=lambda u: u.guess_score or 0) if guesses else None
            photos = [u.image for u in reversed(members) if u.image][:6]
            groups.append({"id": members[-1].id, "members": members, "latest": members[-1], "photos": photos,
                           "photo_urls": [url_for("media", path=p) for p in photos],
                           "body": next((body_photo(u.image) for u in reversed(members) if u.image
                                         and (core.data_dir / body_photo(u.image)).exists()), None),
                           "guess": best.guess_id if best else None, "guess_score": best.guess_score if best else None})
        ignored = [{"id": g[-1].id, "count": len(g), "latest": g[-1],
                    "photo": next((u.image for u in reversed(g) if u.image), None)}
                   for g in store.ignored_groups(service.now().date())]
        # ?review=first (the link in chat) or ?review=<id> opens that group's review window at once.
        review = request.args.get("review", "")
        open_id = groups[0]["id"] if review == "first" and groups else (int(review) if review.isdigit() else None)
        return render_template("unknowns.html", groups=groups, ignored=ignored, employees=store.employees(),
                               local=lambda ts: ts.astimezone(service.tz), next_id=_next_id(store.all_employees()),
                               no_photos=not core.gallery.counts(), open_id=open_id)

    @app.post("/unknowns/<int:unknown_id>/assign")
    def assign(unknown_id: int):
        try:
            service.resolve_unknown(unknown_id, request.form.get("employee_id", ""), user())
        except ValueError as exc:
            return failed(str(exc), "unknowns")
        return done(f"Confirmed as {store.employees().get(request.form.get('employee_id', ''), '')}; face learned.", "unknowns")

    @app.post("/unknowns/<int:unknown_id>/new-employee")
    def new_employee_from_face(unknown_id: int):
        emp_id, name = request.form.get("id", "").strip(), request.form.get("name", "").strip()
        try:
            service.add_employee_from_unknown(unknown_id, emp_id, name, user())
        except ValueError as exc:
            return failed(str(exc), "unknowns")
        flash(f"{name} added as a new employee and their face learned.", "ok")
        return redirect(url_for("unknowns"))

    @app.post("/unknowns/<int:unknown_id>/ignore")
    def ignore(unknown_id: int):
        try:
            service.ignore_unknown(unknown_id, user())
        except ValueError as exc:
            return failed(str(exc), "unknowns")
        return done("Ignored for today. If this person comes back on another day, they will be shown again.",
                    "unknowns")

    @app.post("/unknowns/<int:unknown_id>/restore")
    def restore(unknown_id: int):
        try:
            service.restore_unknown(unknown_id, user())
        except ValueError as exc:
            return failed(str(exc), "unknowns")
        return done("Restored to the review list.", "unknowns")

    # employees ---------------------------------------------------------------------

    @app.get("/employees")
    def employees():
        counts = {}
        thumbs = {}
        for r in store.references(cfg.recognition.model_id()):
            counts[r.employee_id] = counts.get(r.employee_id, 0) + 1
            if r.image and r.employee_id not in thumbs:
                thumbs[r.employee_id] = r.image
        names = {e.id: e.name for e in store.all_employees()}
        return render_template("employees.html", people=store.all_employees(), counts=counts, thumbs=thumbs,
                               look_alikes=library.look_alikes(), names=names,
                               on_leave=store.leaves_on(service.now().date()))

    @app.post("/employees")
    def add_employee():
        try:
            store.add_employee(request.form.get("id", ""), request.form.get("name", ""))
        except ValueError as exc:
            return failed(str(exc), "employees")
        store.log(service.now(), user(), "add employee", f"{request.form['id']} {request.form['name']}")
        return done("Employee added. Now upload their photos.", "employee", employee_id=request.form["id"].strip())

    @app.post("/employees/import")
    def import_employees():
        file = request.files.get("file")
        if not file:
            return failed("Choose a CSV file.", "employees")
        try:
            n = store.import_employees(file.read().decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            return failed(f"Import failed: {exc}", "employees")
        library.reload()
        store.log(service.now(), user(), "import employees", f"{n} rows")
        return done(f"Imported {n} employees.", "employees")

    @app.get("/employees/<employee_id>")
    def employee(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        refs = store.references(cfg.recognition.model_id(), employee_id)
        return render_template("employee.html", person=person, refs=refs,
                               leaves=store.leaves(employee_id=employee_id, ending_after=service.now().date()))

    @app.post("/employees/<employee_id>")
    def update_employee(employee_id: str):
        store.employee(employee_id) or abort(404)
        try:
            store.update_employee(employee_id, request.form.get("name", ""), request.form.get("active") == "on",
                                  request.form.get("chat_user"), request.form.get("exempt") == "on")
        except ValueError as exc:
            return failed(str(exc), "employee", employee_id=employee_id)
        library.reload()
        store.log(service.now(), user(), "edit employee", f"{employee_id} {request.form.get('name')} "
                  f"active={request.form.get('active') == 'on'} exempt={request.form.get('exempt') == 'on'}")
        return done("Saved.", "employee", employee_id=employee_id)

    @app.post("/employees/<employee_id>/test-mention")
    def test_mention(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        if not cfg.google_chat.webhook_url:
            return failed("Set the Google Chat webhook in Settings first.", "employee", employee_id=employee_id)
        if not person.chat_user:
            return failed("Save the Google Chat user ID first.", "employee", employee_id=employee_id)
        try:
            core.chat.send_now(f"Test mention from the attendance system: {service.mention(employee_id)}")
        except Exception as exc:
            return failed(f"Sending failed: {exc}", "employee", employee_id=employee_id)
        return done(f"Test sent. Check in Google Chat that {person.name} is highlighted as a mention.",
                    "employee", employee_id=employee_id)

    @app.post("/employees/<employee_id>/photos")
    def upload_photos(employee_id: str):
        from .faces import decode_image

        store.employee(employee_id) or abort(404)
        files = [f for f in request.files.getlist("photos") if f.filename]
        if not files:
            return failed("Choose one or more photos.", "employee", employee_id=employee_id)
        try:
            core.recognizer()
        except Exception as exc:
            return failed(f"Face models are not available: {exc}", "employee", employee_id=employee_id)
        added = 0
        for f in files:
            try:
                result = library.add_photo(employee_id, decode_image(f.read()))
            except ValueError as exc:
                flash(f"{f.filename}: {exc}", "error")
                continue
            if result.ok:
                added += 1
            else:
                flash(f"{f.filename}: {result.message}", "error")
            for w in result.warnings:
                flash(f"{f.filename}: {w}", "warn")
        store.log(service.now(), user(), "upload photos", f"{employee_id}: {added} added")
        if added:
            flash(f"{added} photo(s) added.", "ok")
        return redirect(url_for("employee", employee_id=employee_id))

    @app.post("/employees/<employee_id>/photos/delete")
    def delete_photo(employee_id: str):
        refs = store.references(cfg.recognition.model_id(), employee_id)
        image = request.form.get("image", "")
        if request.form.get("ref_id"):  # a reference without a saved photo
            ref_id = int(request.form["ref_id"])
            if ref_id not in {r.id for r in refs}:
                abort(400)
            store.delete_reference(ref_id)
            library.reload()
            image = f"#{ref_id}"
        elif image in {r.image for r in refs}:
            library.delete_photo(image)
        else:
            abort(400)
        store.log(service.now(), user(), "delete photo", f"{employee_id} {image}")
        return done("Photo deleted.", "employee", employee_id=employee_id)

    @app.post("/employees/<employee_id>/delete")
    def delete_employee(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        if request.form.get("confirm") != employee_id:
            return failed("Type the employee id to confirm.", "employee", employee_id=employee_id)
        library.delete_employee(employee_id)
        store.log(service.now(), user(), "delete employee", f"{employee_id} {person.name}")
        return done(f"{person.name} and their face data were deleted.", "employees")

    # leave data and reports ------------------------------------------------------------

    @app.post("/leave/sync")
    def leave_sync():
        if not core.leave_sync.enabled:
            return failed("Set the HR system API address in Settings first.", "settings_page")
        status = core.leave_sync.sync()
        back = request.form.get("back") or url_for("index")
        flash(("Leave data updated: " if status["ok"] else "Leave data update failed: ") + status["message"],
              "ok" if status["ok"] else "error")
        return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("index"))

    def _period():
        today = service.now().date()
        start = _date(request.args.get("start"), today.replace(day=1))
        end = _date(request.args.get("end"), today)
        if end < start or (end - start).days > 366:
            abort(400, "choose an end date on or after the start date, at most one year later")
        return start, end

    @app.get("/report")
    def report():
        start, end = _period()
        today = service.now().date()
        first = today.replace(day=1)
        last_month_end = first - dt.timedelta(days=1)
        ranges = [("This month", first, today), ("Last month", last_month_end.replace(day=1), last_month_end),
                  ("Last 7 days", today - dt.timedelta(days=6), today), ("Last 30 days", today - dt.timedelta(days=29), today)]
        return render_template("report.html", rep=service.period_report(start, end), start=start, end=end,
                               ranges=ranges)

    @app.get("/accuracy")
    def accuracy():
        from .accuracy import accuracy_report

        today = service.now().date()
        start = _date(request.args.get("start"), today - dt.timedelta(days=29))
        end = _date(request.args.get("end"), today)
        if end < start:
            start, end = end, start
        ranges = [("Last 7 days", today - dt.timedelta(days=6), today),
                  ("Last 30 days", today - dt.timedelta(days=29), today),
                  ("Last 90 days", today - dt.timedelta(days=89), today)]
        return render_template("accuracy.html", acc=accuracy_report(store, core.gallery, start, end),
                               names=store.employees(), ranges=ranges)

    def _csv(name: str, header: list[str], rows) -> Response:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(rows)
        # The byte order mark makes Excel read accented names correctly.
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/report/summary.csv")
    def report_summary_csv():
        start, end = _period()
        rep = service.period_report(start, end)
        return _csv(f"attendance-summary-{start}-{rep.end}.csv",
                    ["employee_id", "name", "work_days", "on_time", "late", "late_minutes", "not_seen", "on_leave"],
                    [[p.employee_id, p.name, p.work_days, p.on_time, p.late, p.late_minutes, p.not_seen, p.on_leave]
                     for p in rep.people])

    @app.get("/report/details.csv")
    def report_details_csv():
        start, end = _period()
        rep = service.period_report(start, end)
        rows = []
        for d in rep.days:
            outage = "; ".join(f"camera down {a:%H:%M}-{b:%H:%M}" for a, b in d.outages)
            for a in d.on_time:
                rows.append([d.day, a.employee_id, a.name, "on_time", a.time, 0, SOURCES.get(a.source, a.source), outage])
            for a in d.late:
                rows.append([d.day, a.employee_id, a.name, "late", a.time, minutes_late(a, d.window),
                             SOURCES.get(a.source, a.source), outage])
            for emp_id, name in d.not_seen:
                rows.append([d.day, emp_id, name, "no_camera_data" if d.no_camera_data else "not_seen",
                             "", "", "", outage])
            for emp_id, name, kind in d.on_leave:
                rows.append([d.day, emp_id, name, "on_leave", "", "", kind, outage])
        return _csv(f"attendance-details-{start}-{rep.end}.csv",
                    ["date", "employee_id", "name", "status", "arrived", "minutes_late", "source_or_leave", "note"], rows)

    # live camera --------------------------------------------------------------------

    @app.get("/live")
    def live_page():
        return render_template("live.html", cfg=cfg, in_window=service.in_window(service.now()),
                               employees=store.employees(), enrol=core.enrolment.status())

    @app.post("/live/enrol")
    def enrol_start():
        """Enrol at the door: the chosen person's next walks past the camera become reference photos."""
        action = request.form.get("action")
        if action == "stop":
            core.enrolment.stop()
            store.log(service.now(), user(), "enrol at the door", "stopped")
            return redirect(url_for("live_page"))
        if action == "skip":
            step = core.enrolment.skip()
            if step:
                store.log(service.now(), user(), "enrol at the door", f"skipped step: {step}")
            return redirect(url_for("live_page"))
        if action == "more":
            if not core.enrolment.resume():
                return failed("Nothing to continue; start again.", "live_page")
            store.log(service.now(), user(), "enrol at the door", "continued for one more minute")
            return redirect(url_for("live_page"))
        if not cfg.camera.url:
            return failed("Set the camera address in Settings first.", "live_page")
        try:
            name = core.enrolment.start(request.form.get("employee_id", ""))
        except ValueError as exc:
            return failed(str(exc), "live_page")
        store.log(service.now(), user(), "enrol at the door", name)
        return redirect(url_for("live_page"))

    @app.get("/live.jpg")
    def live_jpg():
        jpeg, _ = live.frame()
        if jpeg is None:
            abort(404)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/live.json")
    def live_json():
        _, frame_time = live.frame()  # counts as a viewer, so the camera loop keeps drawing frames
        return jsonify(camera=live.camera_state, problem=live.problem, fps=round(live.fps, 1), faces=live.faces,
                       people=live.people if cfg.tracking.mode == "person" else None, pose=core.pose.describe(),
                       last_identified=live.last_identified, previewing=live.previewing,
                       in_window=service.in_window(service.now()), enrol=core.enrolment.status(),
                       frame_age=round(time.time() - frame_time, 1) if frame_time else None)

    @app.get("/models.json")
    def models_json():
        return jsonify(status=live.models)

    @app.post("/camera/check")
    def camera_check():
        """Test a camera address typed on a page, before it is saved."""
        from .camera_check import check

        return jsonify(check(request.form.get("url", "")))

    @app.post("/live/area")
    def set_area():
        area = request.form.get("area", "").strip()
        try:
            settings.save(cfg, store, {"camera": {"area": area}})
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "live_page")
        store.log(service.now(), user(), "change settings", f"camera.area = {cfg.camera.area or 'whole picture'}")
        return done("Detection area saved." if cfg.camera.area else "The whole picture is analysed again.", "live_page")

    @app.post("/live/preview")
    def preview():
        if request.form.get("action") == "stop":
            live.stop_preview()
        else:
            live.start_preview()
        return redirect(url_for("live_page"))

    # settings ---------------------------------------------------------------------

    @app.get("/settings")
    def settings_page():
        from . import latephoto

        return render_template("settings.html", s=settings.current(cfg), cfg=cfg, leave=leave_status(),
                               late_background=latephoto.background_path(core.data_dir).exists(),
                               pose=core.pose.describe(), version=__version__, packaged=getattr(sys, "frozen", False),
                               update=updater.read_status(core.data_dir), backup=core.backup.status())

    @app.post("/settings")
    def save_settings():
        form = request.form
        changes: dict = {}
        current = settings.current(cfg)
        for section, names in settings.EDITABLE.items():
            for name in names:
                key = f"{section}.{name}" if section else name
                value = current[section][name]
                if name in ("password", "token"):
                    if form.get(key):
                        changes.setdefault(section, {})[name] = form[key]
                elif name == "work_days":
                    changes.setdefault(section, {})[name] = [int(d) for d in form.getlist(key)]
                elif isinstance(value, bool):
                    changes.setdefault(section, {})[name] = form.get(key) == "on"
                elif key in form:
                    changes.setdefault(section, {})[name] = form[key]
        try:
            settings.save(cfg, store, changes)
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "settings_page")
        store.log(service.now(), user(), "change settings",
                  ", ".join(f"{s}.{n}" if s else n for s, f in changes.items() for n in f))
        return done("Settings saved and applied.", "settings_page")

    @app.post("/settings/late-background")
    def late_background():
        """The background of the late message's picture: a picture the admin chose, or none (drawn)."""
        from .faces import decode_image, encode_jpeg

        from . import latephoto

        path = latephoto.background_path(core.data_dir)
        if request.form.get("remove"):
            path.unlink(missing_ok=True)
            store.log(service.now(), user(), "late message background", "removed")
            return done("The drawn background is used again.", "settings_page")
        upload = request.files.get("background")
        if upload is None or not upload.filename:
            return failed("Choose a picture.", "settings_page")
        try:
            image = decode_image(upload.read())
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "settings_page")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_jpeg(image))
        store.log(service.now(), user(), "late message background", upload.filename)
        return done("Background saved; the next picture uses it.", "settings_page")

    @app.get("/settings/late-background.jpg")
    def late_background_jpg():
        from . import latephoto

        path = latephoto.background_path(core.data_dir)
        if not path.exists():
            abort(404)
        return send_from_directory(core.data_dir, path.relative_to(core.data_dir).as_posix(), max_age=0)

    @app.post("/settings/update-now")
    def update_now():
        (core.data_dir / updater.REQUEST_FILE).touch()  # picked up by the launcher within half a minute
        store.log(service.now(), user(), "check for updates", "")
        return done("Checking for a new version. If there is one, it is installed and the program restarts "
                    "by itself in a minute or two.", "settings_page")

    @app.post("/settings/backup-now")
    def backup_now():
        # The button sits in the settings form: use the folder as typed, saving it first.
        form = request.form
        changes = {n: form[f"backup.{n}"] for n in ("folder", "time", "keep_days") if f"backup.{n}" in form}
        if "backup.folder" in form:
            changes["include_photos"] = form.get("backup.include_photos") == "on"
        try:
            if changes:
                settings.save(cfg, store, {"backup": changes})
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "settings_page")
        if not cfg.backup.folder:
            return failed("Enter a backup folder first.", "settings_page")
        if not core.backup.start(user()):
            return failed("A backup is already running.", "settings_page")
        store.log(service.now(), user(), "backup now", cfg.backup.folder)
        return done("Backup started; the result shows here in a moment (reload the page).", "settings_page")

    @app.post("/settings/pose-reset")
    def reset_pose():
        core.pose.reset()
        store.log(service.now(), user(), "relearn camera angle", "")
        return done("The camera angle will be learned again from the next faces.", "settings_page")

    @app.post("/settings/test-chat")
    def test_chat():
        # Test the address typed in the form, so it can be checked before saving.
        url = (request.form.get("google_chat.webhook_url") or cfg.google_chat.webhook_url).strip()
        if not url:
            return failed("Enter a Google Chat webhook URL first.", "settings_page")
        try:
            core.chat.send_now("✅ The attendance system can post to this space.", url)
        except Exception as exc:
            return failed(f"Sending failed: {exc}", "settings_page")
        return done("Test message sent; check Google Chat.", "settings_page")

    # first-run setup ------------------------------------------------------------------

    SETUP_FIELDS = {"camera.url": ("camera", "url"), "web.password": ("web", "password"),
                    "web.public_url": ("web", "public_url"), "google_chat.webhook_url": ("google_chat", "webhook_url"),
                    "schedule.window_start": ("schedule", "window_start"),
                    "schedule.late_after": ("schedule", "late_after"), "schedule.window_end": ("schedule", "window_end")}

    @app.get("/setup")
    def setup_page():
        step = request.args.get("step") or first_setup.wizard_step(core)
        if step not in first_setup.WIZARD:
            abort(404)
        return render_template("setup.html", steps=first_setup.steps(core), cfg=cfg, step=step,
                               wizard=first_setup.WIZARD, hours_done=store.get(first_setup.HOURS_KEY) == "1",
                               suggested_url=first_setup.lan_address(cfg.web.port))

    @app.post("/setup/save")
    def setup_save():
        changes: dict = {}
        for key, (section, name) in SETUP_FIELDS.items():
            value = request.form.get(key)
            if value and value.strip():  # empty fields are left as they are
                changes.setdefault(section, {})[name] = value.strip()
        if "schedule.days_shown" in request.form:  # the working hours step (no box ticked = no work days)
            changes.setdefault("schedule", {})["work_days"] = [int(d) for d in request.form.getlist("schedule.work_days")]
        if not changes:
            return failed("Nothing to save.", "setup_page")
        try:
            settings.save(cfg, store, changes)
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "setup_page")
        if "schedule" in changes:
            store.put(first_setup.HOURS_KEY, "1")
        store.log(service.now(), user(), "change settings",
                  ", ".join(f"{s}.{n}" for s, f in changes.items() for n in f))
        return done("Saved.", "setup_page")

    @app.post("/setup/<action>")
    def setup_action(action: str):
        if action == "skip-employees":
            store.put(first_setup.SKIP_EMPLOYEES_KEY, "1")
            return done("The camera will collect faces; name them on the Review page.", "setup_page")
        if action == "skip-chat":
            store.put(first_setup.SKIP_CHAT_KEY, "1")
            return redirect(url_for("setup_page", step="done"))
        if action == "later":
            store.put(first_setup.DISMISSED_KEY, "1")
            return done("You can finish the setup any time from Settings.", "index")
        if action == "check-camera":
            live.start_preview()
            return redirect(url_for("live_page"))
        abort(404)

    @app.get("/audit")
    def audit():
        return render_template("audit.html", rows=store.audit(), local=lambda ts: ts.astimezone(service.tz))

    @app.get("/late-photo/<day>/<token>.jpg")
    def late_photo(day: str, token: str):
        """The picture of the day's late people, for Google Chat: no password, the token in the
        address (only in the message and on the Today page) stands in for it."""
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            abort(404)
        if not hmac.compare_digest(token, service.late_photo_token(d)):
            abort(404)
        rel = service.late_photo(d)
        if rel is None:
            abort(404)
        return send_from_directory(core.data_dir, rel, max_age=0)

    @app.get("/media/<path:path>")
    def media(path: str):
        if not (path.startswith("crops/") or path.startswith("faces/")) or ".." in Path(path).parts:
            abort(404)
        return send_from_directory(core.data_dir, path)

    return app
