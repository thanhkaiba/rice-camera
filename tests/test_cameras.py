"""Several cameras: the extra addresses, a picture per camera on the live page, and the last match."""
import numpy as np
import pytest

from attendance.config import Config
from attendance.core import settings
from attendance.vision.live import LiveStatus
from conftest import at, person_vector
from test_web import client


def test_camera_addresses_are_the_first_one_and_the_extra_lines():
    cfg = Config()
    cfg.camera.url = "rtsp://a/1"
    cfg.camera.more = "\n rtsp://b/1 \n\nrtsp://c/1\n"
    assert cfg.camera.urls() == ["rtsp://a/1", "rtsp://b/1", "rtsp://c/1"]
    cfg.validate()
    cfg.camera.more = "rtsp://a/1"
    with pytest.raises(ValueError, match="twice"):
        cfg.validate()
    cfg.camera.more = "\n".join(f"rtsp://x/{i}" for i in range(4))
    with pytest.raises(ValueError, match="at most"):
        cfg.validate()


def test_more_cameras_is_a_setting(core):
    app, clock, sent = core
    settings.save(app.cfg, app.store, {"camera": {"more": "rtsp://second/1\n"}})
    assert app.cfg.camera.urls()[-1] == "rtsp://second/1"
    assert settings.current(app.cfg)["camera"]["more"] == "rtsp://second/1"


def test_live_keeps_a_picture_per_camera_and_shows_the_one_asked_for():
    live = LiveStatus()
    first, second = np.zeros((2, 2, 3), np.uint8), np.ones((2, 2, 3), np.uint8)
    live.set_frame(b"one", first)  # the first camera, as before
    live.set_frame(b"two", second, camera=1)
    assert live.frame()[0] == b"one" and live.raw_frame().sum() == 0
    assert live.frame(1)[0] == b"two"  # asking for a camera makes it the viewed one
    assert live.viewed == 1 and live.raw_frame().sum() == 12
    assert live.frame(5)[0] is None


def test_live_json_lists_the_cameras_and_the_last_match(core):
    app, clock, sent = core
    live = app.live
    live.cameras = [{"index": 0, "label": "Camera 1", "state": "ok", "fps": 3.1, "source_fps": 15.0, "faces": []},
                    {"index": 1, "label": "Camera 2", "state": "connecting", "fps": 0.0, "source_fps": 0.0, "faces": []}]
    live.camera_state = "ok"
    live.last_match, live.last_identified = "Ann 0.52 at 09:01:02 (camera 2)", "Ann at 09:01:03 (camera 2)"
    c = client(app)
    s = c.get("/live.json?cam=1").get_json()
    assert s["cam"] == 1 and [x["label"] for x in s["cameras"]] == ["Camera 1", "Camera 2"]
    assert s["last_match"].startswith("Ann 0.52") and s["last_identified"].startswith("Ann at")
    assert c.get("/live.json?cam=x").get_json()["cam"] == 0
    app.cfg.camera.url, app.cfg.camera.more = "rtsp://a/1", "rtsp://b/1"
    page = c.get("/live").data.decode()
    assert 'data-cam="1"' in page and "Last match" in page and "Last check-in" in page
    status = c.get("/status").data.decode()
    assert "Camera 1: 3.1 of" in status and "Camera 2: 0.0 of" in status
    assert 'name="camera.more"' in c.get("/settings").data.decode()


def test_tracks_of_different_cameras_never_share_a_card(env):
    service, store, clock, sent = env
    v = person_vector(31)
    a = service.on_unknown(at(8, 30), "a.jpg", v, None, None, track=(0, 1))
    b = service.on_unknown(at(8, 30, 3), "b.jpg", v, None, None, track=(1, 1), follows=(1, 1))  # camera 2's track 1
    c = service.on_unknown(at(8, 30, 6), "c.jpg", v, None, None, track=(0, 2), follows=(0, 1))
    assert store.unknown(b).group_id == b
    assert store.unknown(c).group_id == store.unknown(a).group_id


def test_live_page_script_keeps_the_camera_index_apart_from_the_camera_badge(core):
    """1.1.33 named the camera index `cam` and the refresh function already declared `const cam`
    for the camera badge: every poll raised before the fetch and the page said "cannot reach the program"."""
    app, clock, sent = core
    page = client(app).get("/live").data.decode()
    script = page[page.index("const camIndex"):]
    assert "?cam=' + camIndex, {cache" in script and "?cam=' + camIndex + '&t='" in script
    for wrong in ("'?cam=' + cam,", "'?cam=' + cam +", "'?cam=' + cam;"):
        assert wrong not in script
