"""Face detection, alignment and embedding.

Two interchangeable backends, compared on your own photos with `python -m attendance evaluate`:

- "insightface": SCRFD-10G detector + ArcFace ResNet50 (WebFace600K), run directly with
  onnxruntime. Most accurate, but the pretrained weights are licensed for non-commercial
  research only; commercial use needs a licence from InsightFace.
- "opencv": YuNet detector (MIT) + SFace recogniser (Apache 2.0), built into OpenCV.
  Less accurate, but free for commercial use and has no extra dependencies.

Both align every face on its 5 landmarks before computing the embedding.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..core.config import RecognitionConfig

# ArcFace reference positions of the eyes, nose tip and mouth corners in a 112x112 crop.
ARCFACE_LANDMARKS = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


@dataclass
class DetectedFace:
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray  # L2-normalised
    det_score: float
    size: float  # shorter side of the box, in pixels
    sharpness: float
    yaw: float  # degrees, signed, relative to how this camera normally sees faces (see posecal)
    pitch: float  # degrees, signed (+ = seen from above), relative likewise; a rough estimate
    quality_ok: bool
    quality: float
    landmarks: np.ndarray | None = None  # 5 points: eyes, nose, mouth corners
    size_ok: bool = True  # big and clearly detected; the rest of the gate (pose, blur) can be outweighed
    raw_yaw: float | None = None  # pose as estimated, before the camera's usual angle is taken off
    raw_pitch: float | None = None


def enhance_low_light(frame: np.ndarray, threshold: float = 90.0) -> np.ndarray:
    """Equalise contrast (CLAHE on the L channel) when the frame is dark."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if gray.mean() >= threshold:
        return frame
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def sharpness(frame: np.ndarray, bbox) -> float:
    """Variance of the Laplacian on the face, resized to a fixed width so it is comparable across sizes."""
    x1, y1, x2, y2 = (int(round(v)) for v in bbox)
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return 0.0
    face = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    face = cv2.resize(face, (112, int(112 * (y2 - y1) / (x2 - x1))))
    return float(cv2.Laplacian(face, cv2.CV_64F).var())


def head_pose(landmarks: np.ndarray) -> tuple[float, float]:
    """Approximate signed (yaw, pitch) in degrees from eyes, nose tip and mouth corners.

    When the head turns, the nose tip moves sideways relative to the eyes by roughly
    0.55 * eye distance * tan(yaw). Pitch uses where the nose sits between eyes and mouth;
    it is positive when the face is seen from above (or tilted down).
    """
    eyes, nose, mouth = landmarks[:2], landmarks[2], landmarks[3:5]
    eye_mid, mouth_mid = eyes.mean(axis=0), mouth.mean(axis=0)
    eye_dist = float(np.linalg.norm(eyes[1] - eyes[0]))
    face_height = float(mouth_mid[1] - eye_mid[1])
    if eye_dist < 1 or face_height < 1:
        return 90.0, 90.0
    yaw = math.degrees(math.atan((nose[0] - eye_mid[0]) / eye_dist / 0.55))
    pitch = (float(nose[1] - eye_mid[1]) / face_height - 0.55) * 150
    return yaw, pitch


def align(frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    matrix, _ = cv2.estimateAffinePartial2D(landmarks.astype(np.float32), ARCFACE_LANDMARKS, method=cv2.LMEDS)
    if matrix is None:  # degenerate landmarks (e.g. all on one point): no similarity transform exists
        raise ValueError("cannot align face: degenerate landmarks")
    return cv2.warpAffine(frame, matrix, (112, 112), borderValue=0)


def crop_face(frame: np.ndarray, bbox, margin: float = 0.4) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    mw, mh = (x2 - x1) * margin, (y2 - y1) * margin
    h, w = frame.shape[:2]
    x1, y1 = int(max(0, x1 - mw)), int(max(0, y1 - mh))
    x2, y2 = int(min(w, x2 + mw)), int(min(h, y2 + mh))
    return frame[y1:y2, x1:x2].copy()


def crop_body(frame: np.ndarray, bbox) -> np.ndarray:
    """The person below a face, so whoever reviews the photo can tell people apart by build and
    clothes: a standing person is about eight face heights tall and four face widths wide."""
    x1, y1, x2, y2 = bbox
    fw, fh, cx = x2 - x1, y2 - y1, (x1 + x2) / 2
    return crop_face(frame, (cx - 2 * fw, y1 - fh, cx + 2 * fw, y1 + 8 * fh), margin=0)


MAX_AUTO_THREADS = 6  # more threads barely speed up one small image, and each one costs CPU


def cpu_threads(setting: int = 0) -> int:
    """Threads for model inference: the setting, or about one per physical core (os.cpu_count()
    counts hyper-threads twice), at most MAX_AUTO_THREADS, leaving the rest for the video decoder
    and the admin page (at least 1)."""
    return setting if setting > 0 else max(1, min(MAX_AUTO_THREADS, (os.cpu_count() or 2) // 2))


def onnx_session(model: Path, use_gpu: bool = False, threads: int = 0):
    """An onnxruntime session with a capped thread pool, logging errors only: SCRFD run on head
    crops (any size other than 640) makes onnxruntime warn about output shapes on every frame,
    which is harmless.

    Spinning is off: by default idle pool threads busy-wait for the next model call, and with a
    few models called several times a second that kept every core at 90 % on an office Mac."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = cpu_threads(threads)
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if use_gpu else ["CPUExecutionProvider"]
    return ort.InferenceSession(str(model), sess_options=options, providers=providers)


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    order = scores.argsort()[::-1]
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        overlap = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][overlap <= threshold]
    return keep


class InsightFaceBackend:
    """SCRFD + ArcFace ONNX models, decoded here so the `insightface` package is not needed."""

    STRIDES = (8, 16, 32)
    ANCHORS = 2

    def __init__(self, cfg: RecognitionConfig, detector: Path, recognizer: Path):
        self.det = onnx_session(detector, cfg.use_gpu, cfg.cpu_threads)
        self.rec = onnx_session(recognizer, cfg.use_gpu, cfg.cpu_threads)
        self.det_size = cfg.det_size
        self._centers: dict[tuple[int, int, int], np.ndarray] = {}

    def detect(self, frame: np.ndarray, threshold: float = 0.5,
               det_size: int | None = None) -> list[tuple[np.ndarray, float, np.ndarray]]:
        size = det_size or self.det_size
        h, w = frame.shape[:2]
        scale = size / max(h, w)
        resized = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
        canvas = np.zeros((size, size, 3), dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (size, size), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(None, {self.det.get_inputs()[0].name: blob})

        boxes, scores, kpss = [], [], []
        n = len(self.STRIDES)
        for i, stride in enumerate(self.STRIDES):
            score = outs[i].reshape(-1)
            dist = outs[i + n].reshape(-1, 4) * stride
            kps = outs[i + 2 * n].reshape(-1, 10) * stride
            centers = self._anchor_centers(stride, size)
            keep = score >= threshold
            c, d, k = centers[keep], dist[keep], kps[keep]
            boxes.append(np.hstack([c - d[:, :2], c + d[:, 2:]]))
            kpss.append(k.reshape(-1, 5, 2) + c[:, None, :])
            scores.append(score[keep])
        boxes, scores, kpss = np.vstack(boxes) / scale, np.concatenate(scores), np.vstack(kpss) / scale
        return [(boxes[i], float(scores[i]), kpss[i]) for i in nms(boxes, scores, 0.4)]

    def _anchor_centers(self, stride: int, det_size: int) -> np.ndarray:
        key = (det_size, det_size, stride)
        if key not in self._centers:
            size = det_size // stride
            grid = np.stack(np.mgrid[:size, :size][::-1], axis=-1).astype(np.float32).reshape(-1, 2) * stride
            self._centers[key] = np.repeat(grid, self.ANCHORS, axis=0)
        return self._centers[key]

    def embed(self, frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        face = align(frame, landmarks)
        blob = cv2.dnn.blobFromImage(face, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        return self.rec.run(None, {self.rec.get_inputs()[0].name: blob})[0][0]


class OpenCVBackend:
    """YuNet + SFace through OpenCV's FaceDetectorYN / FaceRecognizerSF."""

    def __init__(self, cfg: RecognitionConfig, detector: Path, recognizer: Path):
        cv2.setNumThreads(cpu_threads(cfg.cpu_threads))
        self.det = cv2.FaceDetectorYN.create(str(detector), "", (320, 320), 0.5, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(recognizer), "")
        self.max_side = cfg.det_size

    def detect(self, frame: np.ndarray, threshold: float = 0.5,
               det_size: int | None = None) -> list[tuple[np.ndarray, float, np.ndarray]]:
        h, w = frame.shape[:2]
        scale = (det_size or self.max_side) / max(h, w)
        scale = scale if det_size else min(1.0, scale)  # head crops may be enlarged, whole frames are not
        small = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale)))) if scale != 1 else frame
        self.det.setInputSize((small.shape[1], small.shape[0]))
        self.det.setScoreThreshold(threshold)
        _, faces = self.det.detect(small)
        out = []
        for f in faces if faces is not None else []:
            x, y, bw, bh = f[:4] / scale
            out.append((np.array([x, y, x + bw, y + bh]), float(f[14]), f[4:14].reshape(5, 2) / scale))
        return out

    def embed(self, frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        # Same 5-point alignment as the other backend; SFace expects a 112x112 aligned face.
        return self.rec.feature(align(frame, landmarks))[0]


BACKENDS = {"insightface": InsightFaceBackend, "opencv": OpenCVBackend}


class FaceRecognizer:
    def __init__(self, cfg: RecognitionConfig, base_dir: Path = Path(".")):
        if cfg.backend not in BACKENDS:
            raise ValueError(f"recognition.backend must be one of {sorted(BACKENDS)}")
        detector, recognizer = (base_dir / p for p in cfg.model_files())
        for path in (detector, recognizer):
            if not path.exists():
                raise FileNotFoundError(f"{path} not found; run: python -m attendance download-models")
        self.cfg = cfg
        self.model_id = cfg.model_id()
        self.backend = BACKENDS[cfg.backend](cfg, detector, recognizer)

    def detect(self, frame: np.ndarray, regions=(), region_size: int = 320,
               full_frame: bool = True) -> list[tuple[np.ndarray, float, np.ndarray]]:
        """Faces in the whole frame (unless full_frame is False), plus faces found in `regions`
        (x1, y1, x2, y2) searched at full resolution: small faces at the back of a group are
        easier to find in a crop of the head."""
        # The backends stop at 0.5 by default; a lower min_det_score has to reach the detector.
        threshold = min(0.5, max(0.2, self.cfg.min_det_score))
        found = list(self.backend.detect(frame, threshold=threshold)) if full_frame else []
        for x1, y1, x2, y2 in regions:
            if x2 - x1 < 16 or y2 - y1 < 16:
                continue
            for box, score, kps in self.backend.detect(frame[y1:y2, x1:x2], threshold=threshold, det_size=region_size):
                found.append((box + [x1, y1, x1, y1], score, kps + [x1, y1]))
        if len(found) <= 1 or not regions or not full_frame:
            return found
        boxes = np.array([f[0] for f in found], dtype=np.float32)
        scores = np.array([f[1] for f in found], dtype=np.float32)
        return [found[i] for i in nms(boxes, scores, 0.4)]

    def analyze(self, frame: np.ndarray, regions=(), region_size: int = 320,
                full_frame: bool = True, pose_offset: tuple[float, float] = (0.0, 0.0)) -> list[DetectedFace]:
        """`pose_offset`: how this camera normally sees faces (yaw, pitch); the pose check is made
        relative to it. Photos (enrolment, evaluation) use a straight-on view."""
        c = self.cfg
        out = []
        for box, det, landmarks in self.detect(frame, regions, region_size, full_frame):
            bbox = tuple(float(v) for v in box)
            size = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            raw_yaw, raw_pitch = head_pose(landmarks)
            yaw, pitch = raw_yaw - pose_offset[0], raw_pitch - pose_offset[1]
            sharp = sharpness(frame, bbox)
            size_ok = size >= c.min_face_px and det >= c.min_det_score
            ok = size_ok and sharp >= c.min_sharpness and abs(yaw) <= c.max_yaw and abs(pitch) <= c.max_pitch
            quality = (det * min(1.0, size / 112) * min(1.0, sharp / 150)
                       * math.cos(math.radians(min(abs(yaw), 89))) * math.cos(math.radians(min(abs(pitch), 89))))
            try:
                emb = self.backend.embed(frame, landmarks).astype(np.float32)
            except ValueError:  # unusable landmarks: skip this face rather than stop the camera loop
                continue

            emb /= max(float(np.linalg.norm(emb)), 1e-12)
            out.append(DetectedFace(bbox, emb, det, size, sharp, yaw, pitch, ok, quality, landmarks, size_ok,
                                    raw_yaw, raw_pitch))
        return out

    def analyze_photo(self, image: np.ndarray) -> list[DetectedFace]:
        """Retry small photos at crop scales when enlarging them hid the face."""
        faces = self.analyze(image)
        if faces or max(image.shape[:2]) >= self.cfg.det_size:
            return faces
        h, w = image.shape[:2]
        for size in (320, 160):
            if size >= self.cfg.det_size:
                continue
            faces = self.analyze(image, [(0, 0, w, h)], region_size=size, full_frame=False)
            if faces:
                return faces
        return []

    def largest_face(self, image: np.ndarray, mirror: bool = False) -> DetectedFace | None:
        """The biggest face in a photo. With `mirror` (reference photos), its embedding is the
        average of the photo and its mirror image, which describes the face rather than the
        side it happened to be lit or turned from."""
        faces = self.analyze_photo(image)
        if not faces:
            return None
        face = max(faces, key=lambda f: f.size)
        if mirror and face.landmarks is not None:
            flipped = np.ascontiguousarray(image[:, ::-1])
            kps = face.landmarks.copy()
            kps[:, 0] = image.shape[1] - 1 - kps[:, 0]
            kps = kps[[1, 0, 2, 4, 3]]  # the left eye of the mirror image is the right eye, etc.
            try:
                other = self.backend.embed(flipped, kps).astype(np.float32)
            except ValueError:
                return face
            other /= max(float(np.linalg.norm(other)), 1e-12)
            both = face.embedding + other
            face.embedding = both / max(float(np.linalg.norm(both)), 1e-12)
        return face
