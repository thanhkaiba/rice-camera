import datetime as dt

import numpy as np
import pytest

from attendance.config import RecognitionConfig
from attendance.gallery import Gallery
from attendance.tracker import FaceTracker, Observation

T0 = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)
rng = np.random.default_rng(0)


def unit(v):
    return v / np.linalg.norm(v)


A, B = unit(rng.normal(size=512)), unit(rng.normal(size=512))
GALLERY = Gallery([("A", A), ("B", B)])


def noisy(v, amount=0.6):
    return unit(v + amount * unit(rng.normal(size=512)))


def obs(emb, x=100.0, ok=True, quality=0.5):
    return Observation((x, 100.0, x + 80, 180.0), emb, GALLERY.match(emb), ok, quality, crop=f"crop{quality}")


def tracker(**kw):
    params = RecognitionConfig(accept_score=0.45, min_margin=0.08, votes_required=3, track_timeout_s=1.5,
                               unknown_min_frames=3)
    for k, v in kw.items():
        setattr(params, k, v)
    return FaceTracker(params)


def frames(tr, make, n, start=T0, step=0.2):
    out = []
    for i in range(n):
        out += tr.update(make(i), start + dt.timedelta(seconds=i * step))
    return out


def test_identifies_after_enough_agreeing_frames_using_first_seen_time():
    tr = tracker()
    out = frames(tr, lambda i: [obs(noisy(A), x=100 + 5 * i, quality=i / 10)], 5)
    assert len(out) == 1
    d = out[0]
    assert (d.kind, d.employee_id, d.ts) == ("identified", "A", T0)
    assert d.crop == "crop0.2"  # best-quality frame up to the decision


def test_two_ordinary_frames_are_not_enough():
    tr = tracker()
    weak = lambda i: [Observation((100, 100, 180, 180), noisy(A), [("A", 0.50), ("B", 0.1)], True, 0.5)]
    out = frames(tr, weak, 2)  # votes for A, but not far above accept_score
    out += tr.update([], T0 + dt.timedelta(seconds=20))
    assert [d.kind for d in out] == []  # too few frames to report as unknown either


def test_two_strong_unanimous_frames_identify_when_the_track_ends():
    tr = tracker()
    assert frames(tr, lambda i: [obs(noisy(A, 0.3))], 2) == []
    out = tr.update([], T0 + dt.timedelta(seconds=5))
    assert [(d.kind, d.employee_id) for d in out] == [("identified", "A")]


def test_settings_changes_apply_to_a_running_tracker():
    tr = tracker()
    tr.p.votes_required = 1
    out = frames(tr, lambda i: [obs(noisy(A))], 1)
    assert [d.employee_id for d in out] == ["A"]


def test_poor_quality_frames_with_ordinary_matches_never_identify_and_become_unknown():
    tr = tracker()
    out = frames(tr, lambda i: [obs(noisy(A, 1.4), ok=False)], 6)  # matches A at ~0.58, below the strong bar
    assert out == []
    out = tr.update([], T0 + dt.timedelta(seconds=20))
    assert [d.kind for d in out] == ["unknown"]
    assert out[0].employee_id == "A"  # best guess shown to the admin


def test_stranger_becomes_unknown_and_short_glimpses_are_ignored():
    stranger = unit(rng.normal(size=512))
    tr = tracker()
    out = frames(tr, lambda i: [obs(noisy(stranger, 0.3))], 4)
    out += frames(tr, lambda i: [obs(noisy(stranger, 0.3), x=600)], 2, start=T0 + dt.timedelta(seconds=30))
    out += tr.flush()
    assert [d.kind for d in out] == ["unknown"]


def test_two_people_side_by_side_are_tracked_separately():
    tr = tracker()
    out = frames(tr, lambda i: [obs(noisy(A), x=100), obs(noisy(B), x=400)], 4)
    assert sorted(d.employee_id for d in out) == ["A", "B"]


@pytest.mark.parametrize('gap', [0.2, 1.9, 2.3])
def test_eight_people_keep_votes_when_frame_processing_is_slow(gap):
    tr = tracker()
    vectors = np.eye(8, 512, dtype=np.float32)
    out = []
    for frame in range(3):
        observations = [Observation((i * 100, 0, i * 100 + 80, 80), v,
                                    [(str(i), .50), ('other', .1)], True, .8)
                        for i, v in enumerate(vectors)]
        decisions = tr.update(observations, T0 + dt.timedelta(seconds=frame * gap),
                              frame_interval_s=gap + .2)
        if frame < 2:
            assert not decisions
        out += decisions
    assert {d.employee_id for d in out} == {str(i) for i in range(8)}
    assert len(out) == 8 and all(d.ts == T0 for d in out)
    assert tr.flush() == []


def test_slow_frames_still_send_unrecognized_faces_to_review():
    tr = tracker()
    for i in range(3):
        unknown = Observation((100, 100, 180, 180), A, [], True, .8)
        assert tr.update([unknown], T0 + dt.timedelta(seconds=i * 2.3), frame_interval_s=2.5) == []
    out = tr.update([], T0 + dt.timedelta(seconds=12), frame_interval_s=2.5)
    assert len(out) == 1 and out[0].kind == 'unknown' and out[0].ts == T0
    assert tr.flush() == []


def test_processing_allowance_does_not_join_visits_across_a_camera_outage():
    tr = tracker()
    for seconds in [0, 10, 20]:
        weak = Observation((100, 100, 180, 180), A, [('A', .5), ('B', .1)], True, .8)
        assert tr.update([weak], T0 + dt.timedelta(seconds=seconds), frame_interval_s=2.5) == []
    assert tr.flush() == []  # each isolated glimpse stays below the vote and review requirements


def test_different_people_at_the_same_spot_stay_separate_with_slow_frames():
    tr = tracker()
    for i, vector in enumerate([A, A, B]):
        person = 'A' if vector is A else 'B'
        face = Observation((100, 100, 180, 180), vector, [(person, .5)], True, .8)
        assert tr.update([face], T0 + dt.timedelta(seconds=i * 2.3), frame_interval_s=2.5) == []
    assert tr.flush() == []  # two A votes and one B vote cannot form a three-vote decision


def test_next_person_at_same_spot_is_not_merged_into_previous_track():
    tr = tracker()
    out = frames(tr, lambda i: [obs(noisy(A))], 4)
    out += frames(tr, lambda i: [obs(noisy(B))], 4, start=T0 + dt.timedelta(seconds=1))
    assert [d.employee_id for d in out] == ["A", "B"]


def test_brief_turned_head_next_to_identified_track_is_not_reported():
    tr = tracker()
    frames(tr, lambda i: [obs(noisy(A))], 4)
    # Same person, frames too poor to vote, appearing as a separate track right after.
    frames(tr, lambda i: [obs(noisy(A, 1.2), x=900, ok=False)], 4, start=T0 + dt.timedelta(seconds=2))
    assert tr.flush() == []


def test_conflicting_votes_do_not_identify():
    tr = tracker(min_margin=0.0, accept_score=0.0)
    seq = [A, B, A, B, A, B]
    # Force both identities onto one track by keeping the box still and the link similarity high.
    mixed = [unit(A + B + 0.05 * v) for v in seq]
    out = []
    for i, m in enumerate(mixed):
        candidates = [("A", 0.6), ("B", 0.55)] if seq[i] is A else [("B", 0.6), ("A", 0.55)]
        o = Observation((100, 100, 180, 180), m, candidates, True, 0.5)
        out += tr.update([o], T0 + dt.timedelta(seconds=0.2 * i))
    assert [d for d in out if d.kind == "identified"] == []


def test_strong_matches_outweigh_the_pose_check_but_not_a_tiny_face():
    """A camera above head height makes every face look tilted down; a face that still matches one
    person far above the threshold votes. A face too small to trust never does."""
    tr = tracker()
    out = frames(tr, lambda i: [obs(noisy(A, 0.6), ok=False)], 4)  # ~0.86, pose check failed
    assert [d.employee_id for d in out] == ["A"]

    tr = tracker()
    tiny = lambda i: [Observation((100, 100, 130, 130), noisy(A, 0.6), GALLERY.match(noisy(A, 0.6)), False, 0.1,
                                  size_ok=False)]
    assert [d.kind for d in frames(tr, tiny, 6) + tr.flush()] == ["unknown"]
