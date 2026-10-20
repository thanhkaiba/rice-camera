"""One camera frame -> faces, people and tracker decisions. Shared by the live loop and `benchmark`."""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time

import numpy as np

from .person import ByteTracker, PersonDetector, PersonTrack, assign_faces, head_region
from .recognizer import DetectedFace, crop_body, crop_face
from .tracker import Decision, FaceTracker, Observation, clear_match

log = logging.getLogger(__name__)


def _scene(whole, bbox):
    """(whole frame, face box in whole-frame pixels) for the evidence picture, or None."""
    if whole is None:
        return None
    frame, (ox, oy) = whole
    x1, y1, x2, y2 = bbox
    return frame, (float(x1 + ox), float(y1 + oy), float(x2 + ox), float(y2 + oy))


class FrameProcessor:
    def __init__(self, app, mode: str | None = None, *, realtime: bool = True):
        """`mode` overrides cfg.tracking.mode (the benchmark runs both); otherwise the setting is
        read on every frame, so switching it on the Settings page takes effect right away.
        Offline videos use realtime=False: expiry follows video timestamps regardless of CPU speed.
        """
        self.app = app
        self.cfg = app.cfg
        self.face_tracker = FaceTracker(self.cfg.recognition)
        self._mode = mode
        self._detector: PersonDetector | None = None
        self._people = ByteTracker(new_track=self.cfg.tracking.person_conf)
        self.person_error: str | None = None
        self.people: list[PersonTrack] = []
        self._frames = 0
        self._last_processing_s = 0.0
        self._realtime = realtime

    @property
    def mode(self) -> str:
        return self._mode or self.cfg.tracking.mode

    def _detector_or_none(self) -> PersonDetector | None:
        if self._detector is None and self.person_error is None:
            try:
                self._detector = PersonDetector(self.cfg.path(self.cfg.tracking.person_model),
                                                self.cfg.recognition.use_gpu, self.cfg.recognition.cpu_threads)
                log.info("Person tracking on (%s model)", self._detector.kind)
            except Exception as exc:  # missing or unsupported model: keep working with faces only
                self.person_error = f"Person tracking is off: {exc}"
                log.error(self.person_error)
        return self._detector

    def process(self, frame: np.ndarray, ts: dt.datetime, record: bool = True
                ) -> tuple[list[Decision], list[tuple[DetectedFace, list, int | None]], list[PersonTrack]]:
        """Returns (decisions, faces with their gallery matches and person id, visible people).
        With record=False (camera preview) nothing is added to the trackers' decisions.

        Only camera.area of the frame is analysed when it is set; the boxes returned are in
        whole-frame pixels."""
        area = self.cfg.camera.area_box(frame.shape)
        if area is not None:
            x0, y0, x1, y1 = area
            decisions, matched, people = self._process(frame[y0:y1, x0:x1], ts, record, (frame, (x0, y0)))
            shift = np.array([x0, y0, x0, y0], dtype=np.float32)
            matched = [(dataclasses.replace(f, bbox=tuple(float(v) for v in np.add(f.bbox, shift)),
                                            landmarks=None if f.landmarks is None else f.landmarks + shift[:2]), c, pid)
                       for f, c, pid in matched]
            people = [dataclasses.replace(p, box=p.box + shift) for p in people]
            return decisions, matched, people
        return self._process(frame, ts, record, (frame, (0, 0)))

    def _process(self, frame: np.ndarray, ts: dt.datetime, record: bool, whole=None):
        """`whole`: (the whole camera frame, where `frame` sits in it), kept with each observation so
        the evidence picture of a sighting shows the entire scene."""
        started = time.monotonic()
        regions, people, followed, full_frame = (), [], None, True
        detector = self._detector_or_none() if self.mode == "person" else None
        if detector is not None:
            self._people.new_track = self.cfg.tracking.person_conf
            boxes, scores = detector.detect(frame, min_score=0.1)
            people = self._people.update(boxes, scores, ts)
            followed = self._people.alive()
            regions = [head_region(p.box, frame.shape) for p in people]
            full_frame = not people or self._frames % self.cfg.tracking.full_frame_every == 0
            self._frames += 1
        with self.app.recognizer_lock:
            faces = self.app.recognizer().analyze(frame, regions, self.cfg.tracking.head_crop_size, full_frame,
                                                  self.app.pose.offset)
        owners = assign_faces([f.bbox for f in faces], people) if people else [None] * len(faces)
        matched = [(f, self.app.gallery.match(f.embedding), pid) for f, pid in zip(faces, owners)]
        for f, c, _ in matched:  # clear matches teach how this camera sees faces
            if f.size_ok and f.raw_pitch is not None and clear_match(c, self.cfg.recognition):
                self.app.pose.add(f.raw_yaw, f.raw_pitch)
        decisions = []
        if record:
            observations = [Observation(f.bbox, f.embedding, c, f.quality_ok, f.quality, crop_face(frame, f.bbox), pid,
                                        f.size_ok, crop_body(frame, f.bbox), _scene(whole, f.bbox))
                            for f, c, pid in matched]
            # Capture timestamps are separated by the previous frame's processing time.
            interval = self._last_processing_s + 1 / self.cfg.camera.process_fps if self._realtime else 0.0
            decisions = self.face_tracker.update(observations, ts, followed, frame_interval_s=interval)
        self._last_processing_s = time.monotonic() - started
        self.people = people
        return decisions, matched, people

    def flush(self) -> list[Decision]:
        return self.face_tracker.flush()
