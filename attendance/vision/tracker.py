"""Follow each face across frames and decide only after several consistent, good-quality frames.

A single frame is never trusted: a person is identified when at least `votes_required`
good frames agree on them with a clear margin over the runner-up. Tracks that end without
a decision become "unknown" so an admin can look at them.

With person tracking on, each face carries the id of the person (body) it belongs to. Faces of
the same person always join the same track, faces of different people never do, and the track
lives as long as the person is followed, even while their face is hidden.
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
SPLIT_WINDOW = dt.timedelta(seconds=5)
# A track that ends one vote short is still accepted when its votes are unanimous and this much
# above accept_score: someone walking fast past a slow PC may only give two good frames.
STRONG_BONUS = 0.1
# A face that fails the pose or sharpness check still votes when it matches one person this far
# above accept_score, with twice the usual margin. The pose is only estimated from 5 landmarks,
# and a camera mounted above head height makes every face look tilted down; tested on doorway
# video, such faces matched the right person at 0.85-0.9 while everyone else stayed below 0.2.
STRONG_MATCH = 0.15


def clear_match(candidates, params) -> bool:
    """One person far above accept_score, with twice the usual margin over the next."""
    if not candidates:
        return False
    top = candidates[0][1]
    margin = top - (candidates[1][1] if len(candidates) > 1 else -1.0)
    return top >= params.accept_score + STRONG_MATCH and margin >= 2 * params.min_margin


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


@dataclass
class _Track:
    id: int
    first_ts: dt.datetime
    last_ts: dt.datetime
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray
    frames: int = 0
    votes: Counter = field(default_factory=Counter)
    best_score: dict[str, float] = field(default_factory=dict)
    best: Observation | None = None
    decided: str | None = None
    person_id: int | None = None


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
            track = self._link(obs, free)
            if track is None:
                first = min(ts, self._people.get(obs.person_id, ts))  # arrival = when the person appeared
                track = _Track(next(self._ids), first, ts, obs.bbox, obs.embedding, person_id=obs.person_id)
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

    def _link(self, obs: Observation, tracks: list[_Track]) -> _Track | None:
        if obs.person_id is not None:
            for t in tracks:
                if t.person_id == obs.person_id:
                    return t
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
        person = vote_for(obs.candidates, obs.quality_ok, obs.size_ok, self.p)
        if person is not None:
            t.votes[person] += 1

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
                        t.best.body, t.best.scene, t.best.quality)

    def _strong_exit(self, t: _Track) -> str | None:
        if len(t.votes) != 1:
            return None
        person, n = next(iter(t.votes.items()))
        strong = t.best_score.get(person, 0) >= self.p.accept_score + STRONG_BONUS
        return person if strong and n >= max(1, self.p.votes_required - 1) else None

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
                                          t.best.body, t.best.scene, t.best.quality))
        self._ended = waiting
        return decisions

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
