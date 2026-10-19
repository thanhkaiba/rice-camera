"""Routes for live camera view (/live), system settings (/settings), and first-run setup (/setup)."""
from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from .. import __version__
from ..cli import updater
from ..core import settings, transfer
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
        return render_template("live.html", cfg=cfg, in_window=service.in_window(service.now()))

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
                       in_window=service.in_window(service.now()),
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
        store.log(service.now(), current_user(cfg), "change settings",
                  ", ".join(f"{s}.{n}" if s else n for s, f in changes.items() for n in f))
        return done("Settings saved and applied.", "settings_page")

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
