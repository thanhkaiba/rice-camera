"""One picture of everyone who came late, for the daily late message.

The camera keeps a photo of the whole person for every sighting (store.body_photo). This module
cuts the late people out of those photos and puts them side by side on a cheerful background
(a drawn morning scene, or a picture the admin uploaded), each with a name tag with the arrival
time, so the message in Google Chat shows at a glance who it is about.

Cutting out: with the U²-Net model (models/u2netp.onnx, downloaded on the first start) when it is
on disk, otherwise with OpenCV's GrabCut, which is rougher. A white sticker edge around each
person hides the rough edges of either.

The picture is served without the admin password, under an address that holds an unguessable
token, because Google Chat fetches the image itself.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import math
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

FOLDER = "late"  # under the data folder: late/<day>.jpg, late/background.jpg
BACKGROUND_FILE = "background.jpg"
CUTOUT_MODEL = "u2netp.onnx"  # in models/
HEIGHT = 720
MIN_WIDTH = 1280
PERSON_H = 440  # a person is at most this tall
SLOT_MAX = 250  # and gets at most this much width
MARGIN = 60
GROUND = 92  # the ground line is this far above the bottom edge
STICKER = 4  # white edge around each cutout, px
FONT = 0  # cv2.FONT_HERSHEY_SIMPLEX


@dataclass
class Person:
    name: str
    time: str  # HH:MM
    minutes: int  # late by
    photo: Path | None  # the whole person (or the face when that is all there is)


def photo_path(day: dt.date) -> str:
    return f"{FOLDER}/{day.isoformat()}.jpg"


def background_path(data_dir: Path) -> Path:
    return data_dir / FOLDER / BACKGROUND_FILE


def token(secret: str, day: dt.date) -> str:
    """The unguessable part of the picture's address: Google Chat fetches the picture without the
    admin password, while nobody can browse the pictures of other days."""
    return hmac.new(secret.encode(), day.isoformat().encode(), hashlib.sha256).hexdigest()[:32]


def ascii_label(text: str) -> str:
    """OpenCV only draws ASCII: drop the accents (Nguyễn Văn Đức -> Nguyen Van Duc)."""
    text = text.replace("Đ", "D").replace("đ", "d")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().strip() or "?"


def _width(text: str, size: float, thick: int = 1) -> int:
    import cv2

    return cv2.getTextSize(text, FONT, size, thick)[0][0]


def _fit(text: str, width: int, size: float, thick: int = 1) -> str:
    """Shorten `text` with ".." until it is at most `width` pixels wide."""
    if _width(text, size, thick) <= width:
        return text
    while len(text) > 1 and _width(text + "..", size, thick) > width:
        text = text[:-1].rstrip()
    return text + ".."


# cutting people out -------------------------------------------------------------------

_sessions: dict[Path, object] = {}
_sessions_lock = threading.Lock()


def _session(model: Path):
    with _sessions_lock:
        if model not in _sessions:
            from .recognizer import onnx_session

            _sessions[model] = onnx_session(model, threads=1)
        return _sessions[model]


def _largest_blob(binary: np.ndarray) -> np.ndarray:
    """Keep the biggest connected piece of a 0/1 mask: the person, not a bag on the floor."""
    import cv2

    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary.astype(np.uint8), connectivity=8)
    if n <= 2:
        return binary
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == biggest).astype(binary.dtype)


def model_mask(image: np.ndarray, model: Path) -> np.ndarray:
    """Where the person is, 0..1, from U²-Net (salient object), at the image's size."""
    import cv2

    session = _session(model)
    rgb = cv2.cvtColor(cv2.resize(image, (320, 320), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    x = rgb.astype(np.float32) / max(float(rgb.max()), 1e-6)
    x = (x - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)
    x = x.transpose(2, 0, 1)[None].astype(np.float32)
    pred = session.run(None, {session.get_inputs()[0].name: x})[0][0, 0]
    pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-6)
    return cv2.resize(pred, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR)


def grabcut_mask(image: np.ndarray, iterations: int = 5) -> np.ndarray:
    """Where the person is, 0..1, from colour statistics alone: the camera's body photo has the
    person in the middle, so the sides are taken as background."""
    import cv2

    h, w = image.shape[:2]
    rect = (int(w * 0.15), int(h * 0.02), int(w * 0.7), int(h * 0.96))
    mask = np.zeros((h, w), np.uint8)
    bg, fg = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(image, mask, rect, bg, fg, iterations, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        mask[rect[1]:rect[1] + rect[3], rect[0]:rect[0] + rect[2]] = cv2.GC_PR_FGD
    return ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.float32)


def cutout(image: np.ndarray, model: Path | None) -> tuple[np.ndarray, np.ndarray]:
    """(image, alpha 0..1): the person with a soft edge, cropped to what remains."""
    import cv2

    try:
        soft = model_mask(image, model) if model is not None and model.exists() else grabcut_mask(image)
    except Exception as exc:  # a broken model file: fall back rather than lose the picture
        log.warning("Cutout model failed (%s); using GrabCut", exc)
        soft = grabcut_mask(image)
    binary = _largest_blob((soft > 0.5).astype(np.uint8))
    if binary.sum() < 0.02 * binary.size:  # nothing found: keep the whole photo as a card
        return image, np.ones(image.shape[:2], np.float32)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    alpha = cv2.GaussianBlur(binary.astype(np.float32), (5, 5), 0)
    ys, xs = np.where(alpha > 0.02)
    y1, y2, x1, x2 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return image[y1:y2, x1:x2], alpha[y1:y2, x1:x2]


# drawing ----------------------------------------------------------------------------------

def _rounded(canvas, x1, y1, x2, y2, color, r=12, alpha=1.0) -> None:
    import cv2

    layer = canvas.copy() if alpha < 1 else canvas
    cv2.rectangle(layer, (x1 + r, y1), (x2 - r, y2), color, -1)
    cv2.rectangle(layer, (x1, y1 + r), (x2, y2 - r), color, -1)
    for cx, cy in ((x1 + r, y1 + r), (x2 - r, y1 + r), (x1 + r, y2 - r), (x2 - r, y2 - r)):
        cv2.circle(layer, (cx, cy), r, color, -1, cv2.LINE_AA)
    if alpha < 1:
        cv2.addWeighted(layer, alpha, canvas, 1 - alpha, 0, canvas)


def draw_background(width: int, height: int, seed: int = 0) -> np.ndarray:
    """A cheerful morning: sky, sun, clouds, hills and a meadow. `seed` moves the clouds."""
    import cv2

    rng = np.random.default_rng(seed)
    canvas = np.zeros((height, width, 3), np.uint8)
    horizon = int(height * 0.6)
    top, bottom = np.array((252, 190, 120), np.float32), np.array((225, 240, 255), np.float32)  # BGR: blue -> cream
    for y in range(horizon + 40):
        canvas[y] = top + (bottom - top) * (y / (horizon + 40))
    # sun, top right, with rays
    sx, sy, sr = width - 150, 110, 52
    for i in range(12):
        a = i * math.pi / 6
        p1 = (int(sx + math.cos(a) * (sr + 14)), int(sy + math.sin(a) * (sr + 14)))
        p2 = (int(sx + math.cos(a) * (sr + 42)), int(sy + math.sin(a) * (sr + 42)))
        cv2.line(canvas, p1, p2, (120, 220, 255), 6, cv2.LINE_AA)
    cv2.circle(canvas, (sx, sy), sr, (90, 210, 255), -1, cv2.LINE_AA)
    cv2.circle(canvas, (sx, sy), sr - 12, (150, 235, 255), -1, cv2.LINE_AA)
    # clouds
    for _ in range(4):
        cx, cy = int(rng.integers(80, width - 300)), int(rng.integers(50, horizon - 160))
        for dx, dy, r in ((0, 0, 34), (34, -10, 42), (76, 0, 36), (36, 18, 30), (-6, 14, 26)):
            cv2.circle(canvas, (cx + dx, cy + dy), r, (255, 255, 255), -1, cv2.LINE_AA)
    # hills and the meadow
    cv2.ellipse(canvas, (int(width * 0.25), horizon + 40), (int(width * 0.5), 110), 0, 180, 360, (110, 190, 120), -1,
                cv2.LINE_AA)
    cv2.ellipse(canvas, (int(width * 0.8), horizon + 50), (int(width * 0.45), 130), 0, 180, 360, (95, 175, 105), -1,
                cv2.LINE_AA)
    canvas[horizon + 40:] = (80, 165, 95)
    cv2.ellipse(canvas, (width // 2, height + 60), (int(width * 0.7), 150), 0, 180, 360, (95, 185, 110), -1,
                cv2.LINE_AA)
    # a few flowers
    for _ in range(18):
        fx, fy = int(rng.integers(20, width - 20)), int(rng.integers(horizon + 60, height - 10))
        colour = [(120, 120, 255), (255, 200, 120), (140, 220, 255), (255, 160, 220)][int(rng.integers(0, 4))]
        cv2.circle(canvas, (fx, fy), 4, colour, -1, cv2.LINE_AA)
        cv2.circle(canvas, (fx, fy), 2, (120, 240, 255), -1, cv2.LINE_AA)
    return canvas


def load_background(path: Path | None, width: int, height: int, seed: int = 0) -> np.ndarray:
    """The admin's picture scaled and cropped to fill the canvas, or the drawn scene."""
    import cv2

    image = cv2.imread(str(path)) if path is not None and path.exists() else None
    if image is None or not image.size:
        return draw_background(width, height, seed)
    h, w = image.shape[:2]
    scale = max(width / w, height / h)
    image = cv2.resize(image, (max(width, round(w * scale)), max(height, round(h * scale))), interpolation=cv2.INTER_AREA)
    y, x = (image.shape[0] - height) // 2, (image.shape[1] - width) // 2
    return np.ascontiguousarray(image[y:y + height, x:x + width])


def _paste(canvas: np.ndarray, image: np.ndarray, alpha: np.ndarray, x: int, y: int) -> None:
    """Alpha-blend `image` onto `canvas` with its top-left corner at (x, y), clipped to the canvas."""
    h, w = image.shape[:2]
    H, W = canvas.shape[:2]
    x1, y1, x2, y2 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x2 <= x1 or y2 <= y1:
        return
    img = image[y1 - y:y2 - y, x1 - x:x2 - x].astype(np.float32)
    a = alpha[y1 - y:y2 - y, x1 - x:x2 - x][..., None]
    region = canvas[y1:y2, x1:x2].astype(np.float32)
    canvas[y1:y2, x1:x2] = (img * a + region * (1 - a)).round().astype(np.uint8)


def _sticker(image: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A white edge around the cutout (hides rough edges, looks like a sticker)."""
    import cv2

    pad = STICKER + 2
    image = cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    alpha = cv2.copyMakeBorder(alpha, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    edge = cv2.dilate((alpha > 0.5).astype(np.uint8), np.ones((2 * STICKER + 1, 2 * STICKER + 1), np.uint8))
    edge = cv2.GaussianBlur(edge.astype(np.float32), (3, 3), 0)
    image[(edge > 0.5) & (alpha < 0.5)] = (255, 255, 255)
    return image, np.maximum(alpha, edge)


def _placeholder(height: int) -> tuple[np.ndarray, np.ndarray]:
    """A grey figure with a question mark, for someone whose arrival was entered without a photo."""
    import cv2

    width = int(height * 0.5)
    image = np.full((height, width, 3), 255, np.uint8)
    alpha = np.zeros((height, width), np.float32)
    grey = (170, 170, 170)
    cv2.circle(image, (width // 2, height // 6), height // 7, grey, -1, cv2.LINE_AA)
    cv2.circle(alpha, (width // 2, height // 6), height // 7, 1.0, -1, cv2.LINE_AA)
    body = (width // 2, int(height * 0.62)), (int(width * 0.42), int(height * 0.36))
    cv2.ellipse(image, body[0], body[1], 0, 0, 360, grey, -1, cv2.LINE_AA)
    cv2.ellipse(alpha, body[0], body[1], 0, 0, 360, 1.0, -1, cv2.LINE_AA)
    size = height / 220
    (tw, th), _ = cv2.getTextSize("?", FONT, size, 2)
    cv2.putText(image, "?", ((width - tw) // 2, int(height * 0.62) + th // 2), FONT, size, (255, 255, 255), 2, cv2.LINE_AA)
    return image, alpha


def _figure(person: Person, model: Path | None, height: int) -> tuple[np.ndarray, np.ndarray]:
    """The person cut out and scaled to at most `height` tall, with the sticker edge."""
    import cv2

    photo = cv2.imread(str(person.photo)) if person.photo and person.photo.exists() else None
    if photo is None or not photo.size:
        image, alpha = _placeholder(height)
    else:
        image, alpha = cutout(photo, model)
        if image.shape[0] > image.shape[1] * 1.5:  # a standing person (the body photo is 9:4): full height
            target = height
        else:  # a face or a wide crop: smaller, so it does not look like a giant head
            target = int(height * 0.38)
        scale = target / image.shape[0]
        size = (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        alpha = cv2.resize(alpha, size, interpolation=cv2.INTER_LINEAR)
    return _sticker(image, alpha)


def _tag(canvas: np.ndarray, cx: int, y: int, name: str, line2: str, width: int) -> None:
    """A rounded name tag centred on cx with its top at y."""
    import cv2

    name = _fit(ascii_label(name), width - 24, 0.62, 2)
    line2 = _fit(line2, width - 24, 0.52)
    w = min(width, max(_width(name, 0.62, 2), _width(line2, 0.52)) + 28)
    x1, x2 = cx - w // 2, cx + w // 2
    _rounded(canvas, x1 + 3, y + 3, x2 + 3, y + 58, (40, 40, 40), 10, alpha=0.35)  # shadow
    _rounded(canvas, x1, y, x2, y + 55, (255, 255, 255), 10)
    cv2.putText(canvas, name, (cx - _width(name, 0.62, 2) // 2, y + 24), FONT, 0.62, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.putText(canvas, line2, (cx - _width(line2, 0.52) // 2, y + 46), FONT, 0.52, (0, 110, 230), 1, cv2.LINE_AA)


def _banner(canvas: np.ndarray, title: str) -> None:
    """The title in a dark rounded box top left, with a small alarm clock."""
    import cv2

    title = _fit(ascii_label(title), canvas.shape[1] - 160, 0.9, 2)
    tw = _width(title, 0.9, 2)
    _rounded(canvas, 24, 24, 24 + tw + 86, 90, (35, 35, 35), 14, alpha=0.75)
    cx, cy = 58, 58
    cv2.circle(canvas, (cx - 12, cy - 13), 7, (90, 90, 230), -1, cv2.LINE_AA)  # bells
    cv2.circle(canvas, (cx + 12, cy - 13), 7, (90, 90, 230), -1, cv2.LINE_AA)
    cv2.circle(canvas, (cx, cy), 17, (90, 90, 230), -1, cv2.LINE_AA)
    cv2.circle(canvas, (cx, cy), 12, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.line(canvas, (cx, cy), (cx, cy - 8), (40, 40, 40), 2, cv2.LINE_AA)
    cv2.line(canvas, (cx, cy), (cx + 6, cy + 3), (40, 40, 40), 2, cv2.LINE_AA)
    cv2.putText(canvas, title, (92, 68), FONT, 0.9, (255, 255, 255), 2, cv2.LINE_AA)


def compose(people: list[Person], title: str, model: Path | None = None, background: Path | None = None,
            seed: int = 0) -> bytes:
    """Everyone side by side on the background, as JPEG."""
    import cv2

    from ..vision.faces import encode_jpeg

    if not people:
        raise ValueError("nobody to show")
    n = len(people)
    slot = min(SLOT_MAX, (MIN_WIDTH - 2 * MARGIN) // n)
    width = max(MIN_WIDTH, 2 * MARGIN + n * slot)
    canvas = load_background(background, width, HEIGHT, seed)
    ground = HEIGHT - GROUND
    person_h = min(PERSON_H, int(slot * 1.9))
    for i, person in enumerate(people):
        cx = MARGIN + i * slot + slot // 2
        image, alpha = _figure(person, model, person_h)
        h, w = image.shape[:2]
        if w > slot + 20:  # a wide photo: shrink it to the slot
            scale = (slot + 20) / w
            image = cv2.resize(image, (slot + 20, max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
            alpha = cv2.resize(alpha, (slot + 20, max(1, round(h * scale))), interpolation=cv2.INTER_LINEAR)
            h, w = image.shape[:2]
        shadow = canvas.copy()
        cv2.ellipse(shadow, (cx, ground - 2), (w // 2, max(6, w // 9)), 0, 0, 360, (40, 60, 40), -1, cv2.LINE_AA)
        cv2.addWeighted(shadow, 0.35, canvas, 0.65, 0, canvas)
        _paste(canvas, image, alpha, cx - w // 2, ground - h)
        _tag(canvas, cx, ground + 14, person.name, f"{person.time}  +{person.minutes} min", slot - 6)
    _banner(canvas, title)
    return encode_jpeg(canvas)


# files --------------------------------------------------------------------------------------

def fingerprint(people: list[Person], model: Path | None, background: Path | None) -> str:
    """Changes when the picture would: someone added or removed, a time corrected, a photo
    replaced, the background changed, the cutout model arrived."""
    def stat(p: Path | None):
        s = p.stat() if p and p.exists() else None
        return (str(p), s.st_size if s else 0, s.st_mtime_ns if s else 0)

    parts = [(p.name, p.time, p.minutes, stat(p.photo)) for p in people] + [stat(model), stat(background)]
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def write(data_dir: Path, day: dt.date, people: list[Person], title: str, model: Path | None = None) -> str:
    """Make (or refresh, when something changed) the day's picture; returns its path under data_dir."""
    rel = photo_path(day)
    path = data_dir / rel
    key = path.with_suffix(".key")
    background = background_path(data_dir)
    fp = fingerprint(people, model, background)
    if path.exists() and key.exists() and key.read_text() == fp:
        return rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(compose(people, title, model, background, seed=day.toordinal()))
    tmp.replace(path)  # readers (the web page, Google Chat) never see a half-written file
    key.write_text(fp)
    return rel


def remove(data_dir: Path, day: dt.date) -> bool:
    path = data_dir / photo_path(day)
    found = path.exists()
    for p in (path, path.with_suffix(".key"), path.with_suffix(".tmp")):
        p.unlink(missing_ok=True)
    return found


def expire(data_dir: Path, cutoff: dt.date) -> int:
    """Delete the pictures of days before `cutoff`."""
    folder = data_dir / FOLDER
    removed = 0
    if folder.exists():
        for p in folder.glob("*.jpg"):
            try:
                day = dt.date.fromisoformat(p.stem)
            except ValueError:
                continue  # background.jpg
            if day < cutoff:
                removed += remove(data_dir, day)
    return removed
