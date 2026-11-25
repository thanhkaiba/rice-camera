import datetime as dt

import numpy as np

from attendance.core.config import RecognitionConfig
from attendance.vision.tracker import FaceTracker, Observation

T0 = dt.datetime(2026, 11, 25, 9, 26, 57, tzinfo=dt.timezone.utc)


def obs(cands, quality_ok=True, size_ok=True):
    v = np.zeros(512, np.float32); v[0] = 1
    return Observation((0, 0, 10, 10), v, cands, quality_ok, 0.5, size_ok=size_ok)


def run(frames, allowed=lambda p: True):
    tr = FaceTracker(RecognitionConfig(accept_score=0.35))  # as set in the office
    tr.agree_allowed = allowed
    out = []
    for i, f in enumerate(frames):
        out += tr.update([f], T0 + dt.timedelta(seconds=0.3 * i))
    return out + tr.update([], T0 + dt.timedelta(seconds=60)) + tr.flush()


def ted_like():  # 26/11 morning: three frames, all Ted, one vote
    return [obs([("ted", 0.39), ("x", 0.30)], quality_ok=False),
            obs([("ted", 0.44), ("x", 0.32)]),
            obs([("ted", 0.42), ("x", 0.34)])]


def test_every_frame_agreeing_names_the_person():
    named = [d for d in run(ted_like()) if d.kind == "identified"]
    assert [d.employee_id for d in named] == ["ted"] and named[0].rule == "agreeing frames"


def test_not_for_people_not_counted():
    assert not [d for d in run(ted_like(), allowed=lambda p: False) if d.kind == "identified"]


def test_a_frame_matching_someone_else_stops_it():
    frames = ted_like() + [obs([("x", 0.40), ("ted", 0.30)], quality_ok=False)]
    assert not [d for d in run(frames) if d.kind == "identified"]


def test_low_average_or_no_vote_stops_it():
    low = [obs([("ted", 0.36), ("x", 0.20)]), obs([("ted", 0.36), ("x", 0.20)], quality_ok=False),
           obs([("ted", 0.37), ("x", 0.30)], quality_ok=False)]
    assert not [d for d in run(low) if d.kind == "identified"]
    no_vote = [obs([("ted", 0.45), ("x", 0.40)]), obs([("ted", 0.45), ("x", 0.40)])]
    assert not [d for d in run(no_vote) if d.kind == "identified"]
