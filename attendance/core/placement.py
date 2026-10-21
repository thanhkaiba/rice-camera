"""How well the camera sees faces, per day: how many faces it detected during the camera hours,
how big they were and why they could not be used. The daily summary in chat gets a hint when the
numbers say the camera is in the wrong place (pointing away, too far, seeing people from the side).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import RecognitionConfig
    from ..store import Store

KEY = "placement:"  # + YYYY-MM-DD in the kv table
BUCKET_PX = 10
BUCKETS = 31  # face sizes 0-9, 10-19, ... 300+ px
MIN_CONNECTED_S = 1800
MIN_FACES = 10  # fewer detections say nothing about the placement
SHARE = 0.5


def empty() -> dict:
    return {"frames": 0, "connected_s": 0.0, "faces": 0, "small": 0, "pose": 0, "blur": 0, "good": 0,
            "sizes": [0] * BUCKETS}


def classify(face, rc: "RecognitionConfig") -> str:
    """Why a detected face can or cannot be used, in the order the live view labels them."""
    if face.size < rc.min_face_px:
        return "small"
    if face.sharpness < rc.min_sharpness:
        return "blur"
    if abs(face.yaw) > rc.max_yaw or abs(face.pitch) > rc.max_pitch:
        return "pose"
    return "good"


def median_size(sizes: list[int]) -> int | None:
    total = sum(sizes)
    if not total:
        return None
    seen = 0
    for i, n in enumerate(sizes):
        seen += n
        if seen * 2 >= total:
            return i * BUCKET_PX + BUCKET_PX // 2
    return None


def hints(day: dict, min_face_px: int) -> list[str]:
    """Placement advice for one day's numbers; empty when they look fine or say too little."""
    faces = day.get("faces", 0)
    if faces == 0:
        if day.get("connected_s", 0) >= MIN_CONNECTED_S:
            return ["📷 The camera worked but saw no faces at all: it may be pointing away from the walkway, "
                    "or the detection area misses it. Check the picture on the Camera page."]
        return []
    if faces < MIN_FACES:
        return []
    out = []
    if day.get("small", 0) / faces > SHARE:
        size = median_size(day.get("sizes", []))
        out.append(f"📷 Most faces were too small for recognition (typically {size} px, needs {min_face_px}): "
                   "move the camera closer to where people walk, or zoom in.")
    if day.get("pose", 0) / faces > SHARE:
        out.append("📷 Most faces were seen from the side: people pass the camera sideways. "
                   "Point the camera at the door so people walk towards it.")
    return out


class Placement:
    """Counts for today in memory, saved to the database now and then and at the end of the camera
    hours; a restart carries on from what was saved."""

    def __init__(self, store: "Store"):
        self.store = store
        self._day: dt.date | None = None
        self._stats = empty()
        self._lock = threading.Lock()

    def _today(self, day: dt.date) -> dict:
        if self._day != day:
            if self._day is not None:
                self._save_locked()
            self._day, self._stats = day, self.load(day)
        return self._stats

    def add_frame(self, day: dt.date, faces, rc: "RecognitionConfig", gap_s: float) -> None:
        """One analysed frame of the camera hours with its detected faces; `gap_s` is the time since
        the previous analysed frame, counted as connected time when the camera kept sending."""
        with self._lock:
            s = self._today(day)
            s["frames"] += 1
            s["connected_s"] += min(max(gap_s, 0.0), 5.0)
            for f in faces:
                s["faces"] += 1
                s[classify(f, rc)] += 1
                s["sizes"][min(BUCKETS - 1, int(f.size) // BUCKET_PX)] += 1

    def load(self, day: dt.date) -> dict:
        try:
            saved = json.loads(self.store.get(KEY + day.isoformat()) or "{}")
        except ValueError:
            saved = {}
        out = empty()
        out.update({k: v for k, v in saved.items() if k in out})
        if len(out["sizes"]) != BUCKETS:
            out["sizes"] = [0] * BUCKETS
        return out

    def _save_locked(self) -> None:
        if self._day is not None and self._stats["frames"]:
            self.store.put(KEY + self._day.isoformat(), json.dumps(self._stats))

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def day(self, day: dt.date) -> dict:
        with self._lock:
            if day == self._day:
                return json.loads(json.dumps(self._stats))
        return self.load(day)

    def hints(self, day: dt.date, min_face_px: int) -> list[str]:
        return hints(self.day(day), min_face_px)

    def recent(self, today: dt.date, days: int, min_face_px: int) -> list[dict]:
        """Newest first, days with camera data only, with shares and hints for the Status page."""
        rows = []
        for back in range(days):
            d = today - dt.timedelta(days=back)
            s = self.day(d)
            if not s["frames"]:
                continue
            n = s["faces"]
            rows.append({"day": d, "faces": n, "frames": s["frames"], "connected_min": round(s["connected_s"] / 60),
                         "median_px": median_size(s["sizes"]),
                         **{k: (s[k] / n if n else None) for k in ("small", "pose", "blur", "good")},
                         "hints": hints(s, min_face_px)})
        return rows
