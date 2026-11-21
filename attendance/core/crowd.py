"""Groups at the door: how many of the people who came in together did the camera name?

Whenever GROUP_MIN or more people are in the picture at once, the moment is followed until they have walked
on. Then one picture of the scene is kept (the frame with the most people, every person and face marked) with
the count: how many people, how many faces found, how many named. The portal's Groups page adds them up per
day, so "the camera misses one or two in a group" becomes a number that can be watched while settings change.

People are counted as bodies when tracking.mode is "person" (so someone whose face was never found still
counts as missed), and as faces otherwise (then a face that was never found is not counted at all).
Someone walking away from the camera counts as a person not named: read the rate on the morning's arrivals.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from typing import TYPE_CHECKING

from ..vision.tracker import vote_for

if TYPE_CHECKING:
    from ..store import Store

log = logging.getLogger(__name__)

GROUP_MIN = 3  # people in the picture at once for a group
END_GAP_S = 3.0  # fewer than GROUP_MIN for this long: the group has walked on
MAX_S = 45.0  # a group that never leaves (people talking at the door) is closed and counted after this long
PICTURE_SIDE = 1280  # the kept picture's longer side, pixels
NAMED, FACE, NOBODY = (60, 200, 60), (0, 150, 255), (40, 40, 240)  # BGR: named, face not named, no face found
PERSON = (255, 190, 60)  # a person whose face was found: light blue


def picture(frame, people, faces, named_faces: set[int], label: str) -> bytes:
    """The scene as a JPEG: a box around every person (light blue, red when no face of theirs was found), and
    around every face (green when it was named in this frame, orange when not)."""
    import cv2

    from ..vision.faces import encode_jpeg

    image = frame.copy()
    with_face = {pid for _, _, pid in faces if pid is not None}
    for p in people:
        x1, y1, x2, y2 = (int(v) for v in p.box)
        cv2.rectangle(image, (x1, y1), (x2, y2), PERSON if p.id in with_face else NOBODY, 3)
    for n, (f, _, _) in enumerate(faces):
        x1, y1, x2, y2 = (int(v) for v in f.bbox)
        cv2.rectangle(image, (x1, y1), (x2, y2), NAMED if n in named_faces else FACE, 3)
    scale = PICTURE_SIDE / max(image.shape[:2])
    if scale < 1:
        image = cv2.resize(image, (int(image.shape[1] * scale), int(image.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    cv2.rectangle(image, (0, 0), (image.shape[1], 30), (0, 0, 0), -1)
    cv2.putText(image, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    return encode_jpeg(image)


class CrowdLog:
    """Call `observe` with every analysed frame of one camera."""

    def __init__(self, cfg, store: "Store", data_dir: Path, send=None):
        self.cfg, self.store, self.data_dir, self.send = cfg, store, Path(data_dir), send
        self._open: dict | None = None

    def observe(self, ts: dt.datetime, frame, faces: list, people: list, decisions: list = ()) -> dict | None:
        """`faces`: (face, gallery matches, person id) as VideoProcessor.process returns them. Returns the group
        that has just ended, once it is kept."""
        p = self.cfg.recognition
        count = max(len(people), len(faces))
        named_faces, names = set(), set()
        for n, (f, candidates, _) in enumerate(faces):
            person = vote_for(candidates, f.quality_ok, f.size_ok, p)
            if person is not None:
                named_faces.add(n)
                names.add(person)
        names |= {d.employee_id for d in decisions if d.kind == "identified" and d.employee_id}
        g = self._open
        if g is None:
            if count < GROUP_MIN:
                return None
            g = self._open = {"start": ts, "last": ts, "people": 0, "faces": 0, "names": set(), "frame": None}
        g["names"] |= names
        if count >= GROUP_MIN:
            g["last"] = ts
            if (count, len(faces)) > (g["people"], g["faces"]) or g["frame"] is None:
                # The boxes as they are now: the trackers go on moving their people after this frame.
                seen = [SimpleNamespace(id=p.id, box=np.array(p.box, dtype=np.float32)) for p in people]
                g["frame"] = (frame.copy(), seen, list(faces), named_faces, ts)
            g["people"], g["faces"] = max(g["people"], count), max(g["faces"], len(faces))
        if (ts - g["last"]).total_seconds() < END_GAP_S and (ts - g["start"]).total_seconds() < MAX_S:
            return None
        self._open = None
        return self._keep(g)

    def _keep(self, g: dict) -> dict | None:
        frame, people, faces, named_faces, at = g["frame"]
        named = min(len(g["names"]), g["people"])
        row = {"id": uuid.uuid4().hex, "day": g["start"].date().isoformat(), "start": g["start"].isoformat(),
               "end": g["last"].isoformat(), "people": g["people"], "faces": g["faces"], "named": named,
               "names": sorted(g["names"]), "bodies": bool(people), "image": None}
        try:
            rel = Path("crops") / row["day"] / f"crowd_{g['start']:%H%M%S}_{row['id'][:12]}.jpg"
            path = self.data_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            label = (f"{at:%Y-%m-%d %H:%M:%S}  {g['people']} {'people' if people else 'faces'}  "
                     f"{g['faces']} faces found  {named} named")
            path.write_bytes(picture(frame, people, faces, named_faces, label))
            row["image"] = rel.as_posix()
        except Exception:
            log.exception("The group's picture could not be kept")
        self.store.add_crowd(row)
        log.info("Group at %s: %d %s, %d faces found, %d named (%d%%)", g["start"].strftime("%H:%M:%S"), g["people"],
                 "people" if people else "faces", g["faces"], named, round(100 * named / max(g["people"], 1)))
        if self.send is not None:
            self.send(row)
        return row
