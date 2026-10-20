import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from attendance import config
from attendance.evaluate import Sample, recommend, sweep
from attendance.gallery import Gallery
from attendance.notifier import GoogleChat
from attendance.store import Store

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)


def test_gallery_uses_mean_of_two_best_references():
    a1, a2, b = np.eye(3, dtype=np.float32)
    g = Gallery([("A", a1), ("A", a2), ("B", b)])
    m = g.match(a1)
    assert m[0] == ("A", pytest.approx(0.5))
    assert m[1] == ("B", pytest.approx(0.0))


def test_outbox_keeps_messages_until_delivered(tmp_path):
    store = Store(tmp_path / "db")
    online = {"up": False}
    delivered = []

    def post(url, payload):
        if not online["up"]:
            raise OSError("network down")
        delivered.append(payload["text"])

    chat = GoogleChat("https://example/hook?key=1", store, post=post)
    chat.send("one", None, NOW)
    chat.send("two", None, NOW)
    assert delivered == [] and len(store.outbox()) == 2
    online["up"] = True
    chat.flush(NOW)
    assert delivered == ["one", "two"] and store.outbox() == []


def test_outbox_drops_messages_older_than_a_day(tmp_path):
    store = Store(tmp_path / "db")
    chat = GoogleChat("https://example/hook", store, post=lambda u, p: (_ for _ in ()).throw(OSError()))
    chat.send("old", None, NOW)
    chat.flush(NOW + dt.timedelta(days=2))
    assert store.outbox() == []


def test_config_loads_example_and_quoted_or_unquoted_times(tmp_path):
    cfg = config.load(Path(__file__).parents[1] / "config.example.yaml")
    assert cfg.schedule.clocks() == (dt.time(8), dt.time(10), dt.time(11))
    p = tmp_path / "c.yaml"
    p.write_text("schedule:\n  window_start: 09:00\n  late_after: 10:00\n  window_end: 11:30\n")
    assert config.load(p).schedule.clocks() == (dt.time(9), dt.time(10), dt.time(11, 30))


def test_config_rejects_bad_schedule_and_typos(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('schedule:\n  window_start: "10:00"\n  late_after: "09:00"\n')
    with pytest.raises(ValueError):
        config.load(p)
    p.write_text("camera:\n  ulr: rtsp://x\n")
    with pytest.raises(ValueError, match="ulr"):
        config.load(p)


def test_sweep_and_recommend_pick_safest_setting_with_no_wrong_matches():
    samples = [
        Sample("A", "A", 0.70, 0.20),
        Sample("A", "A", 0.50, 0.45),  # accepted once the threshold is low enough
        Sample("B", "A", 0.48, 0.47),  # would be a wrong match
        Sample(None, "B", 0.40, 0.10),  # stranger
    ]
    rows = sweep(samples, thresholds=[0.35, 0.45, 0.6], margins=[0.0, 0.04])
    best = recommend(rows)
    assert best.wrong == 0
    assert (best.threshold, best.margin, best.correct) == (0.45, 0.04, 2)


def test_import_employees_handles_excel_bom_and_never_removes_anyone(tmp_path):
    store = Store(tmp_path / "db")
    store.import_employees("\ufeffemail,name\nanh@example.com,Ánh\nbinh@example.com,Bình\n")
    store.import_employees("Email , Name\nANH@example.com,Ánh Nguyễn\n")
    assert {p.email: p.name for p in store.all_employees()} == {"anh@example.com": "Ánh Nguyễn", "binh@example.com": "Bình"}
    with pytest.raises(ValueError, match="email,name"):
        store.import_employees("code;full name\nA;X\n")


def test_config_model_files_follow_backend(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("recognition:\n  backend: opencv\n")
    rc = config.load(p).recognition
    assert rc.model_files()[1] == "models/face_recognition_sface_2021dec.onnx"
    assert rc.model_id() == "face_recognition_sface_2021dec"
    p.write_text("recognition:\n  backend: dlib\n")
    with pytest.raises(ValueError, match="backend"):
        config.load(p)


def test_recommend_takes_middle_of_safe_range_not_its_edge():
    samples = [Sample("A", "A", 0.80, 0.10), Sample(None, "A", 0.30, 0.05)]
    best = recommend(sweep(samples, thresholds=[0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], margins=[0.0]))
    # 0.2 and 0.3 accept the stranger; 0.4-0.8 are all safe, so take the middle.
    assert best.threshold == 0.6 and best.correct == 1 and best.employees == 1


def test_background_delivery_never_blocks_the_sender(tmp_path):
    import threading
    import time

    store = Store(tmp_path / "db")
    release, delivered = threading.Event(), []

    def slow_post(url, payload):
        release.wait(5)  # a webhook that hangs
        delivered.append(payload["text"])

    chat = GoogleChat("https://example/hook", store, post=slow_post)
    chat.start_background()
    now = dt.datetime.now(dt.timezone.utc)  # the background sender uses the real clock to expire old messages
    t0 = time.monotonic()
    chat.send("hello", None, now)
    chat.kick(now)
    assert time.monotonic() - t0 < 1 and delivered == []  # queued, the caller is not held up
    release.set()
    for _ in range(100):
        if delivered:
            break
        time.sleep(0.02)
    assert delivered == ["hello"] and store.outbox() == []


def test_degenerate_landmarks_skip_the_face_instead_of_crashing():
    from attendance.recognizer import FaceRecognizer, align

    class Backend:
        def detect(self, frame, threshold=0.5):
            return [(np.array([10, 10, 110, 110]), 0.9, np.zeros((5, 2), np.float32)),
                    (np.array([10, 10, 110, 110]), 0.9, np.array([[30, 40], [70, 40], [50, 60], [35, 80], [65, 80]], np.float32))]

        def embed(self, frame, landmarks):
            align(frame, landmarks)
            return np.ones(8, np.float32)

    rec = FaceRecognizer.__new__(FaceRecognizer)
    rec.cfg, rec.backend = config.RecognitionConfig(), Backend()
    frame = np.full((200, 200, 3), 128, np.uint8)
    with pytest.raises(ValueError):
        align(frame, np.zeros((5, 2), np.float32))
    assert len(rec.analyze(frame)) == 1


def test_config_checks_detector_size(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("recognition:\n  det_size: 500\n")
    with pytest.raises(ValueError, match="multiple of 32"):
        config.load(p)
    p.write_text("recognition:\n  backend: opencv\n  det_size: 500\n")
    assert config.load(p).recognition.det_size == 500


def test_sharpest_buffered_frame_is_picked_where_the_faces_are():
    import time

    import cv2

    from attendance.camera import Camera, focus

    rng = np.random.default_rng(1)
    sharp = np.zeros((240, 320, 3), np.uint8)
    sharp[80:160, 100:180] = rng.integers(0, 255, (80, 80, 1), dtype=np.uint8)  # detailed "face"
    blurred = cv2.GaussianBlur(sharp, (21, 21), 8)
    face = [(90, 70, 190, 170)]
    assert focus(sharp, face) > 5 * focus(blurred, face)

    cam = Camera("0")
    now = time.time()
    frames = [(blurred, now - 0.3), (sharp, now - 0.2), (blurred.copy(), now - 0.1)]
    cam._recent.extend(frames)
    cam._frame, cam._frame_time = frames[-1]
    frame, t = cam.sharpest(now - 1, face)
    assert frame is sharp and t == now - 0.2
    assert cam.sharpest(now - 0.15, face)[1] == now - 0.1   # only frames newer than the last analysed
    assert cam.sharpest(now - 1, ())[0] is frames[-1][0]    # no faces: just the newest frame


def test_centering_removes_the_shared_direction_and_needs_enough_people():
    from attendance.gallery import MIN_PEOPLE_TO_CENTER

    rng = np.random.default_rng(3)
    shared = rng.normal(size=64)
    shared /= np.linalg.norm(shared)
    people = {f"P{i}": rng.normal(size=64) for i in range(10)}
    items = []
    for p, v in people.items():
        for _ in range(3):
            e = 0.8 * shared + 0.6 * (v / np.linalg.norm(v)) + 0.05 * rng.normal(size=64)
            items.append((p, e / np.linalg.norm(e)))
    raw, centered = Gallery(items), Gallery(items, center=True)
    probe = items[0][1]
    # Raw cosine: strangers look alike because everyone leans the same way.
    assert raw.match(probe, k=2)[1][1] > 0.5
    assert centered.centered and centered.match(probe)[0][0] == "P0"
    assert centered.match(probe, k=2)[1][1] < 0.3
    assert not Gallery(items[: 3 * (MIN_PEOPLE_TO_CENTER - 1)], center=True).centered


def test_visit_votes_like_the_live_tracker():
    from attendance.evaluate import visits

    frames = [Sample("A", "A", 0.6, 0.1, "walk1")] * 3 + [Sample("A", "B", 0.6, 0.1, "walk1")]
    frames += [Sample("B", "A", 0.6, 0.1, "walk2"), Sample("B", "B", 0.6, 0.1, "walk2")]  # split vote
    frames += [Sample(None, "A", 0.3, 0.1, "stranger")]  # rejected: below threshold
    r = visits(frames, 0.5, 0.08, votes_required=3)
    assert (r.correct, r.wrong, r.rejected, r.employees) == (1, 0, 2, 2)


def test_inference_threads_default_to_physical_cores(monkeypatch):
    from attendance.vision import recognizer

    monkeypatch.setattr(recognizer.os, "cpu_count", lambda: 12)  # 6 cores with hyper-threading
    assert recognizer.cpu_threads() == 6
    monkeypatch.setattr(recognizer.os, "cpu_count", lambda: 32)
    assert recognizer.cpu_threads() == recognizer.MAX_AUTO_THREADS
    monkeypatch.setattr(recognizer.os, "cpu_count", lambda: 1)
    assert recognizer.cpu_threads() == 1
    assert recognizer.cpu_threads(3) == 3  # an explicit setting wins


def test_onnx_sessions_do_not_spin(monkeypatch, tmp_path):
    import sys
    import types

    from attendance.vision import recognizer

    seen = {}

    class Options:
        def __init__(self):
            self.entries = {}

        def add_session_config_entry(self, key, value):
            self.entries[key] = value

    def session(path, sess_options, providers):
        seen.update(options=sess_options, providers=providers)

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(SessionOptions=Options, InferenceSession=session))
    recognizer.onnx_session(tmp_path / "m.onnx", threads=4)
    options = seen["options"]
    assert options.intra_op_num_threads == 4 and options.inter_op_num_threads == 1
    assert options.entries == {"session.intra_op.allow_spinning": "0", "session.inter_op.allow_spinning": "0"}


def test_routine_requests_are_not_logged():
    import logging

    from attendance.web import QuietRoutineRequests

    def record(msg):
        return logging.LogRecord("werkzeug", logging.INFO, __file__, 1, msg, None, None)

    quiet = QuietRoutineRequests()
    assert not quiet.filter(record('192.168.10.77 - - [20/Oct/2026 09:25:21] "GET /live.jpg?t=1 HTTP/1.1" 200 -'))
    assert not quiet.filter(record('1.2.3.4 - - [x] "\x1b[36mGET /static/app.css?v=1 HTTP/1.1\x1b[0m" 304 -'))
    assert quiet.filter(record('1.2.3.4 - - [x] "GET /media/crops/a.jpg HTTP/1.1" 404 -'))  # failures stay
    assert quiet.filter(record('1.2.3.4 - - [x] "GET /unknowns HTTP/1.1" 200 -'))  # pages stay
    assert quiet.filter(record('1.2.3.4 - - [x] "POST /settings HTTP/1.1" 302 -'))


def test_service_log_is_rotated_when_large(tmp_path):
    from attendance.cli.launcher import rotate

    log = tmp_path / "service.log"
    log.write_text("x" * 20)
    rotate(log, max_bytes=100)
    assert log.exists()
    rotate(log, max_bytes=10)
    assert not log.exists() and (tmp_path / "service.log.1").read_text() == "x" * 20
    rotate(tmp_path / "missing.log")  # nothing to do


def test_camera_opens_with_short_timeouts(monkeypatch):
    from attendance.vision import camera

    calls = []
    monkeypatch.setattr(camera.cv2, "VideoCapture", lambda *a: calls.append(a))
    camera.open_capture("rtsp://cam/stream", 2)
    url, backend, params = calls[0]
    assert dict(zip(params[::2], params[1::2])) == {camera.cv2.CAP_PROP_OPEN_TIMEOUT_MSEC: camera.OPEN_TIMEOUT_MS,
                                                    camera.cv2.CAP_PROP_READ_TIMEOUT_MSEC: camera.READ_TIMEOUT_MS}


def test_camera_page_picture_is_sharp_enough_and_full_size_is_available(core):
    import cv2
    import numpy as np

    from attendance.core.pipeline import PREVIEW_WIDTH, draw
    from attendance.web import create_app

    app, _, _ = core
    frame = np.zeros((1616, 2880, 3), dtype=np.uint8)
    preview = cv2.imdecode(np.frombuffer(draw(frame, []), np.uint8), cv2.IMREAD_COLOR)
    assert preview.shape[1] == PREVIEW_WIDTH > 960
    c = create_app(app).test_client()
    assert c.get("/live/full.jpg").status_code == 404  # nothing analysed yet
    app.live.set_frame(draw(frame, []), frame)
    full = c.get("/live/full.jpg")
    assert full.status_code == 200
    assert cv2.imdecode(np.frombuffer(full.data, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (1616, 2880)


def test_faces_below_the_vote_score_are_followed_but_cannot_vote():
    """Masks and side views lower the detector's confidence (0.5-0.65): such a face is still
    returned, so it is tracked and reviewed when unknown, but it never votes anyone in."""
    from attendance.recognizer import FaceRecognizer
    from attendance.tracker import vote_for

    kps = np.array([[60, 70], [140, 70], [100, 110], [70, 150], [130, 150]], np.float32)

    class Backend:
        def __init__(self, score):
            self.score = score

        def detect(self, frame, threshold=0.5, det_size=None):
            return [(np.array([20, 20, 180, 200]), self.score, kps)]

        def embed(self, frame, landmarks):
            return np.ones(8, np.float32)

    rec = FaceRecognizer.__new__(FaceRecognizer)
    frame = np.random.default_rng(0).integers(0, 255, (220, 220, 3), dtype=np.uint8)
    rec.cfg, rec.backend = config.RecognitionConfig(), Backend(0.55)
    face, = rec.analyze(frame)
    assert not face.size_ok and not face.quality_ok
    assert vote_for([("A", 0.95), ("B", 0.1)], face.quality_ok, face.size_ok, rec.cfg) is None  # however strong
    rec.backend = Backend(0.7)
    assert rec.analyze(frame)[0].quality_ok


def test_a_lower_detection_setting_reaches_the_detector():
    from attendance.recognizer import FaceRecognizer

    seen = {}

    class Backend:
        def detect(self, frame, threshold=0.5, det_size=None):
            seen["threshold"] = threshold
            return []

    rec = FaceRecognizer.__new__(FaceRecognizer)
    rec.cfg, rec.backend = config.RecognitionConfig(), Backend()
    rec.analyze(np.zeros((50, 50, 3), np.uint8))
    assert seen["threshold"] == 0.5  # the detectors' own cut-off with the default 0.65
    rec.cfg.min_det_score = 0.4
    rec.analyze(np.zeros((50, 50, 3), np.uint8))
    assert seen["threshold"] == 0.4


def test_camera_page_labels_use_email_names(core):
    from attendance.core.pipeline import face_label

    app, _, _ = core
    app.store._db.execute("UPDATE employees SET name = 'Lương Thế Hùng', email = 'enzo@example.com' WHERE id = 'B'")
    app.store._db.commit()

    class Face:
        quality_ok = size_ok = True
        size, sharpness, yaw, pitch, det_score = 120, 200, 0, 0, 0.9

    text, _ = face_label(app, Face(), [("B", 0.8), ("A", 0.2)])
    assert text.startswith("Enzo ") and "Lương" not in text
    Face.quality_ok = Face.size_ok = False  # behind a mask (0.55): followed, but does not count
    Face.det_score = 0.55
    assert face_label(app, Face(), [("B", 0.8), ("A", 0.2)])[0] == "? unclear"
