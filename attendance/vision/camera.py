"""Read an RTSP stream on a background thread, keeping the last few frames.

Reading on a separate thread prevents the decoder buffer from filling up while a frame is
being analysed, which would otherwise make the picture lag further and further behind.

The camera sends 15-25 frames a second and only a few are analysed. Of the frames that came
in since the last one analysed, the sharpest where the faces are is picked: a Wi-Fi camera
and people walking past give many motion-blurred frames, and a blurred face does not vote.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

# RTSP over TCP: slower to start than UDP, but no smeared frames on a weak Wi-Fi link.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
_BASE_OPTIONS = os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]  # as set by the user or above

import cv2  # noqa: E402

log = logging.getLogger(__name__)

STALE_AFTER_S = 5.0
# A stalled stream is dropped after READ_TIMEOUT_MS and reopened, instead of FFmpeg's default 30 s
# during which nobody walking past is seen; a camera that is slow to answer gets OPEN_TIMEOUT_MS.
OPEN_TIMEOUT_MS = 10_000
READ_TIMEOUT_MS = 5_000
BUFFER_FRAMES = 6
RATE_WINDOW_S = 5.0  # frames received over this long give the camera's frame rate
# OpenCV reads the FFmpeg options from the environment when a stream is opened; the environment is
# shared by the whole program, so two cameras opening at once must not mix their options.
_open_lock = threading.Lock()


def capture_options(base: str, threads: int) -> str:
    """FFmpeg capture options: `base` ("key;value|key;value") with the decoder thread count set,
    replacing a threads option already in it. 0 threads keeps FFmpeg's choice (all cores)."""
    parts = [p for p in base.split("|") if p and p.split(";", 1)[0] != "threads"]
    if threads > 0:
        parts.append(f"threads;{threads}")
    return "|".join(parts)


def open_capture(url, threads: int = 0):
    """cv2.VideoCapture for a stream or file with `threads` decoder threads; a webcam index as is."""
    if not isinstance(url, str):
        return cv2.VideoCapture(url)
    with _open_lock:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = capture_options(_BASE_OPTIONS, threads)
        try:
            return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                                                         cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS])
        finally:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _BASE_OPTIONS


def focus(frame, regions) -> float:
    """Sharpness of a frame inside `regions` (x1, y1, x2, y2): mean Laplacian variance. Only the
    face areas are scored; the static background is always sharp and would hide the difference."""
    total, n = 0.0, 0
    h, w = frame.shape[:2]
    for x1, y1, x2, y2 in regions:
        x1, y1, x2, y2 = max(0, int(x1)), max(0, int(y1)), min(w, int(x2)), min(h, int(y2))
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        crop = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        total += float(cv2.Laplacian(crop, cv2.CV_64F).var())
        n += 1
    return total / n if n else 0.0


class Camera:
    def __init__(self, url: str, decode_threads: int = 0, name: str = "camera"):
        self.source = url
        self.name = name  # for the log, when there are several cameras
        self.url = int(url) if url.isdigit() else url
        self.decode_threads = decode_threads
        self._arrivals: deque = deque()  # when recent frames came in, for the camera's frame rate
        self._started = time.time()
        self._frame = None
        self._frame_time = 0.0
        self._recent: deque = deque(maxlen=BUFFER_FRAMES)  # (frame, time)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self) -> "Camera":
        self._thread.start()
        return self

    def stop(self, wait_s: float = 10.0) -> None:
        """Ask the reader to stop. A reader stuck in a hung decoder call cannot be interrupted;
        it is left behind (daemon thread) and ignored once `wait_s` is over."""
        self._stop.set()
        self._thread.join(timeout=wait_s)

    def started_for(self) -> float:
        return time.time() - self._started

    def silent_for(self) -> float:
        """Seconds since the last frame (or since start, if there never was one)."""
        with self._lock:
            return time.time() - (self._frame_time or self._started)

    def received_fps(self) -> float:
        """Frames a second the camera actually delivers (and the computer decodes), lately."""
        with self._lock:
            now = time.time()
            while self._arrivals and now - self._arrivals[0] > RATE_WINDOW_S:
                self._arrivals.popleft()
            if len(self._arrivals) < 2:
                return 0.0
            return (len(self._arrivals) - 1) / max(1e-6, self._arrivals[-1] - self._arrivals[0])

    def latest(self):
        """Return (frame, unix_time) of the newest frame, or (None, 0) if the stream is down."""
        with self._lock:
            if self._frame is None or time.time() - self._frame_time > STALE_AFTER_S:
                return None, 0.0
            return self._frame, self._frame_time

    def sharpest(self, after: float, regions=()):
        """Like latest(), but of the frames newer than `after`, the one whose `regions` (where the
        faces were a moment ago) are sharpest. Without regions there is nothing to compare, so
        the newest frame is returned at no cost."""
        frame, frame_time = self.latest()
        if frame is None or not regions:
            return frame, frame_time
        with self._lock:
            fresh = [(f, t) for f, t in self._recent if t > after]
        if len(fresh) <= 1:
            return frame, frame_time
        # Scored outside the lock so the reader thread is never held up mid-decode.
        return max(fresh, key=lambda ft: focus(ft[0], regions))

    def _open(self):
        if not isinstance(self.url, str):
            return cv2.VideoCapture(self.url)  # a webcam
        if "://" not in self.url:
            return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)  # a video file
        return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                                                          cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS])

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            cap = open_capture(self.url, self.decode_threads)
            if not cap.isOpened():
                log.warning("Cannot open the %s, retrying in %.0fs", self.name, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            log.info("%s connected", self.name.capitalize())
            backoff = 1.0
            # A recorded video file is played at its real speed, so it can stand in for the camera when testing.
            is_file = isinstance(self.url, str) and "://" not in self.url
            frame_gap = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 25.0) if is_file else 0.0
            while not self._stop.is_set():
                if frame_gap:
                    time.sleep(frame_gap)
                ok, frame = cap.read()
                if not ok:
                    log.warning("%s stream interrupted, reconnecting", self.name.capitalize())
                    break
                now = time.time()
                with self._lock:
                    self._frame, self._frame_time = frame, now
                    self._recent.append((frame, now))
                    self._arrivals.append(now)
                    if len(self._arrivals) > 200:
                        self._arrivals.popleft()
            cap.release()
