"""Enrol at the door, and the fixes made alongside it (duplicate photos, evidence from the raw
frame, the body photo in area mode, grouping only over recent faces)."""
import datetime as dt

import numpy as np
import pytest

from attendance.enrolment import DISTINCT, MAX_FACES, DoorEnrolment
from attendance.recognizer import DetectedFace
from attendance.web import create_app
from conftest import DAY, at, face_image, person_vector


def face(vector, size_ok=True, sharpness=100.0, bbox=(50.0, 50.0, 150.0, 150.0)):
    return DetectedFace(bbox, vector, 0.9, 100.0, sharpness, 0.0, 0.0, True, 0.8, size_ok=size_ok)


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
    assert "Same view" in e.status()["note"]
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


def test_enrolment_ends_after_its_time_or_enough_faces_and_can_be_stopped(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    e.start("A")
    ticker.t += 91
    assert not e.active and e.status()["active"] is False
    assert "no usable face" in e.status()["note"]
    assert e.observe([(face(person_vector(70)), [])], frame) is False  # too late

    e.start("A")
    for i in range(MAX_FACES + 2):
        e.observe([(face(person_vector(100 + i)), [])], frame)
    assert not e.active and e.status()["kept"] == MAX_FACES
    assert f"{MAX_FACES} face(s) of Alice" in e.status()["note"]

    e.start("B")
    e.observe([(face(person_vector(200)), [])], frame)
    e.stop()
    assert not e.active and "1 face(s) of Bob" in e.status()["note"]
    with pytest.raises(ValueError):
        e.start("nobody")


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

def test_uploading_the_same_photo_twice_is_refused(core):
    app, clock, _ = core
    result = app.library.add_photo("A", face_image(7))
    assert result.ok
    again = app.library.add_photo("A", face_image(7))
    assert not again.ok and "Almost the same" in again.message
    assert app.gallery.counts()["A"] == 2


class Analyzer:
    """Pretends one face sits in the top-left corner of whatever it is given."""

    def analyze(self, frame, regions=(), region_size=320, full_frame=True, pose_offset=(0.0, 0.0)):
        return [DetectedFace((10.0, 10.0, 60.0, 70.0), person_vector(1), 0.9, 50.0, 100.0, 0.0, 0.0, True, 0.8)]


def test_area_mode_cuts_the_body_from_the_whole_frame_and_evidence_from_the_raw_one(core):
    from attendance.processing import FrameProcessor

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


def test_unknown_faces_are_grouped_only_with_recent_ones(env):
    service, store, clock, sent = env
    v = person_vector(30)
    old = at(9, 0, day=DAY - dt.timedelta(days=service.cfg.recognition.group_days + 1))
    first = service.on_unknown(old, "old.jpg", v, None, None)
    second = service.on_unknown(at(9, 0), "new.jpg", v, None, None)
    assert store.unknown(first).group_id != store.unknown(second).group_id
    third = service.on_unknown(at(9, 5), "new2.jpg", v, None, None)
    assert store.unknown(third).group_id == store.unknown(second).group_id
