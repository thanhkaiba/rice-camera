"""Learn how faces normally look to this camera, so head pose is judged relative to it.

Head pose is estimated from 5 landmarks. A camera on a desk pointing up sees every face from
below; one above a door sees every face from above; one beside the walkway sees every face a
little from the side. Judged against a straight-on view, all those faces look "tilted" and would
be kept from voting. Instead, the typical yaw and pitch of faces that matched someone clearly
is learned (the median over the last few hundred), and the pose check uses the difference.
"""
from __future__ import annotations

import json
import statistics
import threading
from collections import deque
from typing import Callable

KEY = "pose_calibration"
WINDOW = 300  # recent clear matches the typical pose is taken from
MIN_SAMPLES = 30  # before this many, faces are judged against a straight-on view
LIMIT = 40.0  # never shift the reference further than this (degrees)
SAVE_EVERY = 20


class PoseCalibration:
    def __init__(self, store=None, enabled: Callable[[], bool] = lambda: True):
        self._store = store
        self._enabled = enabled
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=WINDOW)
        self._unsaved = 0
        if store is not None:
            try:
                self._samples.extend(tuple(s) for s in json.loads(store.get(KEY) or "[]"))
            except (ValueError, TypeError):
                pass

    def add(self, yaw: float, pitch: float) -> None:
        """A face that matched one person clearly: its raw pose is how this camera sees people."""
        with self._lock:
            self._samples.append((round(float(yaw), 1), round(float(pitch), 1)))
            self._unsaved += 1
            save = self._unsaved >= SAVE_EVERY
        if save:
            self.save()

    def save(self) -> None:
        if self._store is None:
            return
        with self._lock:
            data, self._unsaved = json.dumps(list(self._samples)), 0
        self._store.put(KEY, data)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
        self.save()

    @property
    def samples(self) -> int:
        return len(self._samples)

    @property
    def learned(self) -> tuple[float, float]:
        """Median (yaw, pitch) of the samples, whether or not it is in use yet."""
        with self._lock:
            items = list(self._samples)
        if not items:
            return 0.0, 0.0
        clamp = lambda v: max(-LIMIT, min(LIMIT, v))  # noqa: E731
        return clamp(statistics.median(y for y, _ in items)), clamp(statistics.median(p for _, p in items))

    @property
    def active(self) -> bool:
        return self._enabled() and self.samples >= MIN_SAMPLES

    @property
    def offset(self) -> tuple[float, float]:
        """What to subtract from a face's raw (yaw, pitch) before the pose check."""
        return self.learned if self.active else (0.0, 0.0)

    def describe(self) -> str:
        if not self._enabled():
            return "off"
        if not self.active:
            return f"learning ({self.samples}/{MIN_SAMPLES} faces)"
        yaw, pitch = self.learned
        side = "" if abs(yaw) < 3 else f", {abs(yaw):.0f}° to the {'right' if yaw > 0 else 'left'}"
        vertical = "straight on" if abs(pitch) < 3 else f"{abs(pitch):.0f}° from {'above' if pitch > 0 else 'below'}"
        return f"faces seen {vertical}{side} (from {self.samples} faces)"
