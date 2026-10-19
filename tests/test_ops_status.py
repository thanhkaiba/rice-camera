import io
import json
import os
import sys
import zipfile

import pytest

from attendance.cli import watchdog
from attendance.core import diagnostics, health, settings
from attendance.web import create_app
from conftest import DAY, at


def client(core_app):
    app = create_app(core_app)
    app.testing = True
    return app.test_client()


# status page -------------------------------------------------------------------------

def test_status_page_shows_camera_uptime_backup_and_photos(core):
    app, clock, _ = core
    clock.t = at(10, 30)
    app.store.mark_notice(DAY, "camera_ok", at(8, 0))
    app.store.add_outage(DAY, at(8, 30), at(9, 0))  # 30 of the 150 minutes so far
    app.service._last_alive = at(10, 30)
    rows = health.camera_uptime(app)
    assert rows[0]["day"] == DAY and rows[0]["down_min"] == 30 and rows[0]["share"] == pytest.approx(1 - 30 / 150)
    assert all(r["share"] is None for r in rows[1:])  # earlier days: the program did not run

    page = client(app).get("/status").get_data(as_text=True)
    assert "Status" in page and "80%" in page and "30 min down" in page
    assert "Download diagnostics" in page
    assert "Alice" in page  # one reference photo each: fewer than enough


def test_setup_banner_until_chat_and_backup_are_set(core, tmp_path):
    app, _, _ = core
    c = client(app)
    page = c.get("/").get_data(as_text=True)
    assert "No backup folder" in page and "Google Chat is not set up" not in page
    app.cfg.google_chat.webhook_url = ""
    assert "Google Chat is not set up" in c.get("/").get_data(as_text=True)
    app.cfg.google_chat.webhook_url = "https://chat.googleapis.com/x"
    app.cfg.backup.folder = str(tmp_path)
    assert 'class="alert error setup-warning"' not in c.get("/").get_data(as_text=True)


# settings source ------------------------------------------------------------------------

def test_saving_the_form_stores_only_what_differs_from_config_yaml(core):
    app, _, _ = core
    cfg, store = app.cfg, app.store
    settings.save(cfg, store, {"updates": {"enabled": False}, "camera": {"process_fps": 7}})
    saved = json.loads(store.get(settings.KEY))
    assert "updates" not in saved and saved["camera"] == {"process_fps": 7}


def test_override_is_shown_and_can_be_reset(core):
    app, _, _ = core
    cfg, store = app.cfg, app.store
    # Saved before this version: an override equal to the default, then config.yaml says otherwise.
    store.put(settings.KEY, json.dumps({"updates": {"enabled": False}, "web": {"password": "x"}}))
    getattr(cfg, settings.BASELINE)["updates"]["enabled"] = True
    settings.load(cfg, store)
    assert cfg.updates.enabled is False
    found = settings.overrides(cfg, store)
    assert found["updates.enabled"] == "on" and found["web.password"] == "empty"

    c = client(app)
    cfg.web.password = ""  # keep the page open without logging in
    settings.save(cfg, store, {"camera": {"process_fps": 7}})
    page = c.get("/settings").get_data(as_text=True)
    assert f"Overrides config.yaml ({getattr(cfg, settings.BASELINE)['camera']['process_fps']})" in page
    assert 'value="camera.process_fps"' in page and "override" in page
    c.post("/settings/reset", data={"reset": "camera.process_fps"})
    assert cfg.camera.process_fps == getattr(cfg, settings.BASELINE)["camera"]["process_fps"]
    assert "camera" not in json.loads(store.get(settings.KEY))
    assert c.post("/settings/reset", data={"reset": "nope.x"}).status_code == 302


# diagnostics ------------------------------------------------------------------------------

SECRETS = ["k3y", "t0ken", "camPass", "s3cret", "hrTok", "abc-uuid"]


def test_diagnostics_zip_has_logs_and_no_secrets(core):
    app, _, _ = core
    cfg, store = app.cfg, app.store
    data = app.data_dir
    (cfg.base_dir / "config.yaml").write_text("web:\n  password: s3cret\ncamera:\n  url: rtsp://admin:camPass@10.0.0.5:554/x\n")
    settings.save(cfg, store, {"google_chat": {"webhook_url": "https://chat.googleapis.com/v1/spaces/S/messages?key=k3y&token=t0ken"},
                               "camera": {"url": "rtsp://admin:camPass@10.0.0.5:554/x"},
                               "leave_api": {"url": "https://hr/api?token=hrTok", "token": "hrTok"},
                               "": {"healthcheck_url": "https://hc-ping.com/abc-uuid"}})
    (data / "service.log").write_text("Camera rtsp://admin:camPass@10.0.0.5 failed\nposting to ...?key=k3y&token=t0ken\n")
    (data / "attendance.log").write_text("Authorization: Bearer hrTok\nhello\n")
    app.store.add_outage(DAY, at(8, 0), at(8, 30))

    resp = client(app).get("/status/diagnostics.zip")
    assert resp.status_code == 200 and resp.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    names = set(z.namelist())
    assert {"system.json", "config.yaml", "settings-saved.json", "settings-effective.json", "update.json",
            "history.json", "status.json", "logs/service.log", "logs/attendance.log"} <= names
    everything = "".join(z.read(n).decode() for n in names)
    for secret in SECRETS:
        assert secret not in everything, secret
    assert "10.0.0.5" in everything and "chat.googleapis.com" in everything and "hello" in everything
    assert json.loads(z.read("history.json"))["outages"]


def test_diagnostics_command_writes_the_zip(core, tmp_path, monkeypatch):
    app, _, _ = core
    config = app.cfg.base_dir / "config.yaml"
    config.write_text("data_dir: data\nemployees_file: employees.csv\n")
    out = tmp_path / "d.zip"
    monkeypatch.setattr(sys, "argv", ["attendance", "--config", str(config), "diagnostics", str(out)])
    from attendance.__main__ import main

    main()
    assert "system.json" in zipfile.ZipFile(out).namelist()


# crash alerts --------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_crash_alert_after_repeated_stops_then_recovery_once():
    clock, posted = Clock(), []
    watch = watchdog.CrashWatch(posted.append, clock, host="pc")
    for _ in range(2):
        watch.started()
        clock.t += 60
        watch.exited(1)
    assert posted == []
    watch.started()
    clock.t += 60
    watch.exited(1)
    assert len(posted) == 1 and "keeps stopping (3 times" in posted[0] and "exit code 1" in posted[0]
    for _ in range(3):  # still crashing: no second alert within the hour
        watch.started()
        clock.t += 30
        watch.exited(1)
    assert len(posted) == 1
    watch.started()
    clock.t += 100
    watch.tick()
    assert len(posted) == 1
    clock.t += watchdog.STABLE_S
    watch.tick()
    watch.tick()
    assert len(posted) == 2 and "running again" in posted[1]


def test_slow_stops_are_not_an_alert():
    clock, posted = Clock(), []
    watch = watchdog.CrashWatch(posted.append, clock, host="pc")
    for _ in range(5):
        clock.t += watchdog.WITHIN_S
        watch.exited(1)
    assert posted == []


@pytest.mark.skipif(sys.platform == "win32", reason="pid liveness is not checked on Windows")
def test_unclean_stop_is_a_pid_file_left_by_a_dead_run(tmp_path):
    assert not watchdog.unclean_stop(tmp_path)
    (tmp_path / "running.pid").write_text(str(os.getpid()))
    assert not watchdog.unclean_stop(tmp_path)  # still running
    (tmp_path / "running.pid").write_text("999999 999998")
    assert watchdog.unclean_stop(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "service.log").write_text("2026-10-19 Restarting into the update: /x\n")
    assert not watchdog.unclean_stop(tmp_path)  # an older version handing over to an update


def test_chat_poster_uses_the_saved_webhook_and_queues_on_failure(core, monkeypatch):
    app, _, _ = core
    config = app.cfg.base_dir / "config.yaml"
    config.write_text("data_dir: data\nemployees_file: employees.csv\nweb:\n  public_url: http://pc:8000\n")
    settings.save(app.cfg, app.store, {"google_chat": {"webhook_url": "https://chat.example/hook"}})
    calls = []

    def fail(url, payload):
        calls.append((url, payload))
        raise OSError("offline")

    monkeypatch.setattr("attendance.integrations.notifier._http_post", fail)
    monkeypatch.setattr(watchdog.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: target(*args)})())
    watchdog.chat_poster(config)("hello")
    assert calls == [("https://chat.example/hook", {"text": "hello · http://pc:8000/status"})]
    assert [p for _, p, _ in app.store.outbox()] == [{"text": "hello · http://pc:8000/status"}]
