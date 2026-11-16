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


def test_analysis_takes_the_camera_frame_nearest_the_due_time():
    from attendance.core.pipeline import frame_due

    gap = 1 / 15  # a 15 fps camera, 5 analyses a second wanted
    assert not frame_due(2 * gap, 0.0, 5, 15)  # 133 ms: too soon
    assert frame_due(3 * gap - 0.001, 0.0, 5, 15)  # 199 ms: the frame nearest 200 ms is due
    assert not frame_due(3 * gap - 0.001, 0.0, 5, 0.0)  # camera rate unknown: no slack
    assert frame_due(0.2, 0.0, 5, 0.0)


def test_updater_takes_the_highest_release_not_the_last_published(monkeypatch):
    import io
    import json

    from attendance import updater

    releases = [{"tag_name": "v1.1.34", "assets": [{"name": updater.asset_name(), "browser_download_url": "u34"}]},
                {"tag_name": "v1.1.35", "assets": [{"name": updater.asset_name(), "browser_download_url": "u35", "digest": "d"}]},
                {"tag_name": "v9.0.0", "prerelease": True, "assets": [{"name": updater.asset_name(), "browser_download_url": "u9"}]},
                {"tag_name": "v1.1.36", "assets": [{"name": "other.zip", "browser_download_url": "x"}]}]

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: Resp(json.dumps(releases).encode()))
    assert updater.latest_release("x/y") == ("1.1.35", "u35", "d")


def test_live_page_script_keeps_the_camera_index_apart_from_the_camera_badge(core):
    """1.1.33 named the camera index `cam` and the refresh function already declared `const cam`
    for the camera badge: every poll raised before the fetch and the page said "cannot reach the program"."""
    app, clock, sent = core
    page = client(app).get("/live").data.decode()
    script = page[page.index("const camIndex"):]
    assert "?cam=' + camIndex, {cache" in script and "?cam=' + camIndex + '&t='" in script
    for wrong in ("'?cam=' + cam,", "'?cam=' + cam +", "'?cam=' + cam;"):
        assert wrong not in script


def test_frames_that_arrive_in_a_burst_keep_the_times_they_were_taken():
    from attendance.vision.camera import StreamClock

    clock, gap = StreamClock(), 1 / 15
    # 15 frames a second arriving evenly, 60 ms after they were taken.
    even = [clock.at(1000.06 + i * gap, i * gap) for i in range(15)]
    assert all(abs(t - (1000.06 + i * gap)) < 0.01 for i, t in enumerate(even))
    # The link stalls for a second: its 15 frames all arrive within 30 ms of each other.
    burst = [clock.at(1002.06 + n * 0.002, (15 + n) * gap) for n in range(15)]
    assert all(abs(t - (1000.06 + (15 + n) * gap)) < 0.02 for n, t in enumerate(burst))  # a second of video
    assert burst == sorted(burst) and burst[-1] <= 1002.06 + 14 * 0.002  # in order, never after arrival
    # A stream without usable times (always 0): when each frame arrived.
    plain = StreamClock()
    assert [plain.at(5.0, 0.0), plain.at(5.1, 0.0), plain.at(5.2, None)] == [5.0, 5.1, 5.2]
    # The stream's clock jumps (the camera restarted its count): start again from the arrival.
    assert clock.at(1010.0, 2.5) == 1010.0


def test_a_burst_of_frames_is_analysed_in_turn_not_only_its_newest_frame():
    from attendance.core.pipeline import frame_due
    from attendance.vision.camera import pick

    gap = 1 / 15
    frames = [(object(), 100 + i * gap) for i in range(8)]  # what a 4K camera's buffer holds: 0.5 s
    last_processed = last_chosen = 100 - 0.2
    analysed = []
    while True:
        picked = pick(frames, last_chosen, lambda t: frame_due(t, last_processed, 5, 15))
        if picked is None:
            break
        _, last_chosen, last_processed = picked
        analysed.append(round(last_chosen - 100, 2))
    assert analysed == [0.0, 0.2, 0.4]  # 5 a second through the burst; only 0.47 was analysed before


def test_the_buffer_holds_fewer_frames_of_a_bigger_picture():
    import numpy as np

    from attendance.vision.camera import buffer_frames

    assert buffer_frames(np.zeros((2160, 3840, 3), np.uint8)) == 8  # 25 MB each
    assert buffer_frames(np.zeros((1080, 1920, 3), np.uint8)) == 16
    assert buffer_frames(np.zeros((4320, 7680, 3), np.uint8)) == 6


def test_brightness_is_judged_the_same_from_every_8th_pixel():
    import numpy as np

    from attendance.vision.recognizer import enhance_low_light

    rng = np.random.default_rng(3)
    bright = rng.integers(120, 200, (360, 640, 3), dtype=np.uint8)
    dark = rng.integers(10, 60, (360, 640, 3), dtype=np.uint8)
    assert enhance_low_light(bright) is bright
    out = enhance_low_light(dark)
    assert out is not dark and out.shape == dark.shape and out.mean() > dark.mean()


def test_a_long_burst_is_thinned_evenly_and_old_frames_are_dropped():
    from collections import deque

    from attendance.vision.camera import BACKLOG_S, trim

    gap = 1 / 15
    frames = deque()
    for i in range(24):  # 1.6 s of video arriving at once; room for 8
        frames.append((i, 100 + i * gap))
        trim(frames, 8)
    kept = [i for i, _ in frames]
    assert len(kept) == 8 and kept[0] == 0 and kept[-1] == 23
    assert max(b - a for a, b in zip(kept, kept[1:])) <= 5  # spread over the burst, no big hole
    frames.append((99, 100 + 23 * gap + BACKLOG_S + 1))  # the stream stood still, then one new frame
    trim(frames, 8)
    assert [i for i, _ in frames] == [99]
