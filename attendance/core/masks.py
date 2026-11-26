"""Ask someone in a mask to lower it when the camera cannot name them.

A mask hides most of what the face model compares: masked faces at the door are rarely named (25/11/2026:
groups coming out of the lift in masks, six people typed in by hand). Asked once, most people lower the
mask for a moment and are named as on any other day.

A face counts when person tracking follows it, it is big enough to be named, its lower half looks covered
(recognizer.looks_covered) and nobody has been named for that person yet. Once that has lasted ASK_AFTER_S
over at least ASK_FRAMES frames, the greeter is asked to say it, once per person.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..vision.recognizer import looks_covered

log = logging.getLogger(__name__)

ASK_FRAMES = 3  # covered frames of one person, not named, before asking
ASK_AFTER_S = 1.5  # and this long since the first of them
FORGET_S = 60.0  # a person not seen this long is forgotten


class MaskWatch:
    def __init__(self, cfg, ask):
        """`ask(ts)`: says the request (Greeter.mask_nudge)."""
        self.cfg, self.ask = cfg, ask
        self._seen: dict[int, list] = {}  # person id -> [first covered ts, covered frames, last ts, asked]

    def observe(self, tracks, matched, frame, ts: dt.datetime) -> bool:
        """`tracks`: the face tracker's tracks (person_id, decided); `matched`: (face, gallery matches, person id)
        of this frame. True when the request was made."""
        if not getattr(self.cfg.greeting, "mask_nudge", False) or not self.cfg.greeting.enabled:
            return False
        for pid in [p for p, s in self._seen.items() if (ts - s[2]).total_seconds() > FORGET_S]:
            del self._seen[pid]
        named = {id(t.embedding) for t in tracks if t.decided}  # the face's own track, not its person box
        rc, asked = self.cfg.recognition, False
        for face, _, pid in matched:
            if pid is None or id(face.embedding) in named or face.size < rc.min_face_px:
                continue
            if not looks_covered(frame, face.landmarks):
                continue
            seen = self._seen.setdefault(pid, [ts, 0, ts, False])
            seen[1] += 1
            seen[2] = ts
            if seen[3] or seen[1] < ASK_FRAMES or (ts - seen[0]).total_seconds() < ASK_AFTER_S:
                continue
            for other in self._seen.values():  # one request covers everyone in a mask at the door now
                if (ts - other[2]).total_seconds() <= ASK_AFTER_S:
                    other[3] = True
            if not asked:
                self.ask(ts)
                log.info("A face in a mask was not named after %.1f s: asking to lower it",
                         (ts - seen[0]).total_seconds())
                asked = True
        return asked
