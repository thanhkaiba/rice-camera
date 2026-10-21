import io
import json
import sys

import pytest

from attendance import settings, updater
from attendance.core import transfer


def config_file(app, text="timezone: Asia/Ho_Chi_Minh\ndata_dir: data\nweb:\n  port: 8000\n"):
    path = app.cfg.base_dir / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_export_holds_config_yaml_and_saved_settings(core):
    app, _, _ = core
    path = config_file(app)
    settings.save(app.cfg, app.store, {"camera": {"area": "0.1,0.1,0.9,0.9"}, "web": {"password": "s3cret"}})
    data = transfer.export(app.cfg, app.store, path)
    assert data["format"] == transfer.FORMAT and "port: 8000" in data["config_yaml"]
    assert data["settings"]["camera"]["area"] == "0.1,0.1,0.9,0.9" and data["settings"]["web"]["password"] == "s3cret"


def test_import_replaces_the_configuration_and_keeps_this_computers_folder_and_address(core):
    app, _, _ = core
    path = config_file(app)
    settings.save(app.cfg, app.store, {"web": {"public_url": "http://this-pc:8000"},
                                       "schedule": {"late_after": "09:30"}})
    other = {"format": transfer.FORMAT, "version": 1,
             "config_yaml": "timezone: Asia/Ho_Chi_Minh\ndata_dir: elsewhere\nweb:\n  port: 8100\n",
             "settings": {"schedule": {"window_start": "07:30", "late_after": "09:00", "window_end": "10:00"},
                          "camera": {"area": "0.2,0,1,1"}, "web": {"public_url": "http://other-pc:8000"}}}
    backup = transfer.import_(app.cfg, app.store, path, other)

    saved = json.loads(app.store.get(settings.KEY))
    assert saved["camera"]["area"] == "0.2,0,1,1" and saved["schedule"]["late_after"] == "09:00"
    assert saved["web"]["public_url"] == "http://this-pc:8000"
    assert app.cfg.schedule.late_after == "09:00"  # applied right away where possible
    written = path.read_text()
    assert "8100" in written and "elsewhere" not in written and "data_dir: data" in written
    before = json.loads(backup.read_text())
    assert before["settings"]["schedule"]["late_after"] == "09:30" and "port: 8000" in before["config_yaml"]
    assert json.loads(app.store.get(transfer.BACKUP_KEY)) == before


@pytest.mark.parametrize("data, message", [
    ({"format": "something else"}, "not a configuration file"),
    ({"format": transfer.FORMAT, "version": 2}, "not supported"),
    ({"format": transfer.FORMAT, "version": 1, "config_yaml": "nonsense_key: 1\n"}, "not valid"),
    ({"format": transfer.FORMAT, "version": 1, "settings": {"schedule": {"late_after": "25:00"}}}, "not valid here"),
])
def test_a_wrong_file_is_refused_and_changes_nothing(core, data, message):
    app, _, _ = core
    path = config_file(app)
    with pytest.raises(ValueError, match=message):
        transfer.import_(app.cfg, app.store, path, data)
    assert app.store.get(transfer.BACKUP_KEY) is None and "port: 8000" in path.read_text()


def test_export_and_import_on_the_settings_page(core, monkeypatch):
    from attendance.web import create_app

    app, _, _ = core
    config_file(app)
    settings.save(app.cfg, app.store, {"camera": {"area": "0.1,0.1,0.9,0.9"}})
    c = create_app(app).test_client()
    assert c.post("/settings/export-config").status_code == 302  # the confirmation box is required
    r = c.post("/settings/export-config", data={"export.confirm": "on"})
    assert "attachment" in r.headers["Content-Disposition"]
    exported = r.get_data()

    settings.save(app.cfg, app.store, {"camera": {"area": ""}})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    r = c.post("/settings/import-config", data={"import.file": (io.BytesIO(exported), "conf.json")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and app.cfg.camera.area == "0.1,0.1,0.9,0.9"
    assert (app.data_dir / updater.RESTART_FILE).exists()  # the launcher restarts the program

    c.post("/settings/import-config", data={"import.file": (io.BytesIO(b"not json"), "x.json")},
           content_type="multipart/form-data")
    assert app.cfg.camera.area == "0.1,0.1,0.9,0.9"


def test_export_and_import_from_the_command_line(tmp_path, monkeypatch):
    from attendance.__main__ import main

    path = tmp_path / "config.yaml"
    path.write_text("timezone: Asia/Ho_Chi_Minh\n", encoding="utf-8")
    out = tmp_path / "conf.json"
    monkeypatch.setattr(sys, "argv", ["attendance", "--config", str(path), "export-config", str(out)])
    main()
    data = json.loads(out.read_text())
    data["settings"] = {"schedule": {"late_after": "09:15"}}
    out.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", ["attendance", "--config", str(path), "import-config", str(out)])
    main()
    monkeypatch.setattr(sys, "argv", ["attendance", "--config", str(path), "export-config", str(out)])
    main()
    assert json.loads(out.read_text())["settings"]["schedule"]["late_after"] == "09:15"
