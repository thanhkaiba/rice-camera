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
    source_fps: float = 0.0  # frames a second the camera delivers and the computer decodes
    faces: list[str] = field(default_factory=list)
    people: int = 0  # people followed in the last frame (person tracking)
    last_identified: str = ""  # the last arrival recorded (a track that reached a decision)
    last_match: str = ""  # the last face that matched someone in a single frame, counted or not
    cameras: list[dict] = field(default_factory=list)  # one {index, label, state, fps, source_fps, faces} per camera
    viewed: int = 0  # the camera the live page shows; only its picture is drawn and encoded
    preview_until: float = 0.0
    _frames: dict[int, tuple[bytes, float, np.ndarray | None]] = field(default_factory=dict)  # per camera
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

    def set_frame(self, jpeg: bytes, raw: np.ndarray | None = None, camera: int = 0) -> None:
        with self._lock:
            self._frames[camera] = (jpeg, time.time(), raw)  # each camera picture is a new array: no copy needed

    def raw_frame(self, camera: int | None = None) -> np.ndarray | None:
        """The newest unlabelled picture of a camera (the viewed one by default) while it is fresh
        (someone has the live page open)."""
        with self._lock:
            jpeg, at, raw = self._frames.get(self.viewed if camera is None else camera, (None, 0.0, None))
            if raw is None or time.time() - at > VIEWER_TIMEOUT_S:
                return None
            return raw.copy()

    def frame(self, camera: int | None = None) -> tuple[bytes | None, float]:
        """The labelled picture of a camera; asking for one makes it the viewed camera."""
        self._viewer_seen = time.time()
        if camera is not None:
            self.viewed = max(0, camera)
        with self._lock:
            jpeg, at, _ = self._frames.get(self.viewed if camera is None else camera, (None, 0.0, None))
            return jpeg, at
