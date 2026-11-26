"""Follow each face across frames and decide only after several consistent, good-quality frames.

A single frame is never trusted: a person is identified when at least `votes_required`
good frames agree on them with a clear margin over the runner-up. Tracks that end without
a decision become "unknown" so an admin can look at them.

With person tracking on, each face carries the id of the person (body) it belongs to. Faces of
the same person join the same track, faces of different people never do, and the track lives as long
as the person is followed, even while their face is hidden. In a crowd a person's box can pass from
one body to another: a face that clearly votes for someone other than the one its person's track
voted for starts a track of its own (26/11/2026: Peter's three clear votes were outvoted by two
other people's in one track, and nobody was named).
"""
from __future__ import annotations

import datetime as dt
import itertools
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

LINK_IOU = 0.2  # same position as the previous frame
LINK_SIM = 0.5  # or clearly the same face
LINK_MIN_SIM = 0.3  # never link faces that look like different people, even at the same position
# A person box can hold the face of someone standing right behind (the box takes its largest face, and when
# the person in front turns away that is the face behind). A face this unlike the faces of the box's tracks
# starts a track of its own (see FaceTracker._link). Measured on the picture-a-second recordings of 26/11/2026
# (08:50-09:10, 09:20-09:30): one person's face from one second to the next was 0.28 alike at worst and 0.33
# or more in 95 of 100 pairs (2 of 48 below 0.3); two different people within 250 px, 0.34 at most and
# 0.23 or less in 95 of 100 (1 of 46 above 0.3). Live frames are closer together than a second.
SAME_FACE_SIM = 0.3
SPLIT_WINDOW = dt.timedelta(seconds=5)
# An unknown track that starts this soon after an unknown track ended, and looks like it, continues
# that track's Review card instead of opening a new one: a few frames a second and a face that
# turns or is masked break one walk to the door into several tracks (see Decision.follows).
CONTINUE_WINDOW = SPLIT_WINDOW
REPORTED_KEPT = 10  # recently reported unknown tracks remembered for that
# A track that ends one vote short is still accepted when its votes are unanimous and this much
# above accept_score: someone walking fast past a slow PC may only give two good frames. Over a
# week of doorway cards, tracks whose best frame scored 0.50-0.55 named the right person 25 times
# and the wrong one 3 times; at 0.45-0.50 it was 51 to 17, too often wrong for two votes.
STRONG_BONUS = 0.05
# A face that fails the pose or sharpness check still votes when it matches one person this far
# above accept_score, with twice the usual margin. The pose is only estimated from 5 landmarks,
# and a camera mounted above head height makes every face look tilted down; tested on doorway
# video, such faces matched the right person at 0.85-0.9 while everyone else stayed below 0.2.
STRONG_MATCH = 0.15
# A track that ends without enough votes is still named when every frame's best match was the same
# person, at this average score or more, with at least one real vote: in a crowd a face shows for a
# second or two, often too few frames for votes_required. Measured on the review cards of 13-25/11
# 2026 (406 unnamed tracks): 18 such tracks, all the right person (15 confirmed by an admin, 3 checked
# by eye), and 11 more of Head Chef, whose references all wore a mask: people not counted for
# lateness are left out (see FaceTracker.agree_allowed). Tracks of fewer than three frames were never
# reported, so the rule was measured on three or more and is kept to that.
AGREE_MEAN = 0.38
AGREE_FRAMES = 3


def clear_match(candidates, params) -> bool:
    """One person far above accept_score, with twice the usual margin over the next."""
    if not candidates:
        return False
    top = candidates[0][1]
    margin = top - (candidates[1][1] if len(candidates) > 1 else -1.0)
    return top >= params.accept_score + STRONG_MATCH and margin >= 2 * params.min_margin


TRACE_FRAMES = 20  # frames of a track described in the log when nobody was named


def _frame_note(obs, voted: str | None, params) -> str:
    """One frame of a track for the log: the face, its best match, and why it did not vote."""
    if not obs.candidates:
        return f"{obs.detail} no match"
    person, top = obs.candidates[0]
    margin = top - (obs.candidates[1][1] if len(obs.candidates) > 1 else -1.0)
    if voted is not None:
        why = "vote"
    elif top < params.accept_score:
        why = "low score"
    elif margin < params.min_margin:
        why = "close second"
    elif not obs.size_ok:
        why = "too small or unclear"
    else:
        why = "small, blurred or turned and not a clear match"
    return f"{obs.detail} {person[:6]} {top:.2f} +{min(margin, 9.99):.2f} {why}"


def vote_for(candidates, quality_ok: bool, size_ok: bool, params) -> str | None:
    """The person this face votes for, or None: a clear match on a usable face."""
    if not candidates:
        return None
    person, top = candidates[0]
    margin = top - (candidates[1][1] if len(candidates) > 1 else -1.0)
    if top < params.accept_score or margin < params.min_margin:
        return None
    if quality_ok:
        return person
    return person if size_ok and clear_match(candidates, params) else None


@dataclass
class Observation:
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray
    candidates: list[tuple[str, float]]  # best gallery matches, best first
    quality_ok: bool
    quality: float  # used to pick the best image of a track
    crop: Any = None
    person_id: int | None = None  # the person (body) track this face belongs to, if known
    size_ok: bool = True  # big and clearly detected (see vote_for)
    body: Any = None  # the person below the face, shown on the review page
    scene: Any = None  # (whole camera frame, face box in it): the evidence picture
    detail: str = ""  # size, detector score, sharpness and pose of the face, for the log of a track nobody was named in


@dataclass
class Decision:
    kind: str  # "identified" or "unknown"
    ts: dt.datetime  # when the track was first seen
    employee_id: str | None
    score: float | None
    crop: Any
    embedding: np.ndarray
    body: Any = None
    scene: Any = None
    quality: float | None = None  # of the face in `crop` (the track's best), as the Image score / 100
    track: int | None = None  # the face track this decision ends
    follows: int | None = None  # for an unknown: the track of the unknown decision it continues, if any
    camera: int | None = None  # which camera, when there are several (set by the camera loop)
    frames: int = 0  # frames the track was seen in
    votes: int = 0  # of those, votes for `employee_id` (the decision, or the unknown's closest person)
    trace: str = ""  # for an unknown: what each frame of the track looked like and why it did not vote
    rule: str = ""  # for an identified: "agreeing frames" when named by that rule (see AGREE_MEAN), else ""


@dataclass
class _Track:
    id: int
    first_ts: dt.datetime
    last_ts: dt.datetime
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray  # of the latest face
    first_embedding: np.ndarray | None = None
    frames: int = 0
    votes: Counter = field(default_factory=Counter)
    best_score: dict[str, float] = field(default_factory=dict)
    best: Observation | None = None
    decided: str | None = None
    person_id: int | None = None
    trace: list = field(default_factory=list)
    tops: list = field(default_factory=list)  # (best match, its score) of every frame that had a match


def _alike(obs: "Observation", t: "_Track") -> float:
    """How alike a face is to a track's faces: its latest, its best and its first, whichever is closest."""
    refs = [t.embedding] + [e for e in (t.best.embedding if t.best is not None else None, t.first_embedding)
                            if e is not None]
    return max(float(np.dot(obs.embedding, e)) for e in refs)


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class FaceTracker:
    def __init__(self, params):
        """`params` has accept_score, min_margin, votes_required, track_timeout_s and unknown_min_frames
        (a RecognitionConfig); it is read on every frame so settings changes apply immediately."""
        self.p = params
        self._tracks: list[_Track] = []
        self._ended: list[_Track] = []  # undecided tracks waiting out SPLIT_WINDOW before becoming "unknown"
        self._ids = itertools.count(1)
        self._recent: dict[str, dt.datetime] = {}  # employee -> last time a track identified them
        self._people: dict[int, dt.datetime] = {}  # person tracks currently followed
        self._reported: list[_Track] = []  # the last unknown tracks reported, newest last (see _continues)
        self.agree_allowed = lambda person: True  # who may be named by agreeing frames (see AGREE_MEAN)

    def update(self, observations: list[Observation], ts: dt.datetime,
               people: dict[int, dt.datetime] | None = None, *, frame_interval_s: float = 0.0) -> list[Decision]:
        """`people`: person tracks still followed (id -> when that person first appeared), when
        person tracking is on. Their face tracks stay open while the face is out of sight.
        `frame_interval_s`: processing/sampling grace added to the missing-face timeout for live frames.
        """
        self._people = people or {}
        decisions = self._expire(ts, frame_interval_s=frame_interval_s)
        free = list(self._tracks)
        for obs in observations:
            track = self._link(obs, free, vote_for(obs.candidates, obs.quality_ok, obs.size_ok, self.p))
            if track is None:
                first = min(ts, self._people.get(obs.person_id, ts))  # arrival = when the person appeared
                track = _Track(next(self._ids), first, ts, obs.bbox, obs.embedding, first_embedding=obs.embedding,
                               person_id=obs.person_id)
                self._tracks.append(track)
            else:
                free.remove(track)
                if track.person_id is None and obs.person_id is not None:
                    track.person_id = obs.person_id
                    track.first_ts = min(track.first_ts, self._people.get(obs.person_id, track.first_ts))
            decision = self._observe(track, obs, ts)
            if decision:
                decisions.append(decision)
        return decisions

    def flush(self) -> list[Decision]:
        """End every track, e.g. when the processing window closes."""
        far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        return self._expire(far_future, force=True)

    def _link(self, obs: Observation, tracks: list[_Track], vote: str | None = None) -> _Track | None:
        if obs.person_id is not None:
            same = [t for t in tracks if t.person_id == obs.person_id]
            if same:
                if vote is not None:
                    for t in same:  # the track already voting for this person
                        if t.votes and t.votes.most_common(1)[0][0] == vote:
                            return t
                    same = [t for t in same if not t.votes]  # not one voting for someone else
                best = max(same, key=lambda t: _alike(obs, t), default=None)
                if best is not None and _alike(obs, best) >= SAME_FACE_SIM:
                    return best
                return None  # another face in this person's box: a track of its own
        best, best_value = None, 0.0
        for t in tracks:
            if obs.person_id is not None and t.person_id is not None:
                continue  # a face of one person never joins another person's track
            sim = float(np.dot(obs.embedding, t.embedding))
            overlap = iou(obs.bbox, t.bbox)
            if sim < LINK_MIN_SIM or (overlap < LINK_IOU and sim < LINK_SIM):
                continue
            if overlap + sim > best_value:
                best, best_value = t, overlap + sim
        return best

    def _observe(self, t: _Track, obs: Observation, ts: dt.datetime) -> Decision | None:
        t.last_ts, t.bbox, t.embedding = ts, obs.bbox, obs.embedding
        t.frames += 1
        if t.best is None or obs.quality > t.best.quality:
            t.best = obs
        for person, score in obs.candidates:
            t.best_score[person] = max(score, t.best_score.get(person, -1.0))
        if obs.candidates:
            t.tops.append(obs.candidates[0])
        person = vote_for(obs.candidates, obs.quality_ok, obs.size_ok, self.p)
        if person is not None:
            t.votes[person] += 1
        if len(t.trace) < TRACE_FRAMES:
            t.trace.append(_frame_note(obs, person, self.p))

        if t.decided or not t.votes:
            return None
        leader, n = t.votes.most_common(1)[0]
        others = sum(t.votes.values()) - n
        if n >= self.p.votes_required and n >= 2 * others:
            return self._decide(t, leader, ts)
        return None

    def _decide(self, t: _Track, person: str, ts: dt.datetime) -> Decision:
        t.decided = person
        self._recent[person] = ts
        return Decision("identified", t.first_ts, person, t.best_score[person], t.best.crop, t.best.embedding,
                        t.best.body, t.best.scene, t.best.quality, frames=t.frames, votes=t.votes[person])

    def _strong_exit(self, t: _Track) -> str | None:
        if len(t.votes) != 1:
            return None
        person, n = next(iter(t.votes.items()))
        strong = t.best_score.get(person, 0) >= self.p.accept_score + STRONG_BONUS
        return person if strong and n >= max(1, self.p.votes_required - 1) else None

    def _agreeing_exit(self, t: _Track) -> str | None:
        """The person every frame of `t` matched best, when they also had a vote and the average is high enough."""
        if len(t.tops) < AGREE_FRAMES or len({person for person, _ in t.tops}) != 1:
            return None
        person = t.tops[0][0]
        if not t.votes.get(person) or len(t.votes) != 1:
            return None
        if sum(score for _, score in t.tops) / len(t.tops) < AGREE_MEAN:
            return None
        try:
            return person if self.agree_allowed(person) else None
        except Exception:
            return None

    def _expire(self, now: dt.datetime, force: bool = False, frame_interval_s: float = 0.0) -> list[Decision]:
        timeout = dt.timedelta(seconds=self.p.track_timeout_s + frame_interval_s)
        alive, decisions = [], []
        for t in self._tracks:
            following = t.person_id is not None and t.person_id in self._people
            if not force and (following or now - t.last_ts <= timeout):
                alive.append(t)
            elif t.decided:
                self._recent[t.decided] = t.last_ts
            elif (person := self._strong_exit(t)) is not None:
                decisions.append(self._decide(t, person, t.last_ts))
            elif (person := self._agreeing_exit(t)) is not None:
                decision = self._decide(t, person, t.last_ts)
                decision.rule, decision.trace = "agreeing frames", " | ".join(t.trace)
                decisions.append(decision)
            elif t.frames >= self.p.unknown_min_frames:
                self._ended.append(t)
        self._tracks = alive

        waiting = []
        for t in self._ended:
            if not force and now - t.last_ts < SPLIT_WINDOW:
                waiting.append(t)
            elif not self._is_split(t):
                person, score = self._guess(t)
                decisions.append(Decision("unknown", t.first_ts, person, score, t.best.crop, t.best.embedding,
                                          t.best.body, t.best.scene, t.best.quality, track=t.id,
                                          follows=self._continues(t), frames=t.frames,
                                          votes=t.votes.get(person, 0), trace=" | ".join(t.trace)))
                self._reported = (self._reported + [t])[-REPORTED_KEPT:]
        self._ended = waiting
        return decisions

    def _continues(self, t: _Track) -> int | None:
        """The reported unknown track that `t` carries on, if any: it ended at most CONTINUE_WINDOW
        before `t` began (two faces in the picture at once, or in one frame, are two people, whatever
        the likeness), of a face that resembles this one, and not of another followed person. The most
        similar wins."""
        best, best_sim = None, LINK_MIN_SIM
        for p in reversed(self._reported):
            gap = t.first_ts - p.last_ts
            if gap <= dt.timedelta(0) or gap > CONTINUE_WINDOW:
                continue
            if t.person_id is not None and p.person_id is not None and t.person_id != p.person_id:
                continue
            sim = max(float(np.dot(t.best.embedding, p.best.embedding)),
                      float(np.dot(t.first_embedding, p.embedding)))  # where one track stopped and the next began
            if sim >= best_sim:
                best, best_sim = p.id, sim
        return best

    @staticmethod
    def _guess(t: _Track) -> tuple[str | None, float | None]:
        return max(t.best_score.items(), key=lambda x: x[1]) if t.best_score else (None, None)

    def _is_split(self, t: _Track) -> bool:
        """An undecided track right next to an identified track of the person it resembles is that
        same person with their head turned for a moment, not a stranger worth reporting."""
        person, score = self._guess(t)
        if person is None or score < LINK_MIN_SIM:
            return False
        seen = self._recent.get(person)
        return seen is not None and (abs(t.first_ts - seen) <= SPLIT_WINDOW or abs(t.last_ts - seen) <= SPLIT_WINDOW)
