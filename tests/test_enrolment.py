"""Enrol at the door, and the fixes made alongside it (evidence from the raw frame, the body
photo in area mode)."""
import datetime as dt

import numpy as np
import pytest

from attendance.core.enrolment import DISTINCT, MAX_FACES, PER_STEP, DoorEnrolment
from attendance.vision.recognizer import DetectedFace
from attendance.web import create_app
from conftest import DAY, at, face_image, person_vector


def face(vector, size_ok=True, sharpness=100.0, bbox=(50.0, 50.0, 150.0, 150.0), yaw=0.0, pitch=0.0, size=100.0,
         landmarks=None):
    return DetectedFace(bbox, vector, 0.9, size, sharpness, yaw, pitch, True, 0.8, landmarks, size_ok=size_ok)


LANDMARKS = np.array([[80.0, 80.0], [120.0, 80.0], [100.0, 105.0], [85.0, 130.0], [115.0, 130.0]], np.float32)


def masked_frame(person: int = 1, size: int = 200) -> np.ndarray:
    """A flat 'face' whose mouth and chin are covered by a white, flat patch."""
    frame = face_image(person, size)
    frame[118:170, 60:140] = 245
    return frame


def unit(*parts):
    v = sum(parts)
    return (v / np.linalg.norm(v)).astype(np.float32)


class Ticker:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def enrol(core):
    app, clock, _ = core
    ticker = Ticker()
    e = DoorEnrolment(app.library, app.store, app.cfg.recognition, now=ticker)
    return app, e, ticker


def test_walk_past_the_camera_keeps_different_views_only(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    assert e.start("C") == "Carol"
    assert e.active and e.status()["seconds_left"] == 90
    v1 = person_vector(50)  # Carol with a mask: nothing like her ID photo
    assert e.observe([(face(v1), app.gallery.match(v1))], frame) is True
    assert e.observe([(face(v1), app.gallery.match(v1))], frame) is False  # the next frame, same view
    assert f"1/{MAX_FACES} kept. Hold it…" == e.status()["note"]  # nothing new; the step is not ticked by one frame
    v2 = unit(v1, 0.6 * person_vector(51))  # turned a little
    assert float(v1 @ v2) < DISTINCT
    assert e.observe([(face(v2), app.gallery.match(v2))], frame) is True
    assert e.status()["kept"] == 2
    assert app.gallery.counts()["C"] == 3  # the ID photo plus two faces from the door
    refs = app.store.references(app.cfg.recognition.model_id(), "C")
    assert [r.source for r in refs] == ["upload", "enrol", "enrol"]
    assert all(r.image and (app.data_dir / r.image).exists() for r in refs[1:])


def test_nothing_is_kept_with_two_people_or_a_colleagues_face_or_a_bad_face(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    e.start("C")
    v = person_vector(50)
    assert e.observe([(face(v), []), (face(person_vector(60)), [])], frame) is False
    assert "More than one face" in e.status()["note"]
    assert e.observe([], frame) is False
    assert e.observe([(face(v, size_ok=False), [])], frame) is False
    assert e.observe([(face(v, sharpness=5), [])], frame) is False
    bob = person_vector(2)  # Bob walks past while Carol is being enrolled
    assert e.observe([(face(bob), app.gallery.match(bob))], frame) is False
    assert "matches Bob" in e.status()["note"]
    assert e.status()["kept"] == 0 and app.gallery.counts()["C"] == 1
    assert not any(st["done"] for st in e.status()["steps"])  # none of that ticked a step either


def test_enrolment_ends_after_its_time_or_enough_faces_and_can_be_stopped(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    e.start("A")
    ticker.t += 91
    assert not e.active and e.status()["active"] is False
    assert "no usable face" in e.status()["note"]
    assert e.observe([(face(person_vector(70)), [])], frame) is False  # too late

    e.start("A")
    for i in range(6):  # many faces at once, all in the first step's pose
        e.observe([(face(person_vector(100 + i)), [])], frame)
    assert e.active and e.status()["kept"] == PER_STEP
    e.stop()
    assert f"{PER_STEP} face(s) of Alice" in e.status()["note"] and "not yet: look at the camera" in e.status()["note"]
    assert e.status()["can_continue"]

    e.start("B")
    e.observe([(face(person_vector(200)), [])], frame)
    e.stop()
    assert not e.active and "1 face(s) of Bob" in e.status()["note"]
    with pytest.raises(ValueError):
        e.start("nobody")


def test_steps_come_one_at_a_time_and_tick_only_once_held(enrol):
    """Someone who just stands there, close to the camera, ticks nothing at once: each step must be
    shown long enough to be read, and its pose held; a turn of the head for one frame is not a turn."""
    app, e, ticker = enrol
    frame = face_image(1, 300)
    big = 2 * app.cfg.recognition.min_face_px + 10
    faces = iter(range(300, 400))

    def frames(seconds, **kw):
        for _ in range(round(seconds / 0.2)):
            ticker.t += 0.2
            e.observe([(face(person_vector(next(faces)), **kw), [])], frame)

    def done():
        return [st["key"] for st in e.status()["steps"] if st["done"]]

    e.start("A")
    frames(0.2, size=big)  # looking at the camera, close: one frame ticks nothing
    assert done() == []
    frames(2.0, size=big)  # held long enough, but the step was only just shown
    assert done() == []
    frames(0.4, size=big)
    assert done() == ["straight"]  # and not "close" with it, although the face is close
    frames(2.0, size=big)
    assert done() == ["straight"]
    frames(0.6, size=big)
    assert done() == ["straight", "close"]

    frames(0.2, yaw=30.0)  # a glance aside
    frames(3.0)
    assert done() == ["straight", "close"] and "Turn your head further" in e.status()["note"]
    frames(3.0, yaw=30.0)  # either way first
    assert done()[-1] == "side"
    frames(3.0, yaw=30.0)  # the same way again is not the other side
    assert done()[-1] == "side" and "the other way" in e.status()["note"]
    frames(3.0, yaw=-30.0)
    assert done()[-1] == "other"

    e.observe([(face(person_vector(1)), [])], frame)  # a moment without a mask
    assert "No mask seen yet" in e.status()["note"]
    e.skip()  # no mask at hand
    st = e.status()
    assert not st["active"] and st["complete"] and "every view covered" in st["note"]
    assert st["kept"] == 4 * PER_STEP  # two faces in each pose done, none from the wrong poses


def test_a_held_pose_survives_a_missed_frame_but_not_a_pause(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    e.start("A")
    for i in range(30):  # 3 s looking at the camera, but with a 1 s break in the middle
        ticker.t += 0.1
        if not 10 <= i < 20:
            e.observe([(face(person_vector(500 + i)), [])], frame)
    assert not e.status()["steps"][0]["done"]
    for i in range(10):  # every third frame missed: still held
        ticker.t += 0.1
        if i % 3:
            e.observe([(face(person_vector(600 + i)), [])], frame)
    assert e.status()["steps"][0]["done"] == "seen"


def test_people_and_enrolment_use_the_email_name(core):
    app, clock, _ = core
    app.store.update_employee("C", "Carol Smith", True, email="tan.nguyen@example.com")
    app.cfg.camera.url = "rtsp://cam/stream"
    c = create_app(app).test_client()
    assert app.cfg.google_chat.names == "email"
    assert '<option value="C">Tan Nguyen</option>' in c.get("/live").get_data(as_text=True)
    people = c.get("/employees").get_data(as_text=True)
    assert "Tan Nguyen" in people and "Carol Smith" in people  # the full name still shown, smaller
    assert "<h1>Tan Nguyen</h1>" in c.get("/employees/C").get_data(as_text=True)
    assert app.enrolment.start("C") == "Tan Nguyen"
    app.cfg.google_chat.names = "name"
    assert '<option value="C">Carol Smith</option>' in c.get("/live").get_data(as_text=True)


def test_camera_page_starts_and_stops_enrolment(core):
    app, clock, _ = core
    app.cfg.camera.url = "rtsp://cam/stream"
    flask_app = create_app(app)
    flask_app.testing = True
    c = flask_app.test_client()
    page = c.get("/live").get_data(as_text=True)
    assert "Enrol at the door" in page and '<option value="C">Carol</option>' in page
    assert c.post("/live/enrol", data={"employee_id": "C"}).status_code == 302
    assert app.enrolment.active
    status = c.get("/live.json").get_json()["enrol"]
    assert status["active"] and status["name"] == "Carol" and status["kept"] == 0
    assert c.post("/live/enrol", data={"action": "stop"}).status_code == 302
    assert not app.enrolment.active
    assert c.post("/live/enrol", data={"employee_id": "ZZ"}, follow_redirects=True).status_code == 200
    assert not app.enrolment.active
    assert app.store.audit()[0][2] == "enrol at the door"


# fixes made alongside -------------------------------------------------------------------------

class Analyzer:
    """Pretends one face sits in the top-left corner of whatever it is given."""

    def analyze(self, frame, regions=(), region_size=320, full_frame=True, pose_offset=(0.0, 0.0)):
        return [DetectedFace((10.0, 10.0, 60.0, 70.0), person_vector(1), 0.9, 50.0, 100.0, 0.0, 0.0, True, 0.8)]


def test_area_mode_cuts_the_body_from_the_whole_frame_and_evidence_from_the_raw_one(core):
    from attendance.vision.processing import FrameProcessor

    app, clock, _ = core
    app._recognizer = Analyzer()
    app.cfg.camera.area = "0.5,0.5,1,1"
    frame = np.full((400, 800, 3), 200, np.uint8)  # "enhanced"
    raw = np.full((400, 800, 3), 20, np.uint8)
    proc = FrameProcessor(app)
    proc.face_tracker.p.votes_required = 1
    decisions, matched, _ = proc.process(frame, at(9, 0), raw=raw)
    assert matched[0][0].bbox == (410.0, 210.0, 460.0, 270.0)
    assert len(decisions) == 1
    d = decisions[0]
    scene, box = d.scene
    assert scene is raw and box == (410.0, 210.0, 460.0, 270.0)
    # The person below the face: 4 face widths wide, down to the bottom of the whole picture, not
    # cut at the edge of the analysed area.
    assert d.body.shape[1] == 200 and d.body.shape[0] == 400 - (210 - 60)
    assert d.crop.shape[0] > 0
