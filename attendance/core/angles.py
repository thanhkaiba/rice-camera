"""Learning the angles a person's reference photos lack, with a person's confirmation.

Reference photos are collected only from faces looking at the camera (collect.py), so a turned head keeps
scoring low and people walking in a group, who look at each other, are missed. While whole people are followed
(tracking.mode person), a face belongs to a body: once the camera has named that body from a good look, the
same person's turned faces are known to be theirs. One such face per side and person a day goes to the Review
page as a card that already suggests the name; confirmed there, it becomes a reference photo (and records no
arrival). Nothing is learned without that confirmation, so a face tied to the wrong body cannot slip in.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Callable

from ..vision.recognizer import looks_covered

log = logging.getLogger(__name__)

KEY = "angle_review"  # {"day": ..., "sent": ["<employee>:left", ...]}
MAX_TURN = 75.0  # degrees: beyond this little of the face is left
MIN_OWN = 0.20  # the turned face must still look a little like the person's photos ...
WRONG_LEAD = 0.15  # ... and not clearly more like someone else: then the body was probably mixed up
PER_DAY = 30  # cards a day, everyone together


class AngleLearner:
    def __init__(self, cfg, store, report: Callable):
        self.cfg, self.store, self.report = cfg, store, report

    def _sent(self, day: dt.date) -> list[str]:
        try:
            saved = json.loads(self.store.get(KEY) or "{}")
        except ValueError:
            saved = {}
        return list(saved.get("sent", [])) if saved.get("day") == day.isoformat() else []

    def observe(self, tracks, matched, frame, now: dt.datetime) -> int:
        """`tracks`: the face tracker's tracks (person_id, decided); `matched`: (face, gallery matches, person id)
        of this frame. Returns how many cards were sent to Review."""
        c, rc = self.cfg.collect, self.cfg.recognition
        if not getattr(c, "angles", True) or self.cfg.tracking.mode != "person":
            return 0
        named = {t.person_id: t.decided for t in tracks if t.person_id is not None and t.decided}
        if not named:
            return 0
        skip = {v for v in c.skip_people.split(",") if v}
        sent, added = None, 0
        for face, candidates, pid in matched:
            employee = named.get(pid)
            if employee is None or employee in skip or face.quality_ok:
                continue
            if not face.size_ok or face.size < rc.min_face_px or not rc.max_yaw < abs(face.yaw) <= MAX_TURN:
                continue  # only a clear face that is turned: not a small, blurred or lowered one
            scores = dict(candidates)
            own = scores.get(employee)
            if own is None or own < MIN_OWN or (candidates and candidates[0][0] != employee
                                                 and candidates[0][1] - own >= WRONG_LEAD):
                continue
            if looks_covered(frame, face.landmarks):
                continue
            if sent is None:
                sent = self._sent(now.date())
            key = f"{employee}:{'left' if face.yaw < 0 else 'right'}"
            if key in sent or len(sent) >= PER_DAY:
                continue
            self.report(face, frame, now, [(employee, own)] + [(p, s) for p, s in candidates if p != employee])
            sent.append(key)
            self.store.put(KEY, json.dumps({"day": now.date().isoformat(), "sent": sent}))
            added += 1
            log.info("Sent a turned face of %s to Review (yaw %+.0f, match %.2f): confirm it to teach that angle",
                     employee, face.yaw, own)
        return added
