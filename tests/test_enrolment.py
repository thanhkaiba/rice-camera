"""Enrol at the door, and the fixes made alongside it (duplicate photos, evidence from the raw
frame, the body photo in area mode, grouping only over recent faces)."""
import datetime as dt

import numpy as np
import pytest

from attendance.enrolment import DISTINCT, MAX_FACES, STEPS, DoorEnrolment, looks_masked
from attendance.recognizer import DetectedFace
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
    assert "1/8 kept. Closer still." == e.status()["note"]  # nothing new, told what to do next
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


# the guided steps ----------------------------------------------------------------------------

def steps(e):
    return {st["key"]: st["done"] for st in e.status()["steps"]}


def test_steps_are_ticked_by_what_the_camera_sees_and_the_person_is_told_the_next_one(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    e.start("C")
    st = e.status()
    assert st["instruction"] == "Look at the camera" and st["hint"] and [s["label"] for s in st["steps"]] == [l for _, l in STEPS]
    assert e.observe([(face(person_vector(50)), [])], frame) is True
    assert steps(e)["straight"] == "seen" and e.status()["instruction"] == "Come close to the camera"
    e.observe([(face(person_vector(51)), [])], frame)
    assert "Closer still" in e.status()["note"]
    e.observe([(face(person_vector(52), size=2 * app.cfg.recognition.min_face_px), [])], frame)
    assert steps(e)["close"] == "seen" and e.status()["instruction"] == "Turn your head to one side"
    # Turned, but not enough: told to turn further. Then far enough: ticked, whichever side first.
    e.observe([(face(person_vector(53), yaw=8.0), [])], frame)
    assert steps(e)["side"] is None and "further" in e.status()["note"]
    e.observe([(face(person_vector(54), yaw=25.0), [])], frame)
    assert steps(e)["other"] == "seen" and steps(e)["side"] is None
    assert e.status()["instruction"] == "Turn your head to one side"  # the first step not yet done
    e.observe([(face(person_vector(55), yaw=-25.0), [])], frame)
    assert steps(e)["side"] == "seen" and e.status()["instruction"].startswith("Put a mask on")
    e.observe([(face(person_vector(56), yaw=90.0), [])], frame)  # a profile is no reference
    assert "too far" in e.status()["note"]
    assert not e.status()["complete"] and e.active
    # The mask step finishes it: every view covered, before the time is up.
    e.observe([(face(person_vector(57), landmarks=LANDMARKS), [])], masked_frame(1, 300))
    assert steps(e)["mask"] == "seen" and e.status()["complete"] and not e.active
    assert e.status()["instruction"] == "Enough, thank you!" and "every view covered" in e.status()["note"]
    assert e.status()["can_continue"] is False and e.status()["ended_ago"] == 0


def test_a_step_can_be_skipped_and_a_timed_out_session_continued(enrol):
    app, e, ticker = enrol
    frame = face_image(1, 300)
    assert e.skip() is None  # nothing running
    e.start("A")
    e.observe([(face(person_vector(60)), [])], frame)
    assert e.skip() == "close" and steps(e)["close"] == "skipped"
    assert e.status()["instruction"] == "Turn your head to one side"
    ticker.t += 91
    st = e.status()
    assert not st["active"] and st["can_continue"] and st["instruction"].startswith("Time is up; not yet: turn your head")
    assert "1 face(s) of Alice" in st["note"] and "Continue" in st["note"]
    assert e.resume() and e.active and e.status()["seconds_left"] == 60
    assert steps(e)["straight"] == "seen" and steps(e)["close"] == "skipped"  # progress kept
    e.observe([(face(person_vector(61), yaw=20.0), [])], frame)
    e.observe([(face(person_vector(62), yaw=-20.0), [])], frame)
    assert e.skip() == "mask" and not e.active and e.status()["complete"]
    assert not e.resume()  # nothing left to continue
    # Stopped by the admin with steps missing: says so, and offers to continue.
    e.start("B")
    e.observe([(face(person_vector(63)), [])], frame)
    e.stop()
    assert e.status()["note"].startswith("Stopped: 1 face(s) of Bob") and e.status()["can_continue"]


def test_lower_face_covered_is_noticed_but_a_bare_face_is_not():
    assert looks_masked(masked_frame(), LANDMARKS)
    assert not looks_masked(face_image(1), LANDMARKS)
    assert not looks_masked(masked_frame(), None)
    beard = masked_frame()
    beard[118:170, 60:140] = np.random.default_rng(0).integers(0, 255, (52, 80, 3), np.uint8)  # textured, not a mask
    assert not looks_masked(beard, LANDMARKS)


def test_camera_page_skips_a_step_and_continues(core):
    app, clock, _ = core
    app.cfg.camera.url = "rtsp://cam/stream"
    flask_app = create_app(app)
    flask_app.testing = True
    c = flask_app.test_client()
    assert c.post("/live/enrol", data={"action": "more"}, follow_redirects=True).status_code == 200
    assert not app.enrolment.active
    c.post("/live/enrol", data={"employee_id": "C"})
    page = c.get("/live").get_data(as_text=True)
    assert "Look at the camera" in page and "Skip step" in page and "Read the steps aloud" in page
    assert c.post("/live/enrol", data={"action": "skip"}).status_code == 302
    status = c.get("/live.json").get_json()["enrol"]
    assert status["steps"][0]["done"] == "skipped" and status["instruction"] == "Come close to the camera"
    app.enrolment.observe([(face(person_vector(70)), [])], face_image(1, 300))
    app.enrolment.stop()
    assert c.get("/live.json").get_json()["enrol"]["can_continue"]
    assert c.post("/live/enrol", data={"action": "more"}).status_code == 302
    assert app.enrolment.active
    assert [tuple(a[2:4]) for a in app.store.audit()[:3]] == [("enrol at the door", "continued for one more minute"),
                                                        ("enrol at the door", "skipped step: straight"),
                                                        ("enrol at the door", "Carol")]
