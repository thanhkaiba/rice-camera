"""Person detection and tracking, so a face keeps its identity while the person walks in a group.

When several people come in together, faces turn away and cover each other. Following each
*person* (the whole body) lets the face votes from every frame where their face was visible
add up on one track, and the arrival time is when the person appeared, not when their face
was first sharp.

- PersonDetector: any YOLOX ONNX model (Apache-2.0, `download-models --person`) or a YOLOv8
  ONNX export (Ultralytics, AGPL-3.0: check the licence before using it in a company).
  The model type is recognised from its output shape.
- ByteTracker: ByteTrack (Zhang et al. 2022, MIT) in plain numpy: a Kalman filter per person
  and two association rounds, first with confident detections, then with weak ones, so a
  person who is half hidden for a moment keeps their track.
"""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .recognizer import nms, onnx_session

PERSON_CLASS = 0  # COCO "person"


def box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU matrix between boxes a (N,4) and b (M,4) in x1,y1,x2,y2."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


def greedy_match(scores: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """Pairs (row, col) taken best-first while the score is at least `threshold`."""
    pairs, used_r, used_c = [], set(), set()
    if scores.size == 0:
        return pairs
    for flat in np.argsort(-scores, axis=None):
        r, c = divmod(int(flat), scores.shape[1])
        if scores[r, c] < threshold:
            break
        if r not in used_r and c not in used_c:
            pairs.append((r, c))
            used_r.add(r)
            used_c.add(c)
    return pairs


# detection ----------------------------------------------------------------------------

class PersonDetector:
    def __init__(self, model: Path, use_gpu: bool = False, threads: int = 0):
        self.session = onnx_session(model, use_gpu, threads)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.height, self.width = (int(v) if isinstance(v, int) else 640 for v in inp.shape[2:4])
        shape = self.session.get_outputs()[0].shape
        # YOLOv8: (1, 84, N) channels first, no objectness. YOLOX: (1, N, 85) with objectness.
        if len(shape) == 3 and shape[1] == 84:
            self.kind = "yolov8"
        elif len(shape) == 3 and shape[2] == 85:
            self.kind = "yolox"
        else:
            raise ValueError(f"unsupported person model output {shape}; use a YOLOX or YOLOv8 ONNX model")
        self._grid = self._yolox_grid() if self.kind == "yolox" else None

    def _yolox_grid(self) -> tuple[np.ndarray, np.ndarray]:
        grids, strides = [], []
        for stride in (8, 16, 32):
            h, w = self.height // stride, self.width // stride
            xv, yv = np.meshgrid(np.arange(w), np.arange(h))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((h * w, 1), stride))
        return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)

    def detect(self, frame: np.ndarray, min_score: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
        """Person boxes (N,4) in frame pixels and their scores (N,)."""
        h, w = frame.shape[:2]
        scale = min(self.width / w, self.height / h)
        resized = cv2.resize(frame, (int(w * scale), int(h * scale)))
        canvas = np.full((self.height, self.width, 3), 114, dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        if self.kind == "yolox":
            blob = canvas.transpose(2, 0, 1)[None].astype(np.float32)  # BGR, 0-255
        else:
            blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0  # RGB, 0-1
        out = self.session.run(None, {self.input_name: np.ascontiguousarray(blob)})[0][0]

        if self.kind == "yolox":
            grid, stride = self._grid
            xy = (out[:, :2] + grid) * stride
            wh = np.exp(np.clip(out[:, 2:4], -10, 10)) * stride
            scores = out[:, 4] * out[:, 5 + PERSON_CLASS]
        else:
            out = out.T
            xy, wh = out[:, :2], out[:, 2:4]
            scores = out[:, 4 + PERSON_CLASS]
        keep = scores >= min_score
        xy, wh, scores = xy[keep], wh[keep], scores[keep]
        boxes = np.hstack([xy - wh / 2, xy + wh / 2]) / scale
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h)
        idx = nms(boxes, scores, 0.45) if len(boxes) else []
        return boxes[idx].astype(np.float32), scores[idx].astype(np.float32)


# tracking ------------------------------------------------------------------------------

class KalmanBox:
    """Constant-velocity Kalman filter on (centre x, centre y, aspect ratio, height), as in SORT/ByteTrack."""

    _motion = np.eye(8)
    _motion[:4, 4:] = np.eye(4)
    _update = np.eye(4, 8)
    _pos_w, _vel_w = 1 / 20, 1 / 160

    def __init__(self, box: np.ndarray):
        z = self.to_xyah(box)
        self.mean = np.r_[z, np.zeros(4)]
        h = z[3]
        std = [2 * self._pos_w * h, 2 * self._pos_w * h, 1e-2, 2 * self._pos_w * h,
               10 * self._vel_w * h, 10 * self._vel_w * h, 1e-5, 10 * self._vel_w * h]
        self.cov = np.diag(np.square(std))

    @staticmethod
    def to_xyah(box) -> np.ndarray:
        w, h = box[2] - box[0], max(box[3] - box[1], 1e-6)
        return np.array([box[0] + w / 2, box[1] + h / 2, w / h, h], dtype=np.float64)

    def box(self) -> np.ndarray:
        cx, cy, a, h = self.mean[:4]
        w = a * h
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], dtype=np.float32)

    def predict(self) -> None:
        h = self.mean[3]
        std = [self._pos_w * h, self._pos_w * h, 1e-2, self._pos_w * h,
               self._vel_w * h, self._vel_w * h, 1e-5, self._vel_w * h]
        self.mean = self._motion @ self.mean
        self.cov = self._motion @ self.cov @ self._motion.T + np.diag(np.square(std))

    def correct(self, box: np.ndarray) -> None:
        h = self.mean[3]
        std = [self._pos_w * h, self._pos_w * h, 1e-1, self._pos_w * h]
        proj_cov = self._update @ self.cov @ self._update.T + np.diag(np.square(std))
        gain = self.cov @ self._update.T @ np.linalg.inv(proj_cov)
        self.mean = self.mean + gain @ (self.to_xyah(box) - self._update @ self.mean)
        self.cov = self.cov - gain @ self._update @ self.cov


@dataclass
class PersonTrack:
    id: int
    first_ts: dt.datetime
    last_ts: dt.datetime
    score: float
    kf: KalmanBox
    hits: int = 1
    lost: bool = False
    confirmed: bool = False
    box: np.ndarray = field(default=None)

    def __post_init__(self):
        if self.box is None:
            self.box = self.kf.box()


class ByteTracker:
    def __init__(self, high: float = 0.5, low: float = 0.1, new_track: float = 0.6, match_iou: float = 0.2,
                 lost_s: float = 2.0):
        self.high, self.low, self.new_track = high, low, new_track
        self.match_iou, self.lost_s = match_iou, lost_s
        self.tracks: list[PersonTrack] = []
        self._ids = itertools.count(1)

    def update(self, boxes: np.ndarray, scores: np.ndarray, ts: dt.datetime) -> list[PersonTrack]:
        """Feed this frame's detections; returns the people visible now (confirmed tracks)."""
        for t in self.tracks:
            t.kf.predict()
            t.box = t.kf.box()
        strong = scores >= self.high
        weak = (scores >= self.low) & ~strong
        hi_boxes, hi_scores = boxes[strong], scores[strong]
        lo_boxes, lo_scores = boxes[weak], scores[weak]

        # 1. Confident detections against every track, including recently lost ones.
        candidates = list(self.tracks)
        pairs = greedy_match(box_iou(np.array([t.box for t in candidates]).reshape(-1, 4), hi_boxes), self.match_iou)
        matched_t, matched_d = {r for r, _ in pairs}, {c for _, c in pairs}
        for r, c in pairs:
            self._hit(candidates[r], hi_boxes[c], hi_scores[c], ts)

        # 2. Weak detections (often a half-hidden person) only keep visible tracks alive.
        left = [t for i, t in enumerate(candidates) if i not in matched_t and not t.lost]
        pairs = greedy_match(box_iou(np.array([t.box for t in left]).reshape(-1, 4), lo_boxes), 0.5)
        for r, c in pairs:
            self._hit(left[r], lo_boxes[c], lo_scores[c], ts)
        rescued = {id(left[r]) for r, _ in pairs}

        for i, t in enumerate(candidates):
            if i not in matched_t and id(t) not in rescued:
                t.lost = True

        # 3. New people from unmatched confident detections.
        for c in range(len(hi_boxes)):
            if c not in matched_d and hi_scores[c] >= self.new_track:
                track = PersonTrack(next(self._ids), ts, ts, float(hi_scores[c]), KalmanBox(hi_boxes[c]))
                track.box = hi_boxes[c].copy()
                track.confirmed = hi_scores[c] >= 0.75
                self.tracks.append(track)

        self.tracks = [t for t in self.tracks if not t.lost or (ts - t.last_ts).total_seconds() <= self.lost_s]
        return [t for t in self.tracks if not t.lost and t.confirmed]

    def _hit(self, t: PersonTrack, box: np.ndarray, score: float, ts: dt.datetime) -> None:
        t.kf.correct(box)
        t.box, t.score, t.last_ts, t.lost = box.copy(), float(score), ts, False
        t.hits += 1
        t.confirmed = t.confirmed or t.hits >= 2

    def alive(self) -> dict[int, dt.datetime]:
        """Confirmed people still being followed, including those hidden for a moment: id -> first seen."""
        return {t.id: t.first_ts for t in self.tracks if t.confirmed}


# faces and people -------------------------------------------------------------------------

def head_region(box, frame_shape) -> tuple[int, int, int, int]:
    """Where to look for the face of a person: the top of their box, a little wider.

    For a whole body the head is in the top 45%. Near a desk camera often only the head and
    shoulders are in view, and the box is then about as wide as it is tall; the head height
    is up to about the box width, so the region always reaches a bit further than that."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    fh, fw = frame_shape[:2]
    depth = min(h, max(0.45 * h, 1.2 * w))
    return (int(max(0, x1 - 0.15 * w)), int(max(0, y1 - 0.05 * h)),
            int(min(fw, x2 + 0.15 * w)), int(min(fh, y1 + depth)))


def assign_faces(face_boxes: list, people: list[PersonTrack]) -> list[int | None]:
    """The person each face belongs to (or None): the tightest person box whose upper part holds the
    face centre. Each person gets at most one face, the larger one when two compete."""
    owner: list[int | None] = [None] * len(face_boxes)
    taken: dict[int, int] = {}
    order = sorted(range(len(face_boxes)), key=lambda i: -(face_boxes[i][2] - face_boxes[i][0]))
    for i in order:
        fx = (face_boxes[i][0] + face_boxes[i][2]) / 2
        fy = (face_boxes[i][1] + face_boxes[i][3]) / 2
        best, best_area = None, None
        for p in people:
            x1, y1, x2, y2 = p.box
            depth = min(y2 - y1, max(0.6 * (y2 - y1), 1.3 * (x2 - x1)))  # as in head_region
            if p.id in taken or not (x1 <= fx <= x2 and y1 - 0.1 * (y2 - y1) <= fy <= y1 + depth):
                continue
            area = (x2 - x1) * (y2 - y1)
            if best_area is None or area < best_area:
                best, best_area = p.id, area
        if best is not None:
            owner[i] = best
            taken[best] = i
    return owner
