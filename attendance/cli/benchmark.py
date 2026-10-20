"""Run a recorded video through both tracking modes and compare speed and results.

Nothing is written to the database: use it to see whether this computer is fast enough for
person tracking, and whether person tracking finds more people in your own entrance videos.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..vision.processing import FrameProcessor


@dataclass
class Result:
    mode: str
    frames: int = 0
    seconds: float = 0.0
    identified: list[tuple[float, str]] = field(default_factory=list)  # (video second, employee id)
    unknowns: int = 0
    note: str = ""

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds else 0.0


def run_mode(app, video: Path, mode: str, fps: float) -> Result:
    import cv2

    processor = FrameProcessor(app, mode, realtime=False)
    result = Result(mode)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"Cannot open {video}")
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(video_fps / fps))
    start = dt.datetime.now(app.service.tz)
    index = 0

    def collect(decisions, ts):
        for d in decisions:
            if d.kind == "identified":
                result.identified.append(((ts - start).total_seconds(), d.employee_id))
            else:
                result.unknowns += 1

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            index += 1
            if (index - 1) % step:
                continue
            ts = start + dt.timedelta(seconds=(index - 1) / video_fps)
            t0 = time.perf_counter()
            decisions, _, _ = processor.process(frame, ts)
            result.seconds += time.perf_counter() - t0
            result.frames += 1
            collect(decisions, ts)
        collect(processor.flush(), start + dt.timedelta(seconds=index / video_fps))
    finally:
        cap.release()
    if processor.person_error:
        result.note = processor.person_error
    return result


def benchmark(app, video: Path, modes: list[str]) -> list[Result]:
    fps = app.cfg.camera.process_fps
    names = app.store.employees()
    results = []
    for mode in modes:
        print(f"\n== {mode} mode ==")
        r = run_mode(app, video, mode, fps)
        results.append(r)
        if r.note:
            print(r.note)
        print(f"{r.frames} frames in {r.seconds:.1f} s: {r.fps:.1f} frames/s "
              f"({1000 * r.seconds / max(r.frames, 1):.0f} ms per frame; camera setting is {fps:g})")
        for second, emp in r.identified:
            print(f"  {second:7.1f}s  {names.get(emp, emp)}")
        print(f"  identified: {len(r.identified)}, unknown faces: {r.unknowns}")
    slow = [r.mode for r in results if r.frames and r.fps < fps]
    if slow:
        print(f"\nToo slow for {fps:g} frames/s in: {', '.join(slow)}. Lower camera.process_fps, "
              "raise tracking.full_frame_every, or use the opencv backend.")
    return results
