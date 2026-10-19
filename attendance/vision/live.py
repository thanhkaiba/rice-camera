"""State shared between the camera loop and the admin page's live view."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

VIEWER_TIMEOUT_S = 10.0
PREVIEW_S = 15 * 60


class FrameHealth:
    """Notice a camera that keeps sending useless frames: black, blank (lens covered) or frozen.

    The stream itself looks healthy in those cases, so without this check the system would
    happily report everyone as missing.
    """

    def __init__(self, bad_after_s: float = 60.0):
        self.bad_after_s = bad_after_s
        self._prev = None
        self._problem: str | None = None
        self._since = 0.0

    def update(self, frame: np.ndarray, t: float) -> str | None:
        """Return a description of the problem once it has lasted bad_after_s, else None."""
        small = frame[::16, ::16]
        gray = small.mean(axis=2) if small.ndim == 3 else small.astype(float)
        if gray.mean() < 12:
            problem = "image is black"
        elif gray.std() < 4:
            problem = "image is blank (lens covered?)"
        elif self._prev is not None and np.array_equal(small, self._prev):
            problem = "image is frozen"
        else:
            problem = None
        self._prev = small.copy()
        if problem != self._problem:
            self._problem, self._since = problem, t
        if problem and t - self._since >= self.bad_after_s:
            return problem
        return None


@dataclass
class LiveStatus:
    camera_state: str = "off"
    problem: str | None = None  # model or configuration problem shown on the page
    models: str | None = None  # first-run model download in progress, shown on every page
    fps: float = 0.0
    faces: list[str] = field(default_factory=list)
    people: int = 0  # people followed in the last frame (person tracking)
    last_identified: str = ""
    preview_until: float = 0.0
    _jpeg: bytes | None = None
    _jpeg_time: float = 0.0
    _raw: np.ndarray | None = None  # the analysed picture without labels, for capturing reference photos
    _viewer_seen: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start_preview(self) -> None:
        self.preview_until = time.time() + PREVIEW_S

    def stop_preview(self) -> None:
        self.preview_until = 0.0

    @property
    def previewing(self) -> bool:
        return time.time() < self.preview_until

    def wants_frame(self) -> bool:
        """Only spend CPU on drawing and encoding frames while someone has the live page open."""
        return time.time() - self._viewer_seen < VIEWER_TIMEOUT_S

    def set_frame(self, jpeg: bytes, raw: np.ndarray | None = None) -> None:
        with self._lock:
            self._jpeg, self._jpeg_time = jpeg, time.time()
            self._raw = raw  # each camera picture is a new array, so no copy is needed here

    def raw_frame(self) -> np.ndarray | None:
        """The newest unlabelled picture while it is fresh (someone has the live page open)."""
        with self._lock:
            if self._raw is None or time.time() - self._jpeg_time > VIEWER_TIMEOUT_S:
                return None
            return self._raw.copy()

    def frame(self) -> tuple[bytes | None, float]:
        self._viewer_seen = time.time()
        with self._lock:
            return self._jpeg, self._jpeg_time
