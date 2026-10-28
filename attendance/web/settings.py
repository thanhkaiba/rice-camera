"""Routes for live camera view (/live), system settings (/settings), and first-run setup (/setup)."""
from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from .. import __version__
from ..cli import updater
from ..core import latephoto, occasions, settings, transfer
from ..integrations import chat_api
from ..vision.faces import decode_image, encode_jpeg
from . import current_user, done, failed, first_setup, leave_status

if TYPE_CHECKING:
    from ..core.pipeline import App

SETUP_FIELDS = {"camera.url": ("camera", "url"), "web.password": ("web", "password"),
                "web.public_url": ("web", "public_url"), "google_chat.webhook_url": ("google_chat", "webhook_url"),
                "schedule.window_start": ("schedule", "window_start"),
                "schedule.late_after": ("schedule", "late_after"), "schedule.window_end": ("schedule", "window_end")}


def register(app: Flask, core: "App") -> None:
    cfg, store, service, live = core.cfg, core.store, core.service, core.live

    # live camera --------------------------------------------------------------------

    @app.get("/live")
    def live_page():
        return render_template("live.html", cfg=cfg, in_window=service.in_window(service.now()),
                               employees=service.shown_names(), enrol=core.enrolment.status(),
                               collect=core.collector.status(), cameras=cfg.camera.urls())

    @app.post("/live/enrol")
    def enrol_start():
        """Enrol at the door: the chosen person's next walks past the camera become reference photos."""
        action = request.form.get("action")
        if action == "stop":
            core.enrolment.stop()
            store.log(service.now(), current_user(cfg), "enrol at the door", "stopped")
            return redirect(url_for("live_page"))
        if action == "skip":
            step = core.enrolment.skip()
            if step:
                store.log(service.now(), current_user(cfg), "enrol at the door", f"skipped step: {step}")
            return redirect(url_for("live_page"))
        if action == "more":
            if not core.enrolment.resume():
                return failed("Nothing to continue; start again.", "live_page")
            store.log(service.now(), current_user(cfg), "enrol at the door", "continued for one more minute")
            return redirect(url_for("live_page"))
        if not cfg.camera.url:
            return failed("Set the camera address in Settings first.", "live_page")
        try:
            name = core.enrolment.start(request.form.get("employee_id", ""))
        except ValueError as exc:
            return failed(str(exc), "live_page")
        store.log(service.now(), current_user(cfg), "enrol at the door", name)
        return redirect(url_for("live_page"))

    def which_camera() -> int:
        """?cam=N on the live requests: which camera to show (0 = the first)."""
        try:
            return max(0, int(request.args.get("cam", 0)))
        except ValueError:
            return 0

    @app.get("/live.jpg")
    def live_jpg():
        jpeg, _ = live.frame(which_camera())
        if jpeg is None:
            abort(404)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/live/full.jpg")
    def live_full_jpg():
        """The camera picture at full resolution, unlabelled: to judge focus and sharpness."""
        live.frame(which_camera())  # counts as a viewer
        raw = live.raw_frame()
        if raw is None:
            abort(404)
        return Response(encode_jpeg(raw), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/live.json")
    def live_json():
        _, frame_time = live.frame(which_camera())  # counts as a viewer, so the camera loop keeps drawing frames
        return jsonify(camera=live.camera_state, problem=live.problem, fps=round(live.fps, 1), faces=live.faces,
                       people=live.people if cfg.tracking.mode == "person" else None, pose=core.pose.describe(),
                       last_identified=live.last_identified, last_match=live.last_match, previewing=live.previewing,
                       cameras=live.cameras, cam=live.viewed,
                       in_window=service.in_window(service.now()), enrol=core.enrolment.status(),
                       collect=core.collector.status(),
                       frame_age=round(time.time() - frame_time, 1) if frame_time else None)

    @app.get("/models.json")
    def models_json():
        return jsonify(status=live.models)

    @app.post("/camera/check")
    def camera_check():
        """Test a camera address typed on a page, before it is saved."""
        from ..vision.camera_check import check

        return jsonify(check(request.form.get("url", "")))

    @app.post("/live/area")
    def set_area():
        area = request.form.get("area", "").strip()
        try:
            settings.save(cfg, store, {"camera": {"area": area}})
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "live_page")
        store.log(service.now(), current_user(cfg), "change settings", f"camera.area = {cfg.camera.area or 'whole picture'}")
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
        return render_template("settings.html", s=settings.current(cfg), cfg=cfg, leave=leave_status(core),
                               overrides=settings.overrides(cfg, store),
                               late_background=latephoto.background_path(core.data_dir).exists(),
                               late_backgrounds=latephoto.own_pictures(core.data_dir), occasion_list=occasions.OCCASIONS,
                               occasion_names={o.key: o.name for o in occasions.OCCASIONS},
                               google=core.chat.api.status(),
                               occasion=occasions.occasion_on(service.now().date()),
                               upcoming=occasions.upcoming(service.now().date()),
                               pose=core.pose.describe(), version=__version__, packaged=getattr(sys, "frozen", False),
                               update=updater.read_status(core.data_dir), backup=core.backup.status(),
                               versions=versions())

    def versions() -> dict:
        """Versions on this computer, for going back to the previous one or forward again."""
        home = cfg.base_dir
        found = updater.programs(home)
        return dict(all=[v for v, _ in found], targets=[v for v, _ in updater.rollback_targets(home)],
                    pinned=updater.pinned(home) is not None,
                    newest=found[0][0] if found and updater.parse(found[0][0]) > updater.parse(__version__) else None)

    @app.post("/settings")
    def save_settings():
        form = request.form
        changes: dict = {}
        current = settings.current(cfg)
        for section, names in settings.EDITABLE.items():
            for name in names:
                key = f"{section}.{name}" if section else name
                value = current[section][name]
                if name in ("password", "token", "viewer_password"):
                    if form.get(key):
                        changes.setdefault(section, {})[name] = form[key]
                elif name == "work_days":
                    changes.setdefault(section, {})[name] = [int(d) for d in form.getlist(key)]
                elif isinstance(value, bool):
                    changes.setdefault(section, {})[name] = form.get(key) == "on"
                elif key in form:
                    changes.setdefault(section, {})[name] = form[key]
        if form.get("web.viewer_off") == "on":
            changes.setdefault("web", {})["viewer_password"] = ""
        try:
            settings.save(cfg, store, changes)
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "settings_page")
        store.log(service.now(), current_user(cfg), "change settings",
                  ", ".join(f"{s}.{n}" if s else n for s, f in changes.items() for n in f))
        return done("Settings saved and applied.", "settings_page")

    @app.post("/settings/reset")
    def reset_setting():
        """Back to the config.yaml value for one field (the button next to "overrides config.yaml")."""
        key = request.form.get("reset", "")
        try:
            settings.reset(cfg, store, key)
        except ValueError as exc:
            return failed(f"Not reset: {exc}", "settings_page")
        store.log(service.now(), current_user(cfg), "reset setting", key)
        return done(f"{key} now comes from config.yaml again.", "settings_page")

    @app.post("/settings/late-background")
    def late_background():
        """The background of the late message's picture: the admin's pictures, used every day (one
        picture), in turn (one a day) or around an occasion; or none (the bundled paintings, else drawn)."""
        single = latephoto.background_path(core.data_dir)
        remove = request.form.get("remove", "")
        if remove:
            if remove in ("single", "1"):
                single.unlink(missing_ok=True)
            else:
                try:
                    latephoto.own_picture_path(core.data_dir, remove).unlink()
                except (ValueError, OSError):
                    return failed("That picture is not there any more.", "settings_page")
            store.log(service.now(), current_user(cfg), "late message background", f"removed {remove}")
            return done("Picture removed; the next late picture is made without it.", "settings_page")
        uploads = [f for f in request.files.getlist("background") if f.filename]
        if not uploads:
            return failed("Choose a picture.", "settings_page")
        use = request.form.get("for", "single")
        if use not in ("single", "rotation") and use not in occasions.BY_KEY:
            return failed("Choose what the pictures are for.", "settings_page")
        try:
            images = [encode_jpeg(decode_image(f.read())) for f in uploads]
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "settings_page")
        if use == "single":
            single.parent.mkdir(parents=True, exist_ok=True)
            single.write_bytes(images[0])
            kept = [single.name]
        else:
            kept = [latephoto.add_own_picture(core.data_dir, jpeg, "" if use == "rotation" else use) for jpeg in images]
        store.log(service.now(), current_user(cfg), "late message background", f"{use}: " + ", ".join(kept))
        what = ("Background saved; the next picture uses it." if use == "single" else
                f"{len(kept)} picture(s) saved for " + ("the days in turn." if use == "rotation" else f"{occasions.BY_KEY[use].name}."))
        return done(what, "settings_page")

    @app.get("/settings/late-backgrounds/<path:rel>")
    def late_own_background(rel: str):
        try:
            path = latephoto.own_picture_path(core.data_dir, rel)
        except ValueError:
            abort(404)
        if not path.exists():
            abort(404)
        return send_from_directory(path.parent, path.name, max_age=0)

    @app.get("/settings/late-background.jpg")
    def late_background_jpg():
        path = latephoto.background_path(core.data_dir)
        if not path.exists():
            abort(404)
        return send_from_directory(core.data_dir, path.relative_to(core.data_dir).as_posix(), max_age=0)

    @app.post("/settings/update-now")
    def update_now():
        (core.data_dir / updater.REQUEST_FILE).touch()  # picked up by the launcher within half a minute
        store.log(service.now(), current_user(cfg), "check for updates", "")
        return done("Checking for a new version. If there is one, it is installed and the program restarts "
                    "by itself in a minute or two.", "settings_page")

    @app.post("/settings/go-back")
    def go_back():
        version = request.form.get("rollback.version", "")
        if not getattr(sys, "frozen", False) or version not in versions()["targets"]:
            return failed("That version cannot be started here.", "settings_page")
        (core.data_dir / updater.ROLLBACK_FILE).write_text(json.dumps({"version": version}), encoding="utf-8")
        store.log(service.now(), current_user(cfg), "go back to an earlier version", f"{__version__} -> {version}")
        return done(f"Going back to version {version}: the program restarts by itself within a minute. "
                    f"Version {__version__} is not installed again; a newer release is.", "settings_page")

    @app.post("/settings/go-forward")
    def go_forward():
        if not getattr(sys, "frozen", False):
            return failed("Only the installed program has versions to choose from.", "settings_page")
        (core.data_dir / updater.ROLLBACK_FILE).write_text(json.dumps({"version": ""}), encoding="utf-8")
        store.log(service.now(), current_user(cfg), "use the newest version again", __version__)
        return done("Back to automatic updates: the newest version starts within a minute.", "settings_page")

    @app.post("/settings/export-config")
    def export_config():
        if request.form.get("export.confirm") != "on":
            return failed("Tick the box to confirm that the file holds passwords and keys.", "settings_page")
        data = transfer.export(cfg, store, cfg.base_dir / "config.yaml")
        store.log(service.now(), current_user(cfg), "export configuration", "")
        name = f"face-attendance-configuration-{service.now():%Y-%m-%d}.json"
        return Response(json.dumps(data, ensure_ascii=False, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/settings/import-config")
    def import_config():
        upload = request.files.get("import.file")
        if not upload or not upload.filename:
            return failed("Choose a configuration file first.", "settings_page")
        try:
            data = json.loads(upload.read().decode("utf-8-sig"))
            backup = transfer.import_(cfg, store, cfg.base_dir / "config.yaml", data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return failed("Not imported: this is not a configuration file exported from this program.", "settings_page")
        except ValueError as exc:
            return failed(f"Not imported: {exc}.", "settings_page")
        store.log(service.now(), current_user(cfg), "import configuration", f"{upload.filename}; before: {backup.name}")
        if getattr(sys, "frozen", False):
            (core.data_dir / updater.RESTART_FILE).touch()  # the launcher restarts the program within a minute
            return done(f"Configuration imported; the program restarts by itself within a minute to apply all of it. "
                        f"The previous one is saved as {backup.name}.", "settings_page")
        return done(f"Configuration imported. Restart the program to apply all of it. "
                    f"The previous one is saved as {backup.name}.", "settings_page")

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
        if not core.backup.start(current_user(cfg)):
            return failed("A backup is already running.", "settings_page")
        store.log(service.now(), current_user(cfg), "backup now", cfg.backup.folder)
        return done("Backup started; the result shows here in a moment (reload the page).", "settings_page")

    @app.post("/settings/pose-reset")
    def reset_pose():
        core.pose.reset()
        store.log(service.now(), current_user(cfg), "relearn camera angle", "")
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

    # Google Chat API (the late message with its picture) -----------------------------------

    def google_redirect() -> str:
        # Desktop-app OAuth clients may only come back to this computer's loopback address.
        return f"http://127.0.0.1:{cfg.web.port}{chat_api.CALLBACK}"

    @app.post("/settings/google/client")
    def google_client():
        upload = request.files.get("client")
        if not upload or not upload.filename:
            return failed("Choose the JSON file first.", "settings_page")
        try:
            client = core.chat.api.set_client(upload.read(64 * 1024).decode("utf-8", "replace"))
        except ValueError as exc:
            return failed(f"Not loaded: {exc}.", "settings_page")
        store.log(service.now(), current_user(cfg), "google oauth client", client["project_id"] or client["client_id"][:12])
        return done("OAuth client loaded. Now press Connect and sign in.", "settings_page")

    @app.post("/settings/google/connect")
    def google_connect():
        try:
            return redirect(core.chat.api.start(google_redirect()))
        except ValueError as exc:
            return failed(f"Cannot connect: {exc}.", "settings_page")

    def google_finished(finish) -> Response:
        try:
            finish()
        except (ValueError, chat_api.ChatApiError) as exc:
            return failed(f"Sign-in not finished: {exc}.", "settings_page")
        store.log(service.now(), current_user(cfg), "google sign-in", core.chat.api.status()["space"] or "")
        return done("Signed in: the late message will be posted with its picture. Try Test.", "settings_page")

    @app.get(chat_api.CALLBACK)
    def google_callback():
        if request.args.get("error"):
            return failed(f"Google says: {request.args['error']}.", "settings_page")
        return google_finished(lambda: core.chat.api.finish(request.args.get("code", ""), request.args.get("state", "")))

    @app.post("/settings/google/paste")
    def google_paste():
        return google_finished(lambda: core.chat.api.finish_from_address(request.form.get("address", "")))

    @app.post("/settings/google/test")
    def google_test():
        import cv2
        import numpy as np

        picture = np.full((180, 320, 3), (60, 170, 240), np.uint8)
        cv2.putText(picture, "Test", (100, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 3, cv2.LINE_AA)
        try:
            core.chat.api.send("✅ The attendance system can post the late message here, with its picture attached.",
                               encode_jpeg(picture), "test.jpg")
        except chat_api.ChatApiError as exc:
            return failed(f"Sending failed: {exc}.", "settings_page")
        return done("Test message with a picture sent; check Google Chat.", "settings_page")

    @app.post("/settings/google/disconnect")
    def google_disconnect():
        core.chat.api.disconnect()
        store.log(service.now(), current_user(cfg), "google sign-out", "")
        return done("Signed out: the late message is posted with the webhook again.", "settings_page")

    # first-run setup ------------------------------------------------------------------

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
        store.log(service.now(), current_user(cfg), "change settings",
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
