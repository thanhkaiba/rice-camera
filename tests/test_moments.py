"""A picture a second during the camera hours (recorder.py), played back on the Moments page."""
import datetime as dt
import json

import numpy as np
import pytest

from attendance.core import recorder
from conftest import DAY, at


def test_one_picture_a_second_is_kept_shrunk_and_old_days_go(core, tmp_path):
    app, _, _ = core
    rec = recorder.Recorder(app.cfg, tmp_path)
    frame = np.zeros((2160, 3840, 3), dtype=np.uint8)
    assert rec.offer(frame, at(9, 0), 100.0) and not rec.offer(frame, at(9, 0), 100.5)  # one a second
    rec._write(frame, at(9, 0))
    rec._write(frame, at(9, 0, 3))
    kept = recorder.recorded(tmp_path, DAY, dt.time(8, 59, 59), dt.time(9, 0, 2))
    assert [t for t, _ in kept] == ["09:00:00"]
    import cv2
    assert cv2.imread(str(kept[0][1])).shape[:2] == (720, 1280)
    old = recorder.day_folder(tmp_path, DAY - dt.timedelta(days=3)); old.mkdir(parents=True); (old / "090000.jpg").write_bytes(b"x")
    sent = tmp_path / "crops" / (DAY - dt.timedelta(days=3)).isoformat(); sent.mkdir(parents=True)
    (sent / "rec_090000.jpg").write_bytes(b"x"); (sent / "090000_1_unknown_" ).write_bytes(b"keep")
    recorder.expire(tmp_path, DAY, 3)
    assert not old.exists() and not (sent / "rec_090000.jpg").exists() and (sent / "090000_1_unknown_").exists()
    assert recorder.day_folder(tmp_path, DAY).exists()
    app.cfg.camera.record_days = 0
    assert not rec.offer(frame, at(9, 1), 200.0)  # off


def test_the_office_computer_sends_a_span(core, monkeypatch):
    app, _, _ = core
    edge = app.edge
    edge.data_dir = app.data_dir
    folder = recorder.day_folder(app.data_dir, DAY); folder.mkdir(parents=True)
    for s in ("085959", "090000", "090010", "090100"):
        (folder / f"{s}.jpg").write_bytes(b"\xff\xd8" + s.encode())
    sent = []
    monkeypatch.setattr(edge, "_send", lambda method, path, body, ctype: sent.append((method, path, body)) or (200, b"{}"))
    result = edge._moment({"day": DAY.isoformat(), "start": "09:00:00", "end": "09:00:30"})
    assert result["ok"] and result["frames"] == 2
    assert [p for _, p, _ in sent] == [f"/api/edge/photos/crops/{DAY.isoformat()}/rec_090000.jpg",
                                       f"/api/edge/photos/crops/{DAY.isoformat()}/rec_090010.jpg"]
    assert not edge._moment({"day": DAY.isoformat(), "start": "09:00:00", "end": "09:10:00"})["ok"]  # too long


def test_moments_page_shows_the_seconds_and_the_scenes(core):
    from attendance.web import create_app
    app, _, _ = core
    client = create_app(app).test_client()
    crops = app.data_dir / "crops" / DAY.isoformat(); crops.mkdir(parents=True)
    (crops / "rec_090005.jpg").write_bytes(b"\xff\xd8")
    (crops / f"090003_123456_A_{'a' * 32}_scene.jpg").write_bytes(b"\xff\xd8")
    (crops / f"090007_123456_unknown_{'b' * 32}_scene.jpg").write_bytes(b"\xff\xd8")
    (crops / "crowd_090006_abcdef123456.jpg").write_bytes(b"\xff\xd8")
    (crops / f"091000_123456_A_{'c' * 32}_scene.jpg").write_bytes(b"\xff\xd8")  # outside the span
    app.service.greeter = None  # as on the server: the recording is on the office computer
    page = client.get(f"/moments?day={DAY}&at=09:00:05&span=20").get_data(as_text=True)
    assert "Alice" in page and "unidentified face" in page and "group" in page and "091000" not in page
    assert "rec_090005.jpg" in page and "m-slider" in page
    page = client.get(f"/moments?day={DAY}&at=08:00:00").get_data(as_text=True)
    assert "Fetch from the office computer" in page
    token = __import__("re").search(r'name="csrf-token" content="([^"]*)"', page)
    form = {"day": DAY.isoformat(), "start": "07:59:40", "end": "08:00:20", "at": "08:00:00", "span": "20",
            **({"csrf_token": token.group(1)} if token else {})}
    assert client.post("/moments/fetch", data=form).status_code == 302
    job = app.store.edge_jobs()[-1]
    assert job["kind"] == "moment" and job["payload"]["start"] == "07:59:40"
    assert "arrive within a minute" in client.get(f"/moments?day={DAY}&at=08:00:00").get_data(as_text=True)
