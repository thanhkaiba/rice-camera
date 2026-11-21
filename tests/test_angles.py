"""Turned faces of people the camera has just named go to Review, to teach the missing angle (angles.py)."""
from types import SimpleNamespace

import numpy as np

from attendance.core.angles import PER_DAY, AngleLearner
from conftest import at

FRAME = np.zeros((400, 400, 3), dtype=np.uint8)


def face(yaw, quality_ok=False, size=120.0, size_ok=True):
    return SimpleNamespace(bbox=(50.0, 50.0, 170.0, 170.0), embedding=np.ones(4, np.float32), yaw=yaw, pitch=0.0,
                           quality_ok=quality_ok, quality=0.5, size=size, size_ok=size_ok, landmarks=None)


def learner(core, mode="person"):
    app, _, _ = core
    app.cfg.tracking.mode = mode
    sent = []
    return app, AngleLearner(app.cfg, app.store, report=lambda f, frame, now, candidates: sent.append(candidates)), sent


TRACKS = [SimpleNamespace(person_id=7, decided="A"), SimpleNamespace(person_id=8, decided=None),
          SimpleNamespace(person_id=None, decided="B")]
LIKE_A = [("A", 0.31), ("B", 0.22)]


def test_one_turned_face_per_side_and_person_a_day_with_the_name_suggested(core):
    app, angles, sent = learner(core)
    assert angles.observe(TRACKS, [(face(-50), LIKE_A, 7)], FRAME, at(8, 30)) == 1
    assert sent == [[("A", 0.31), ("B", 0.22)]]  # the person the body was named as comes first: the suggestion
    assert angles.observe(TRACKS, [(face(-60), LIKE_A, 7)], FRAME, at(8, 31)) == 0  # that side is done for today
    assert angles.observe(TRACKS, [(face(55), [("B", 0.33), ("A", 0.30)], 7)], FRAME, at(8, 32)) == 1
    assert sent[-1][0] == ("A", 0.30)
    assert AngleLearner(app.cfg, app.store, report=lambda *a: sent.append(a)).observe(
        TRACKS, [(face(-50), LIKE_A, 7)], FRAME, at(8, 40)) == 0  # remembered after the app starts again
    assert angles.observe(TRACKS, [(face(-50), LIKE_A, 7)], FRAME, at(8, 30, day=at(8, 30).date().replace(day=15))) == 1


def test_only_clear_turned_faces_of_a_named_body(core):
    app, angles, sent = learner(core)
    cases = [(face(-50), LIKE_A, 8),  # the body has no name yet
             (face(-50), LIKE_A, None),  # a face without a body
             (face(-50, quality_ok=True), LIKE_A, 7),  # a good face: the usual collection takes care of it
             (face(-20), LIKE_A, 7),  # not turned: refused for another reason (blur, pitch)
             (face(-85), LIKE_A, 7),  # too far round
             (face(-50, size=40.0), LIKE_A, 7), (face(-50, size_ok=False), LIKE_A, 7),  # small or unclear
             (face(-50), [("B", 0.6), ("A", 0.3)], 7),  # clearly someone else: the body was mixed up
             (face(-50), [("B", 0.3), ("A", 0.1)], 7), (face(-50), [("B", 0.3)], 7)]  # hardly like A at all
    assert angles.observe(TRACKS, cases, FRAME, at(8, 30)) == 0 and sent == []


def test_switched_off_faces_only_or_skipped_people(core):
    app, angles, sent = learner(core, mode="face")
    assert angles.observe(TRACKS, [(face(-50), LIKE_A, 7)], FRAME, at(8, 30)) == 0
    app.cfg.tracking.mode = "person"
    app.cfg.collect.angles = False
    assert angles.observe(TRACKS, [(face(-50), LIKE_A, 7)], FRAME, at(8, 30)) == 0
    app.cfg.collect.angles, app.cfg.collect.skip_people = True, "A"
    assert angles.observe(TRACKS, [(face(-50), LIKE_A, 7)], FRAME, at(8, 30)) == 0
    app.cfg.collect.skip_people = ""
    many = [SimpleNamespace(person_id=n, decided=f"P{n}") for n in range(40)]
    faces = [(face(-50), [(f"P{n}", 0.3)], n) for n in range(40)]
    assert angles.observe(many, faces, FRAME, at(8, 30)) == PER_DAY  # a day's cards are limited


def test_covered_faces_are_recognised_as_such():
    from attendance.vision.recognizer import ARCFACE_LANDMARKS, looks_covered
    skin = np.full((112, 112, 3), (150, 170, 210), dtype=np.uint8)  # BGR skin tone, already aligned
    assert not looks_covered(skin, ARCFACE_LANDMARKS)
    masked = skin.copy()
    masked[66:, :] = (25, 25, 25)  # a black mask from the nose down
    assert looks_covered(masked, ARCFACE_LANDMARKS)
    blue = skin.copy()
    blue[66:, :] = (230, 190, 120)
    assert looks_covered(blue, ARCFACE_LANDMARKS)
    assert not looks_covered(skin, None)
