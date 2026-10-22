"""Collect reference photos outside the camera hours.

People walk past the door all day, not only in the morning. Outside the camera hours, when
nobody's arrival is being recorded, the camera keeps watching at a lower rate and keeps the
very clear faces of people it recognises without doubt, as their reference photos. Those are
the camera's own view of each person (its angle, its light, the distance), which match what
it sees in the morning far better than an uploaded photo.

A face is kept only when all of these hold, so the gallery gains good photos and never a
wrong one:
- its Image score (as shown on the person page) is at least collect.min_score, and it passes
  the quality gate (size, sharpness, head pose);
- it is a clear match (well above accept_score, twice the usual margin over the next person);
- the same person was clearly matched in an earlier frame of the same passage moments before
  (the same followed person when people are followed);
- that person has fewer than collect.enough good photos, got fewer than collect.per_day today,
  and has no photo almost identical to it already.
One photo is kept per passage at most: the next comes from another time they walk by, in
another light or pose.

A clear face (same Image score and quality gate) that nobody is recognised as is often someone
with too few reference photos: it goes to Review, once per passage, confirmed by a second look,
and not again that day for a face like one already sent, up to collect.review_per_day cards.
Naming it there teaches the face; no arrival is recorded for it.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Callable

import numpy as np

from ..vision.tracker import clear_match, vote_for

log = logging.getLogger(__name__)

CONFIRM_S = 3.0  # the earlier clear match of the same person must be this recent
COUNT_EVERY_S = 60.0  # how often the photo counts are read again from the database
SAME_FACE = 0.5  # an unrecognised face this similar to another is taken to be the same person


class Collector:
    def __init__(self, cfg, library, store, service, report: Callable | None = None):
        """`report(face, frame, ts, candidates)`: sends an unrecognised face to Review."""
        self.cfg, self._library, self._store, self._service, self._report = cfg, library, store, service, report
        self._lock = threading.Lock()
        self._seen: dict = {}  # followed person id (or employee id) -> (employee id, last clear match, kept one)
        self._day: dt.date | None = None
        self._today: dict[str, int] = {}  # employee id -> photos collected today
        self._good: dict[str, int] = {}  # employee id -> good reference photos
        self._active: set[str] = set()
        self._counted = 0.0
        self.kept_today = 0
        self._strangers: list[list] = []  # unrecognised faces of recent frames: [vector, last seen, sent to Review]
        self._reviewed: list[np.ndarray] = []  # faces sent to Review today

    def running(self, now: dt.datetime, in_window: bool) -> bool:
        """Whether to collect now: switched on, a work day, within the collect hours, outside the camera hours."""
        c = self.cfg.collect
        return (c.enabled and not in_window and self._service.is_workday(now.date())
                and c.start <= now.strftime("%H:%M") < c.end)

    def _count(self, day: dt.date) -> None:
        """Read the counts again: photos deleted or added on the People page change them."""
        model = self.cfg.recognition.model_id()
        today, good = {}, {}
        for r in self._store.references(model):
            good[r.employee_id] = good.get(r.employee_id, 0) + (r.quality_ok is True)
            created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=self._service.tz)
            if r.source == "collect" and created.astimezone(self._service.tz).date() == day:
                today[r.employee_id] = today.get(r.employee_id, 0) + 1
        self._today, self._good, self._day = today, good, day
        self._active = set(self._store.employees())
        self.kept_today = sum(today.values())
        self._reviewed = [u.vector for u in self._store.unknowns(day, status=None, source="collect")
                          if u.vector is not None]
        self._counted = time.monotonic()

    def _wanted(self, face, candidates) -> str | None:
        """The employee this face could be collected for, before the confirmation and the counts."""
        if not face.quality_ok or round(face.quality * 100) < self.cfg.collect.min_score:
            return None
        if not clear_match(candidates, self.cfg.recognition):
            return None
        return candidates[0][0]

    def _unrecognised(self, face, candidates) -> bool:
        """A clear face that nobody is recognised as, not even with the usual (lower) bar."""
        return (face.quality_ok and round(face.quality * 100) >= self.cfg.collect.min_score
                and vote_for(candidates, True, True, self.cfg.recognition) is None)

    def _stranger(self, face, frame, now: dt.datetime, candidates, at: float) -> bool:
        """Send an unrecognised face to Review once per passage and once a day; returns whether it was."""
        v = face.embedding
        same = next((s for s in self._strangers if float(np.dot(s[0], v)) >= SAME_FACE), None)
        if same is None:
            self._strangers.append([v, at, False])  # the first look: only the next one confirms a real face
            return False
        same[0], same[1] = v, at
        if same[2]:
            return False
        same[2] = True  # sent now, or not to be sent from this passage
        if (self._report is None or len(self._reviewed) >= self.cfg.collect.review_per_day
                or any(float(np.dot(r, v)) >= SAME_FACE for r in self._reviewed)):
            return False
        self._report(face, frame, now, candidates)
        self._reviewed.append(v)
        log.info("Sent an unrecognised clear face to Review (image score %d, closest %s %.2f)",
                 round(face.quality * 100), *(candidates[0] if candidates else ("nobody", 0.0)))
        return True

    def observe(self, matched, frame, now: dt.datetime, at: float | None = None) -> int:
        """The faces of one analysed frame, as (DetectedFace, gallery candidates, followed person id).
        Returns how many were kept."""
        at = time.monotonic() if at is None else at
        kept = 0
        with self._lock:
            if self._day != now.date() or time.monotonic() - self._counted >= COUNT_EVERY_S:
                self._count(now.date())
            self._seen = {k: v for k, v in self._seen.items() if at - v[1] <= CONFIRM_S}
            self._strangers = [s for s in self._strangers if at - s[1] <= CONFIRM_S]
            c = self.cfg.collect
            for face, candidates, pid in matched:
                if self._unrecognised(face, candidates):
                    self._stranger(face, frame, now, candidates, at)
                    continue
                employee = self._wanted(face, candidates)
                if employee is None or employee not in self._active:
                    continue
                key = ("person", pid) if pid is not None else ("employee", employee)
                before = self._seen.get(key)
                same = before is not None and before[0] == employee
                self._seen[key] = (employee, at, same and before[2])
                if not same or before[2]:
                    continue  # the first clear look only confirms who it is; after one photo, this passage is done
                if self._today.get(employee, 0) >= c.per_day or self._good.get(employee, 0) >= c.enough:
                    continue
                if self._library.collect(employee, face, frame):
                    self._seen[key] = (employee, at, True)
                    self._today[employee] = self._today.get(employee, 0) + 1
                    self._good[employee] = self._good.get(employee, 0) + 1
                    self.kept_today += 1
                    kept += 1
                    log.info("Collected a reference photo of %s (image score %d, match %.2f)",
                             employee, round(face.quality * 100), candidates[0][1])
        return kept

    def status(self) -> dict:
        now = self._service.now()
        return {"enabled": self.cfg.collect.enabled, "running": self.running(now, self._service.in_window(now)),
                "kept_today": self.kept_today if self._day == now.date() else 0,
                "reviewed_today": len(self._reviewed) if self._day == now.date() else 0,
                "start": self.cfg.collect.start, "end": self.cfg.collect.end, "min_score": self.cfg.collect.min_score}
