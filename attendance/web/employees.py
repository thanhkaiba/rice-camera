"""Routes for managing employees, CSV import, reference photo uploads, and @mention tests."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from . import current_user, done, failed

if TYPE_CHECKING:
    from ..core.pipeline import App

SEED_UPLOAD_MB = 1024


def register(app: Flask, core: "App") -> None:
    cfg, store, service, library = core.cfg, core.store, core.service, core.library

    @app.get("/employees")
    def employees():
        from ..cli import seed
        from ..vision.faces import GOOD_PHOTOS

        counts = {}
        thumbs = {}
        scores = {}
        quality_report = library.saved_quality()
        checks = quality_report["checks"]
        recognition = {}
        for r in store.references(cfg.recognition.model_id()):
            counts[r.employee_id] = counts.get(r.employee_id, 0) + 1
            if r.image and r.employee_id not in thumbs:
                thumbs[r.employee_id] = r.image
            if r.quality_score is not None:
                scores.setdefault(r.employee_id, []).append(r.quality_score)
            check = checks.get(r.id)
            if check:
                summary = recognition.setdefault(r.employee_id, {"useful": 0, "review": 0, "unverified": 0, "duplicates": 0})
                summary[check.status] += 1
                summary["duplicates"] += check.duplicate_of is not None
        names = service.shown_names(active_only=False)
        people = sorted(store.all_employees(), key=lambda e: (not e.active, names[e.id].lower()))
        return render_template("employees.html", people=people, counts=counts, thumbs=thumbs,
                               look_alikes=quality_report["look_alikes"], names=names, quality=library.quality(),
                               good_photos=GOOD_PHOTOS, photo_scores=scores, recognition=recognition,
                               quality_report=quality_report,
                               on_leave=store.leaves_on(service.now().date()),
                               seed_status=seed.read_status(core), in_window=service.in_window(service.now()))

    @app.post("/employees/quality")
    def reevaluate_quality():
        employee_id = request.form.get("employee_id")
        if employee_id and not store.employee(employee_id):
            abort(404)
        endpoint = "employee" if employee_id else "employees"
        back = {"employee_id": employee_id} if employee_id else {}
        try:
            count = library.reevaluate_quality()
        except ValueError as exc:
            return failed(f"Quality evaluation failed: {exc}", endpoint, **back)
        store.log(service.now(), current_user(cfg), "evaluate photo quality", f"{count} reference photos")
        return done(f"Quality evaluated for {count} reference photos.", endpoint, **back)

    @app.post("/employees")
    def add_employee():
        try:
            employee_id = store.add_employee(request.form.get("email", ""), request.form.get("name", ""))
        except ValueError as exc:
            return failed(str(exc), "employees")
        store.log(service.now(), current_user(cfg), "add employee", f"{request.form['email']} {request.form['name']}")
        return done("Employee added. Now upload their photos.", "employee", employee_id=employee_id)

    @app.post("/employees/import")
    def import_employees():
        file = request.files.get("file")
        if not file:
            return failed("Choose a CSV file.", "employees")
        try:
            n = store.import_employees(file.read().decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            return failed(f"Import failed: {exc}", "employees")
        store.put("employee_email_csv_imported", "1")
        library.reload()
        store.log(service.now(), current_user(cfg), "import employees", f"{n} rows")
        return done(f"Imported {n} employees.", "employees")

    @app.post("/employees/seed")
    def import_seed():
        from ..cli import seed

        request.max_content_length = SEED_UPLOAD_MB * 1024 * 1024  # a seed is bigger than a few photos
        file = request.files.get("file")
        if not file or not file.filename:
            return failed("Choose the seed .zip file.", "employees")
        folder = core.data_dir / "seed-upload"
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / f"{service.now():%Y%m%d-%H%M%S}.zip"
        file.save(archive)
        try:
            seed.start(core, archive, review_only=request.form.get("review_only") == "on", user=current_user(cfg))
        except ValueError as exc:
            archive.unlink(missing_ok=True)
            return failed(f"Seed import not started: {exc}.", "employees")
        return done("Seed import started. The camera is paused until it finishes (a few minutes).", "employees")

    @app.get("/employees/seed.json")
    def seed_status():
        from ..cli import seed

        return jsonify(seed.read_status(core))

    @app.get("/employees/<employee_id>")
    def employee(employee_id: str):
        from ..vision.faces import GOOD_PHOTOS

        person = store.employee(employee_id) or abort(404)
        refs = store.references(cfg.recognition.model_id(), employee_id)
        weeks = {}
        other_photos = []
        for ref in refs:
            if ref.source.split(':')[0] not in {'admin', 'capture'}:
                other_photos.append(ref)
                continue
            if ref.created_at.tzinfo is None:
                ref.created_at = ref.created_at.replace(tzinfo=service.tz)
            ref.created_at = ref.created_at.astimezone(service.tz)
            day = ref.created_at.date()
            start = day - dt.timedelta(days=day.weekday())
            weeks.setdefault(start, []).append(ref)
        photo_groups = [(None, None, other_photos)] if other_photos else []
        photo_groups += [(start, start + dt.timedelta(days=6),
                          sorted(weeks[start], key=lambda r: (r.created_at.timestamp(), r.id), reverse=True))
                         for start in sorted(weeks, reverse=True)]
        quality_report = library.saved_quality()
        return render_template("employee.html", person=person, refs=refs, photo_groups=photo_groups, good_photos=GOOD_PHOTOS,
                               good=sum(r.quality_ok is True for r in refs),
                               checks=quality_report["checks"], quality_report=quality_report,
                               recognition_params=cfg.recognition,
                               look_alikes=[(p.b if p.a == employee_id else p.a, p.score)
                                            for p in quality_report["look_alikes"] if employee_id in (p.a, p.b)],
                               names=service.shown_names(active_only=False),
                               leaves=store.leaves(employee_id=employee_id, ending_after=service.now().date()))

    @app.post("/employees/<employee_id>")
    def update_employee(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        try:
            store.update_employee(employee_id, request.form.get("name", ""), request.form.get("active") == "on",
                                  request.form.get("chat_user"), request.form.get("exempt") == "on",
                                  email=request.form.get("email", person.email))
        except ValueError as exc:
            return failed(str(exc), "employee", employee_id=employee_id)
        library.reload()
        store.log(service.now(), current_user(cfg), "edit employee", f"{store.employee(employee_id).email} {request.form.get('name')} "
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
        from ..vision.faces import decode_image

        person = store.employee(employee_id) or abort(404)
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
        store.log(service.now(), current_user(cfg), "upload photos", f"{person.email}: {added} added")
        if added:
            flash(f"{added} photo(s) added.", "ok")
        return redirect(url_for("employee", employee_id=employee_id))

    @app.post("/employees/<employee_id>/photos/delete")
    def delete_photo(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        refs = store.references(cfg.recognition.model_id(), employee_id)
        if "below_score" in request.form:
            try:
                threshold = float(request.form["below_score"])
            except ValueError:
                abort(400)
            if not 0 <= threshold <= 100 or not threshold.is_integer():
                abort(400)
            selected = [r for r in refs if r.image_score is not None and r.image_score < threshold]
            if not selected:
                return done(f"No photos below {int(threshold)}/100. Nothing deleted.", "employee", employee_id=employee_id)
        elif "ref_id" in request.form:
            try:
                ids = {int(value) for value in request.form.getlist("ref_id")}
            except ValueError:
                abort(400)
            if not ids or not ids <= {r.id for r in refs}:
                abort(400)
            selected = [r for r in refs if r.id in ids]
        else:
            selected = [r for r in refs if r.image and r.image == request.form.get("image")]
        if not selected:
            abort(400)
        for ref in selected:
            if ref.image:
                library.delete_photo(ref.image)
            else:
                store.delete_reference(ref.id)
            store.log(service.now(), current_user(cfg), "delete photo", f"{person.email} {ref.image or f'#{ref.id}'}")
        library.reload()
        message = "Photo deleted." if len(selected) == 1 else f"{len(selected)} photos deleted."
        return done(message, "employee", employee_id=employee_id)

    @app.post("/employees/<employee_id>/delete")
    def delete_employee(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        if not person.email or request.form.get("confirm", "").strip().lower() != person.email:
            return failed("Type the employee email to confirm.", "employee", employee_id=employee_id)
        library.delete_employee(employee_id)
        store.log(service.now(), current_user(cfg), "delete employee", f"{person.email} {person.name}")
        return done(f"{person.name} and their face data were deleted.", "employees")
