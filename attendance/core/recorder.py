"""A picture a second during the camera hours, kept a few days, to play back the moment someone arrived.

The faces the camera keeps (crops, scenes) show only the moments it found a face; someone walking in behind
a colleague, with no face found, leaves nothing. This keeps the whole picture once a second, shrunk to
WIDTH pixels, in data/recordings/<day>/<HHMMSS>.jpg, for camera.record_days days (0 = off). The work is done
on a thread of its own and a busy moment just skips a picture: recognition is never held up.

On the server, the Moments page asks the office computer for the pictures of a time span (a "moment" job):
they are uploaded once as crops/<day>/rec_<HHMMSS>.jpg and expire with the same number of days.
"""
from __future__ import annotations

import datetime as dt
import logging
import queue
import re
import shutil
import threading
from pathlib import Path

log = logging.getLogger(__name__)

FOLDER = "recordings"
WIDTH = 1280
QUALITY = 75
MAX_SPAN_S = 180  # the longest moment one request may fetch
NAME = re.compile(r"(\d{6})\.jpg")
UPLOADED = re.compile(r"rec_(\d{6})\.jpg")


def day_folder(data_dir: Path, day: dt.date) -> Path:
    return Path(data_dir) / FOLDER / day.isoformat()


def recorded(data_dir: Path, day: dt.date, start: dt.time, end: dt.time) -> list[tuple[str, Path]]:
    """(HH:MM:SS, picture) kept on this computer for `day` between `start` and `end`, in time order."""
    folder, lo, hi = day_folder(data_dir, day), start.strftime("%H%M%S"), end.strftime("%H%M%S")
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        m = NAME.fullmatch(p.name)
        if m and lo <= m.group(1) <= hi:
            t = m.group(1)
            out.append((f"{t[:2]}:{t[2:4]}:{t[4:]}", p))
    return out


def uploaded(data_dir: Path, day: dt.date, start: dt.time, end: dt.time) -> list[tuple[str, str]]:
    """(HH:MM:SS, path relative to the data folder) of the pictures the office computer sent for `day`."""
    folder, lo, hi = Path(data_dir) / "crops" / day.isoformat(), start.strftime("%H%M%S"), end.strftime("%H%M%S")
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        m = UPLOADED.fullmatch(p.name)
        if m and lo <= m.group(1) <= hi:
            t = m.group(1)
            out.append((f"{t[:2]}:{t[2:4]}:{t[4:]}", f"crops/{day.isoformat()}/{p.name}"))
    return out


def expire(data_dir: Path, today: dt.date, days: int) -> int:
    """Delete recorded days, and pictures uploaded from them, older than `days` (all of them when 0)."""
    cutoff = (today - dt.timedelta(days=max(days, 0) - 1)).isoformat() if days > 0 else "9999"
    removed = 0
    root = Path(data_dir) / FOLDER
    if root.is_dir():
        for folder in root.iterdir():
            if folder.is_dir() and folder.name < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    crops = Path(data_dir) / "crops"
    if crops.is_dir():
        for folder in crops.iterdir():
            if folder.is_dir() and folder.name < cutoff:
                for p in folder.glob("rec_*.jpg"):
                    p.unlink(missing_ok=True)
    return removed


class Recorder:
    def __init__(self, cfg, data_dir: Path):
        self.cfg, self.data_dir = cfg, Path(data_dir)
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._last = 0.0
        self._expired: dt.date | None = None
        self.kept = 0
        self.error: str | None = None
        threading.Thread(target=self._loop, name="recorder", daemon=True).start()

    def offer(self, frame, ts: dt.datetime, frame_time: float) -> bool:
        """Hand over a camera frame; one a second is kept. Never waits: a full queue skips the picture."""
        if self.cfg.camera.record_days <= 0 or frame_time - self._last < 1.0:
            return False
        self._last = frame_time
        try:
            self._queue.put_nowait((frame, ts))
        except queue.Full:
            return False
        return True

    def _loop(self) -> None:
        while True:
            frame, ts = self._queue.get()
            try:
                self._write(frame, ts)
            except Exception as exc:
                self.error = f"{ts:%H:%M} {type(exc).__name__}: {str(exc)[:200]}"
                log.exception("A recorded picture could not be kept")

    def _write(self, frame, ts: dt.datetime) -> None:
        import cv2

        if self._expired != ts.date():
            self._expired = ts.date()
            expire(self.data_dir, ts.date(), self.cfg.camera.record_days)
        h, w = frame.shape[:2]
        if w > WIDTH:
            frame = cv2.resize(frame, (WIDTH, int(h * WIDTH / w)), interpolation=cv2.INTER_AREA)
        cv2.putText(frame, f"{ts:%Y-%m-%d %H:%M:%S}", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, f"{ts:%Y-%m-%d %H:%M:%S}", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
        if not ok:
            raise ValueError("could not encode the picture")
        folder = day_folder(self.data_dir, ts.date())
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{ts:%H%M%S}.jpg"
        part = path.with_suffix(".part")
        part.write_bytes(buf.tobytes())
        part.replace(path)
        self.kept += 1
