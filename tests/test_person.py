import datetime as dt

import numpy as np
import pytest
from conftest import face_image

from attendance.person import ByteTracker, assign_faces, box_iou, head_region
from attendance.processing import FrameProcessor
from attendance.recognizer import DetectedFace
from test_tracker import A, B, T0, obs, tracker


def step(i, fps=5):
    return T0 + dt.timedelta(seconds=i / fps)


def boxes(*bs):
    return np.array(bs, dtype=np.float32).reshape(-1, 4)


def test_box_iou():
    m = box_iou(boxes([0, 0, 10, 10]), boxes([0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]))
    assert np.allclose(m, [[1.0, 1 / 3, 0.0]])


def test_walking_person_keeps_one_id_and_first_seen_time():
    bt = ByteTracker()
    ids = set()
    for i in range(15):
        x = 100 + 12 * i
        people = bt.update(boxes([x, 100, x + 80, 300]), np.array([0.9]), step(i))
        ids |= {p.id for p in people}
    assert ids == {1}
    assert bt.alive() == {1: step(0)}


def test_half_hidden_person_is_kept_by_a_weak_detection():
    bt = ByteTracker()
    for i in range(3):
        bt.update(boxes([100, 100, 180, 300]), np.array([0.9]), step(i))
    people = bt.update(boxes([102, 100, 182, 300]), np.array([0.2]), step(3))  # mostly covered
    assert [p.id for p in people] == [1]


def test_weak_detection_alone_never_starts_a_person():
    bt = ByteTracker()
    for i in range(5):
        assert bt.update(boxes([100, 100, 180, 300]), np.array([0.3]), step(i)) == []


def test_two_people_crossing_keep_their_ids():
    bt = ByteTracker()
    for i in range(12):
        a, b = 100 + 20 * i, 400 - 20 * i
        people = bt.update(boxes([a, 100, a + 80, 300], [b, 110, b + 80, 310]), np.array([0.9, 0.85]), step(i))
    by_id = {p.id: p.box[0] for p in people}
    assert by_id[1] > by_id[2]  # person 1 started on the left and is now on the right


def test_person_out_of_sight_is_forgotten_after_lost_time():
    bt = ByteTracker(lost_s=1.0)
    for i in range(3):
        bt.update(boxes([100, 100, 180, 300]), np.array([0.9]), step(i))
    assert bt.update(boxes(), np.array([]), step(4)) == []
    assert 1 in bt.alive()  # hidden for a moment: still followed
    bt.update(boxes(), np.array([]), step(10))
    assert bt.alive() == {}


def test_head_region_whole_body_and_close_up():
    # Whole body far away: the top part of the box.
    x1, y1, x2, y2 = head_region((100, 100, 200, 400), (720, 1280))
    assert y2 == 100 + 135 and x1 < 100 < 200 < x2
    # Head and shoulders close to the camera: reaches below the width of the box, within the box.
    _, _, _, y2 = head_region((100, 100, 300, 350), (720, 1280))
    assert y2 == 340
    # Never outside the frame.
    assert head_region((0, 0, 50, 700), (720, 1280))[:2] == (0, 0)


class P:
    def __init__(self, id, box):
        self.id, self.box = id, box


def test_faces_go_to_the_person_whose_head_they_are_in():
    people = [P(1, (100, 100, 200, 400)), P(2, (180, 120, 280, 420))]
    faces = [(120, 110, 160, 150), (215, 130, 255, 170), (120, 350, 160, 390)]  # last one: at knee height
    assert assign_faces(faces, people) == [1, 2, None]


def test_one_face_per_person_the_larger_wins():
    people = [P(1, (100, 100, 200, 400))]
    assert assign_faces([(110, 110, 130, 130), (120, 110, 170, 160)], people) == [None, 1]


# face tracker with person ids ---------------------------------------------------------------

def test_faces_of_two_people_never_share_a_track():
    tr = tracker()
    out = []
    for i in range(5):
        # Same spot and similar look would normally link; the person tracks keep them apart.
        o1, o2 = obs(A, x=100), obs(B, x=105)
        o1.person_id, o2.person_id = 1, 2
        out += tr.update([o1, o2], step(i), {1: T0, 2: T0})
    assert sorted(d.employee_id for d in out if d.kind == "identified") == ["A", "B"]


def test_followed_person_keeps_the_face_track_and_arrival_time():
    tr = tracker(votes_required=3)
    arrived = T0 - dt.timedelta(seconds=3)  # the body was seen before the face was usable
    out = []
    for i in range(2):
        o = obs(A)
        o.person_id = 7
        out += tr.update([o], step(i), {7: arrived})
    for i in range(2, 20):  # face turned away for 3.6 s, longer than track_timeout_s
        out += tr.update([], step(i), {7: arrived})
    o = obs(A)
    o.person_id = 7
    out += tr.update([o], step(20), {7: arrived})
    assert [(d.kind, d.employee_id, d.ts) for d in out] == [("identified", "A", arrived)]


# frame processor -----------------------------------------------------------------------------

class RegionRecognizer:
    """Records how analyze() was called; returns one face of employee A."""

    def __init__(self):
        self.calls = []

    def analyze(self, frame, regions=(), region_size=320, full_frame=True, pose_offset=(0.0, 0.0)):
        from conftest import person_vector

        self.calls.append((list(regions), full_frame))
        return [DetectedFace((120.0, 110.0, 180.0, 170.0), person_vector(1), 0.9, 60.0, 100.0, 0.0, 0.0, True, 0.8)]


class FakeDetector:
    kind = "fake"

    def detect(self, frame, min_score=0.1):
        return boxes([100, 100, 200, 400]), np.array([0.9], dtype=np.float32)


def test_missing_person_model_falls_back_to_faces(core):
    app, clock, _ = core
    app.cfg.tracking.mode = "person"
    app._recognizer = RegionRecognizer()
    proc = FrameProcessor(app)
    _, matched, people = proc.process(face_image(1), step(0))
    assert "Person tracking is off" in proc.person_error
    assert people == [] and len(matched) == 1
    assert app._recognizer.calls == [([], True)]


def test_person_mode_searches_heads_every_frame_and_whole_frame_every_nth(core):
    app, clock, _ = core
    app.cfg.tracking.mode = "person"
    app.cfg.tracking.full_frame_every = 3
    app._recognizer = RegionRecognizer()
    proc = FrameProcessor(app)
    proc._detector = FakeDetector()
    decisions = []
    for i in range(7):
        d, matched, people = proc.process(face_image(1), step(i))
        decisions += d
    calls = app._recognizer.calls
    assert all(regions for regions, _ in calls)  # a confident person is followed from the first frame
    assert [full for _, full in calls] == [True, False, False, True, False, False, True]
    assert matched[0][2] == people[0].id  # the face belongs to the followed person
    assert [d.employee_id for d in decisions] == ["A"]


def test_face_mode_ignores_person_detector(core):
    app, clock, _ = core
    app._recognizer = RegionRecognizer()
    proc = FrameProcessor(app)
    proc.process(face_image(1), step(0))
    assert proc._detector is None and proc.person_error is None


@pytest.mark.parametrize('capture_jitter', [0., .35])
def test_frame_processor_keeps_eight_tracks_across_slow_then_fast_inference(core, monkeypatch, capture_jitter):
    from types import SimpleNamespace
    from attendance.vision import processing

    app, _, _ = core
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(processing, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    vectors = np.eye(8, 512, dtype=np.float32)
    app.gallery.replace([(str(i), v) for i, v in enumerate(vectors)])
    durations = iter([2.3, 2.3, .05])

    class SlowRecognizer:
        def analyze(self, *args):
            clock.now += next(durations)
            return [DetectedFace((i * 100., 10., i * 100. + 80, 90.), v,
                                 .9, 80., 100., 0., 0., True, .8)
                    for i, v in enumerate(vectors)]

    app._recognizer = SlowRecognizer()
    proc = FrameProcessor(app)
    out = []
    for i in range(3):
        decisions, matched, _ = proc.process(np.zeros((100, 800, 3), np.uint8),
                                             T0 + dt.timedelta(seconds=i * (2.5 + capture_jitter)))
        assert len(matched) == 8
        if i < 2:
            assert not decisions
        out += decisions
    assert len(out) == 8 and {d.employee_id for d in out} == {str(i) for i in range(8)}
    assert all(d.ts == T0 for d in out)


@pytest.mark.parametrize('processing_s', [.01, 2.3])
def test_offline_benchmark_expiry_depends_on_video_time(core, monkeypatch, tmp_path, processing_s):
    from types import SimpleNamespace
    import cv2
    from attendance.cli.benchmark import run_mode
    from attendance.vision import processing

    app, _, _ = core
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(processing, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    frames = iter([face_image(1)] * 3)

    class Video:
        def isOpened(self): return True
        def get(self, prop): return .4  # observations are 2.5 seconds apart in the video
        def read(self):
            frame = next(frames, None)
            return frame is not None, frame
        def release(self): pass

    class SlowRecognizer(RegionRecognizer):
        def analyze(self, *args):
            clock.now += processing_s
            return super().analyze(*args)

    monkeypatch.setattr(cv2, 'VideoCapture', lambda path: Video())
    app._recognizer = SlowRecognizer()
    result = run_mode(app, tmp_path / 'recording.avi', 'face', fps=.4)
    assert result.frames == 3
    assert result.identified == [] and result.unknowns == 0


# detection area ------------------------------------------------------------------------------

def test_area_setting_is_parsed_and_validated():
    import pytest

    from attendance.config import CameraConfig, Config

    c = CameraConfig(area="0.25,0.1,0.75,1")
    assert c.area_box((720, 1280)) == (320, 72, 960, 720)
    assert CameraConfig().area_box((720, 1280)) is None
    for bad in ("0.5,0.5,0.4,1", "0,0,0.05,1", "a,b,c,d", "0,0,1"):
        cfg = Config()
        cfg.camera.area = bad
        with pytest.raises(ValueError):
            cfg.validate()
    cfg = Config()
    cfg.camera.area = " 0.2500, 0, 1.0, 1 "
    cfg.validate()
    assert cfg.camera.area == "0.25,0,1,1"


class CropRecognizer(RegionRecognizer):
    def analyze(self, frame, regions=(), region_size=320, full_frame=True, pose_offset=(0.0, 0.0)):
        from conftest import person_vector

        self.calls.append((frame.shape, list(regions), full_frame))
        return [DetectedFace((10.0, 20.0, 70.0, 80.0), person_vector(1), 0.9, 60.0, 100.0, 0.0, 0.0, True, 0.8,
                             np.array([[30.0, 40.0]] * 5))]


def test_only_the_area_is_analysed_and_boxes_come_back_in_frame_pixels(core):
    app, clock, _ = core
    app.cfg.camera.area = "0.5,0.25,1,1"
    app._recognizer = CropRecognizer()
    proc = FrameProcessor(app)
    frame = np.zeros((400, 800, 3), np.uint8)
    _, matched, _ = proc.process(frame, step(0))
    assert app._recognizer.calls[0][0] == (300, 400, 3)  # bottom-right part only
    face = matched[0][0]
    assert face.bbox == (410.0, 120.0, 470.0, 180.0)
    assert face.landmarks[0].tolist() == [430.0, 140.0]


def test_area_with_person_tracking_shifts_people_too(core):
    app, clock, _ = core
    app.cfg.camera.area = "0.5,0,1,1"
    app.cfg.tracking.mode = "person"
    app._recognizer = CropRecognizer()
    proc = FrameProcessor(app)
    proc._detector = FakeDetector()
    _, _, people = proc.process(np.zeros((400, 800, 3), np.uint8), step(0))
    assert people[0].box.tolist() == [500, 100, 600, 400]
    assert proc._people.tracks[0].box.tolist() == [100, 100, 200, 400]  # the tracker keeps area pixels


def test_scene_keeps_the_whole_frame_and_shifts_the_face_box(core):
    app, clock, _ = core
    app.cfg.camera.area = "0.5,0.25,1,1"
    app._recognizer = CropRecognizer()
    proc = FrameProcessor(app)
    frame = np.zeros((400, 800, 3), np.uint8)
    decisions = []
    for i in range(4):
        d, _, _ = proc.process(frame, step(i))
        decisions += d
    [d] = decisions
    scene_frame, box = d.scene
    assert scene_frame.shape == (400, 800, 3) and box == (410.0, 120.0, 470.0, 180.0)
