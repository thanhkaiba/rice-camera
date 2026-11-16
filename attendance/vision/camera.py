"""Read an RTSP stream on a background thread, keeping the last few frames.

Reading on a separate thread prevents the decoder buffer from filling up while a frame is
being analysed, which would otherwise make the picture lag further and further behind.

The camera sends 15-25 frames a second and only a few are analysed. Of the frames that came
in since the last one analysed, the sharpest where the faces are is picked: a Wi-Fi camera
and people walking past give many motion-blurred frames, and a blurred face does not vote.

Frames are stamped with the time the camera took them, from the stream's own clock, not with the
time they reached this computer: over Wi-Fi a stream stalls for a second and then delivers that
second's frames all at once. The last frames are kept, so the ones that came in a burst are still
analysed one after the other, each at its real time, instead of only the newest of them.
"""
from __future__ import annotations

import logging
import math
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
# Frames waiting to be analysed: as many as fit in BUFFER_BYTES (a 4K frame is 25 MB), within these
# limits, and none taken more than BACKLOG_S before the newest: the analysis is never further behind
# than that. When a burst brings more than fit, the frames closest together in time go first, so
# the ones kept are spread over the whole burst.
BUFFER_FRAMES = (6, 16)
BUFFER_BYTES = 200_000_000
BACKLOG_S = 2.0
# The stream's clock is tied to this computer's by the frames that arrived with the least delay.
# A frame is never taken to be older than MAX_LATE_S; CLOCK_LEAK_S a frame lets the tie follow a
# camera clock that runs a little slower than this computer's.
MAX_LATE_S = 5.0
CLOCK_LEAK_S = 0.0005
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


class StreamClock:
    """When each frame of one connection was taken, on this computer's clock.

    `position` is the frame's time in the stream (seconds since the stream started), which advances
    evenly however unevenly the frames arrive. The frame that arrived with the least delay so far
    says how the two clocks relate; a frame arriving later than that was held up on the way."""

    def __init__(self):
        self._offset: float | None = None  # arrival - position of the least delayed frame
        self._position = -math.inf
        self._last = 0.0

    def at(self, arrival: float, position: float | None) -> float:
        if position is None or not math.isfinite(position) or position <= self._position:
            self._last = max(arrival, self._last)  # no usable stream time: when it arrived
            return self._last
        self._position = position
        offset = arrival - position
        if self._offset is None or offset < self._offset or offset - self._offset > MAX_LATE_S:
            self._offset = offset
        else:
            self._offset += min(offset - self._offset, CLOCK_LEAK_S)
        self._last = max(min(arrival, self._offset + position), self._last + 0.001)  # never out of order
        return self._last


def buffer_frames(frame) -> int:
    """How many frames like `frame` are kept."""
    low, high = BUFFER_FRAMES
    return max(low, min(high, BUFFER_BYTES // max(1, frame.nbytes)))


def trim(frames: deque, keep: int) -> None:
    """Make room in the buffer (see BUFFER_FRAMES): drop what is too old, then thin what is left."""
    newest = frames[-1][1]
    while len(frames) > 1 and newest - frames[0][1] > BACKLOG_S:
        frames.popleft()
    while len(frames) > max(2, keep):
        # The frame whose neighbours are closest together is missed least; never the oldest or newest.
        i = min(range(1, len(frames) - 1), key=lambda i: frames[i + 1][1] - frames[i - 1][1])
        del frames[i]


def pick(frames, last_chosen: float, due, regions=()):
    """The frame to analyse next, or None when none is due yet.

    `frames` are the buffered (frame, time), oldest first; `due(time)` says whether a frame of that
    time is far enough after the last analysis. The first due frame sets the pace, so frames that
    arrived in a burst are analysed in turn. Of the frames since the last one chosen up to that one,
    the one whose `regions` (where the faces were a moment ago) are sharpest is analysed; without
    regions there is nothing to compare. Returns (frame, its time, the due frame's time)."""
    for i, (_, t) in enumerate(frames):
        if t > last_chosen and due(t):
            break
    else:
        return None
    fresh = [ft for ft in frames[:i + 1] if ft[1] > last_chosen]
    # Scored outside the camera's lock so the reader thread is never held up mid-decode.
    frame, t = max(fresh, key=lambda ft: focus(ft[0], regions)) if regions and len(fresh) > 1 else frames[i]
    return frame, t, frames[i][1]


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
        self._recent: deque = deque()  # (frame, time it was taken) not analysed yet; see trim()
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
        """Return (frame, when it arrived) of the newest frame, or (None, 0) if the stream is down."""
        with self._lock:
            if self._frame is None or time.time() - self._frame_time > STALE_AFTER_S:
                return None, 0.0
            return self._frame, self._frame_time

    def pending(self, after: float = 0.0) -> list:
        """The buffered (frame, time it was taken) taken after `after`, oldest first; empty if the
        stream is down. Frames up to `after` have been dealt with and are let go."""
        with self._lock:
            while self._recent and self._recent[0][1] <= after:
                self._recent.popleft()
            if self._frame is None or time.time() - self._frame_time > STALE_AFTER_S:
                return []
            return list(self._recent)

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
            is_stream = isinstance(self.url, str) and "://" in self.url
            clock = StreamClock()
            while not self._stop.is_set():
                if frame_gap:
                    time.sleep(frame_gap)
                ok, frame = cap.read()
                if not ok:
                    log.warning("%s stream interrupted, reconnecting", self.name.capitalize())
                    break
                now = time.time()
                taken = clock.at(now, cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if is_stream else None)
                keep = buffer_frames(frame)
                with self._lock:
                    self._frame, self._frame_time = frame, now
                    self._recent.append((frame, taken))
                    trim(self._recent, keep)
                    self._arrivals.append(now)
                    if len(self._arrivals) > 200:
                        self._arrivals.popleft()
            cap.release()
