"""One picture of everyone who came late, for the daily late message.

The camera keeps a photo of the whole person for every sighting (store.body_photo). This module puts
those photos, as cards with a name tag and the arrival time, in rows on a cheerful background (a
drawn scene that changes from day to day, or a picture the admin uploaded), so the message in Google
Chat shows at a glance who it is about. The photos are shown whole, not cut out: cutting people out
lost heads (dark hair on a dark wall, masks, caps) and was slow enough to hold up the Today page.

The picture is served without the admin password, under an address that holds an unguessable
token, because Google Chat fetches the image itself.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

FOLDER = "late"  # under the data folder: late/<day>.jpg, late/background.jpg
BACKGROUND_FILE = "background.jpg"
BACKGROUNDS = "backgrounds"  # late/backgrounds/*.jpg|png: the admin's own pictures, one a day, in turn
BUNDLED = Path(__file__).resolve().parents[1] / "web" / "static" / "late"  # old public-domain pictures
OCCASIONS = "occasions"  # BUNDLED/occasions/<key>*.jpg: a holiday's own pictures (see occasions.py)
HEIGHT = 720  # the picture is at least this tall, taller when there are several rows
MIN_WIDTH = 1280
CARD_W, CARD_H = 170, 340  # a person's photo card (the camera's body photo is about 4:9)
BORDER = 6  # white edge of a card, px
PER_ROW = 6  # people in a row at most; more go on further rows so name tags keep their room
GAP = 34  # between cards in a row
TAG_GAP, TAG_H = 12, 56  # name tag under a card
ROW_GAP = 26
TOP, BOTTOM = 120, 40  # room for the title above, margin below
MARGIN = 60
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


ROTATION = "own"  # late/backgrounds/own-<n>.jpg: the admin's pictures shown in turn (see day_background)


def own_pictures(data_dir: Path) -> list[tuple[str, str]]:
    """The admin's own pictures under late/backgrounds, as (group key, path relative to that folder):
    group "" for the pictures shown in turn, else the occasion key (its folder)."""
    root = data_dir / FOLDER / BACKGROUNDS
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            rel = p.relative_to(root)
            out.append((rel.parts[0] if len(rel.parts) > 1 else "", rel.as_posix()))
    return out


def own_picture_path(data_dir: Path, rel: str) -> Path:
    """The file for a path from own_pictures; ValueError when it points outside the folder."""
    root = (data_dir / FOLDER / BACKGROUNDS).resolve()
    path = (root / rel).resolve()
    if path == root or root not in path.parents or path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise ValueError("not one of the background pictures")
    return path


def add_own_picture(data_dir: Path, jpeg: bytes, occasion: str = "") -> str:
    """Keep a picture for the day's rotation (no occasion) or for an occasion (its key), named so
    day_background finds it: own-<n>.jpg or <key>/<key>-<n>.jpg. Returns the path as own_pictures gives it."""
    folder = data_dir / FOLDER / BACKGROUNDS / occasion if occasion else data_dir / FOLDER / BACKGROUNDS
    folder.mkdir(parents=True, exist_ok=True)
    stem = occasion or ROTATION
    n = 1
    while (folder / f"{stem}-{n}.jpg").exists():
        n += 1
    (folder / f"{stem}-{n}.jpg").write_bytes(jpeg)
    return (f"{occasion}/" if occasion else "") + f"{stem}-{n}.jpg"


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


def _gradient(canvas: np.ndarray, top, bottom, until: int) -> None:
    top, bottom = np.array(top, np.float32), np.array(bottom, np.float32)
    for y in range(min(until, canvas.shape[0])):
        canvas[y] = top + (bottom - top) * (y / max(1, until))


def _sun(canvas, x, y, r, ray=(120, 220, 255), body=(90, 210, 255), core=(150, 235, 255)) -> None:
    import cv2

    for i in range(12):
        a = i * math.pi / 6
        p1 = (int(x + math.cos(a) * (r + 14)), int(y + math.sin(a) * (r + 14)))
        p2 = (int(x + math.cos(a) * (r + 42)), int(y + math.sin(a) * (r + 42)))
        cv2.line(canvas, p1, p2, ray, 6, cv2.LINE_AA)
    cv2.circle(canvas, (x, y), r, body, -1, cv2.LINE_AA)
    cv2.circle(canvas, (x, y), r - 12, core, -1, cv2.LINE_AA)


def _cloud(canvas, x, y, scale=1.0, colour=(255, 255, 255)) -> None:
    import cv2

    for dx, dy, r in ((0, 0, 34), (34, -10, 42), (76, 0, 36), (36, 18, 30), (-6, 14, 26)):
        cv2.circle(canvas, (int(x + dx * scale), int(y + dy * scale)), int(r * scale), colour, -1, cv2.LINE_AA)


def _star(canvas, x, y, r, colour, thick=-1) -> None:
    import cv2

    pts = [(x + r * (1 if i % 2 == 0 else 0.45) * math.cos(-math.pi / 2 + i * math.pi / 5),
            y + r * (1 if i % 2 == 0 else 0.45) * math.sin(-math.pi / 2 + i * math.pi / 5)) for i in range(10)]
    cv2.fillPoly(canvas, [np.array(pts, np.int32)], colour, cv2.LINE_AA) if thick < 0 else \
        cv2.polylines(canvas, [np.array(pts, np.int32)], True, colour, thick, cv2.LINE_AA)


def _sparkle(canvas, x, y, r, colour=(200, 255, 255)) -> None:
    import cv2

    cv2.line(canvas, (x - r, y), (x + r, y), colour, 2, cv2.LINE_AA)
    cv2.line(canvas, (x, y - r), (x, y + r), colour, 2, cv2.LINE_AA)
    cv2.circle(canvas, (x, y), max(1, r // 4), (255, 255, 255), -1, cv2.LINE_AA)


def _fairy(canvas, x, y, rng) -> None:
    """A small fairy: wings, a dress, a head with hair, a wand and a trail of sparkles."""
    import cv2

    wing = canvas.copy()
    cv2.ellipse(wing, (x - 14, y - 6), (16, 26), -30, 0, 360, (255, 235, 200), -1, cv2.LINE_AA)
    cv2.ellipse(wing, (x + 14, y - 6), (16, 26), 30, 0, 360, (255, 235, 200), -1, cv2.LINE_AA)
    cv2.addWeighted(wing, 0.65, canvas, 0.35, 0, canvas)
    dress = [(220, 150, 255), (180, 120, 250), (140, 200, 255), (200, 160, 120)][int(rng.integers(0, 4))]
    cv2.fillPoly(canvas, [np.array([(x, y - 6), (x - 13, y + 22), (x + 13, y + 22)], np.int32)], dress, cv2.LINE_AA)
    cv2.circle(canvas, (x, y - 14), 9, (170, 205, 245), -1, cv2.LINE_AA)  # face
    cv2.ellipse(canvas, (x, y - 18), (10, 7), 0, 180, 360, (40, 80, 150), -1, cv2.LINE_AA)  # hair
    cv2.line(canvas, (x + 10, y + 2), (x + 26, y - 14), (60, 120, 200), 2, cv2.LINE_AA)  # wand
    _star(canvas, x + 27, y - 15, 6, (80, 230, 255))
    for i in range(4):
        _sparkle(canvas, x - 30 - i * 16, y + 18 + i * 6, 5 - i, (190, 250, 255))


def _butterfly(canvas, x, y, rng) -> None:
    import cv2

    colour = [(80, 140, 255), (220, 120, 200), (90, 210, 255), (240, 180, 90)][int(rng.integers(0, 4))]
    for sx in (-1, 1):
        cv2.ellipse(canvas, (x + sx * 9, y - 5), (10, 8), sx * 30, 0, 360, colour, -1, cv2.LINE_AA)
        cv2.ellipse(canvas, (x + sx * 7, y + 6), (7, 6), -sx * 30, 0, 360, colour, -1, cv2.LINE_AA)
    cv2.line(canvas, (x, y - 9), (x, y + 10), (50, 50, 50), 2, cv2.LINE_AA)


def _balloon(canvas, x, y, rng) -> None:
    import cv2

    colour = [(90, 90, 240), (240, 160, 80), (90, 210, 255), (200, 120, 230), (110, 200, 110)][int(rng.integers(0, 5))]
    cv2.line(canvas, (x, y + 30), (x + int(rng.integers(-12, 12)), y + 95), (120, 120, 120), 1, cv2.LINE_AA)
    cv2.ellipse(canvas, (x, y), (22, 29), 0, 0, 360, colour, -1, cv2.LINE_AA)
    cv2.ellipse(canvas, (x - 7, y - 10), (5, 8), -20, 0, 360, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.fillPoly(canvas, [np.array([(x - 4, y + 32), (x + 4, y + 32), (x, y + 27)], np.int32)], colour, cv2.LINE_AA)


def _bird(canvas, x, y) -> None:
    import cv2

    cv2.ellipse(canvas, (x - 9, y), (10, 6), 0, 200, 340, (60, 60, 60), 2, cv2.LINE_AA)
    cv2.ellipse(canvas, (x + 9, y), (10, 6), 0, 200, 340, (60, 60, 60), 2, cv2.LINE_AA)


def _meadow(canvas, rng) -> None:
    import cv2

    h, w = canvas.shape[:2]
    horizon = int(h * 0.62)
    _gradient(canvas, (252, 190, 120), (225, 240, 255), horizon + 40)
    _sun(canvas, w - 150, 110, 52)
    for _ in range(4):
        _cloud(canvas, int(rng.integers(80, w - 300)), int(rng.integers(60, max(70, horizon - 160))))
    cv2.ellipse(canvas, (int(w * 0.25), horizon + 40), (int(w * 0.5), 110), 0, 180, 360, (110, 190, 120), -1, cv2.LINE_AA)
    cv2.ellipse(canvas, (int(w * 0.8), horizon + 50), (int(w * 0.45), 130), 0, 180, 360, (95, 175, 105), -1, cv2.LINE_AA)
    canvas[horizon + 40:] = (80, 165, 95)
    for _ in range(30):
        fx, fy = int(rng.integers(20, w - 20)), int(rng.integers(horizon + 60, h - 10))
        colour = [(120, 120, 255), (255, 200, 120), (140, 220, 255), (255, 160, 220)][int(rng.integers(0, 4))]
        cv2.circle(canvas, (fx, fy), 4, colour, -1, cv2.LINE_AA)
        cv2.circle(canvas, (fx, fy), 2, (120, 240, 255), -1, cv2.LINE_AA)


def _beach(canvas, rng) -> None:
    import cv2

    h, w = canvas.shape[:2]
    sea, sand = int(h * 0.55), int(h * 0.68)
    _gradient(canvas, (250, 200, 110), (240, 235, 210), sea)
    _sun(canvas, int(w * 0.15), 105, 50)
    for _ in range(3):
        _cloud(canvas, int(rng.integers(w // 3, w - 200)), int(rng.integers(60, max(70, sea - 140))), 0.9)
    canvas[sea:sand] = (200, 160, 60)
    for y in range(sea + 12, sand, 18):  # little waves
        for x in range(int(rng.integers(0, 40)), w, 80):
            cv2.ellipse(canvas, (x, y), (22, 5), 0, 180, 360, (235, 210, 150), 2, cv2.LINE_AA)
    canvas[sand:] = (150, 210, 235)
    px = w - 170  # a palm tree
    cv2.line(canvas, (px, sand + 40), (px + 30, sand - 170), (60, 100, 140), 16, cv2.LINE_AA)
    for a in (-160, -120, -60, -20, 200):
        r = math.radians(a)
        cv2.ellipse(canvas, (px + 30 + int(55 * math.cos(r)), sand - 170 + int(25 * math.sin(r))), (60, 14), a,
                    0, 360, (70, 160, 80), -1, cv2.LINE_AA)
    for _ in range(8):  # shells and starfish
        x, y = int(rng.integers(30, w - 30)), int(rng.integers(sand + 20, h - 10))
        _star(canvas, x, y, 9, (110, 150, 245)) if rng.random() < 0.5 else \
            cv2.ellipse(canvas, (x, y), (8, 6), 0, 180, 360, (200, 220, 245), -1, cv2.LINE_AA)


def _night(canvas, rng) -> None:
    import cv2

    h, w = canvas.shape[:2]
    ground = int(h * 0.72)
    _gradient(canvas, (70, 30, 25), (140, 80, 70), ground)
    for _ in range(120):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, ground - 20))
        cv2.circle(canvas, (x, y), int(rng.integers(1, 3)), (230, 245, 255), -1, cv2.LINE_AA)
    for _ in range(6):
        _star(canvas, int(rng.integers(40, w - 40)), int(rng.integers(30, ground // 2)), int(rng.integers(6, 11)),
              (120, 230, 255))
    cv2.circle(canvas, (w - 160, 110), 55, (200, 240, 255), -1, cv2.LINE_AA)  # moon
    cv2.circle(canvas, (w - 135, 95), 50, tuple(int(v) for v in canvas[95, w - 60]), -1, cv2.LINE_AA)
    canvas[ground:] = (55, 90, 50)
    cv2.ellipse(canvas, (w // 3, ground + 10), (int(w * 0.5), 70), 0, 180, 360, (60, 100, 55), -1, cv2.LINE_AA)
    for _ in range(14):  # fireflies
        _sparkle(canvas, int(rng.integers(20, w - 20)), int(rng.integers(ground - 60, h - 20)), 4, (120, 255, 230))


def _autumn(canvas, rng) -> None:
    import cv2

    h, w = canvas.shape[:2]
    ground = int(h * 0.7)
    _gradient(canvas, (230, 200, 160), (200, 225, 250), ground)
    _sun(canvas, w - 140, 100, 44, (120, 200, 255), (80, 170, 255), (130, 210, 255))
    for x in (int(w * 0.08), int(w * 0.9)):  # two trees
        cv2.line(canvas, (x, ground + 10), (x, ground - 150), (40, 70, 110), 18, cv2.LINE_AA)
        for _ in range(9):
            cv2.circle(canvas, (x + int(rng.integers(-70, 70)), ground - 170 + int(rng.integers(-60, 50))),
                       int(rng.integers(35, 55)), [(40, 120, 230), (40, 160, 245), (60, 90, 200)][int(rng.integers(0, 3))],
                       -1, cv2.LINE_AA)
    canvas[ground:] = (70, 130, 170)
    for _ in range(45):  # falling leaves
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h - 5))
        cv2.ellipse(canvas, (x, y), (8, 4), int(rng.integers(0, 180)), 0, 360,
                    [(40, 120, 230), (40, 170, 245), (50, 80, 190)][int(rng.integers(0, 3))], -1, cv2.LINE_AA)


def _snow(canvas, rng) -> None:
    import cv2

    h, w = canvas.shape[:2]
    ground = int(h * 0.7)
    _gradient(canvas, (210, 170, 140), (245, 235, 225), ground)
    canvas[ground:] = (250, 248, 245)
    cv2.ellipse(canvas, (int(w * 0.7), ground + 20), (int(w * 0.5), 90), 0, 180, 360, (245, 240, 235), -1, cv2.LINE_AA)
    sx = int(w * 0.92)  # a snowman
    for r, dy in ((48, 0), (36, -70), (26, -125)):
        cv2.circle(canvas, (sx, ground + dy), r, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(canvas, (sx - 8, ground - 130), 3, (30, 30, 30), -1, cv2.LINE_AA)
    cv2.circle(canvas, (sx + 8, ground - 130), 3, (30, 30, 30), -1, cv2.LINE_AA)
    cv2.fillPoly(canvas, [np.array([(sx, ground - 124), (sx, ground - 118), (sx + 20, ground - 121)], np.int32)],
                 (40, 140, 245), cv2.LINE_AA)
    cv2.rectangle(canvas, (sx - 22, ground - 158), (sx + 22, ground - 150), (40, 40, 40), -1)
    cv2.rectangle(canvas, (sx - 14, ground - 185), (sx + 14, ground - 155), (40, 40, 40), -1)
    for _ in range(90):
        cv2.circle(canvas, (int(rng.integers(0, w)), int(rng.integers(0, h))), int(rng.integers(2, 5)),
                   (255, 255, 255), -1, cv2.LINE_AA)


SCENES = (_meadow, _beach, _night, _autumn, _snow)
EXTRAS = ("fairy", "butterfly", "balloon", "bird")


def draw_background(width: int, height: int, seed: int = 0) -> np.ndarray:
    """A cheerful scene that changes from day to day (`seed`: the day): a meadow, a beach, a starry
    night, autumn or snow."""
    rng = np.random.default_rng(seed)
    canvas = np.zeros((height, width, 3), np.uint8)
    SCENES[seed % len(SCENES)](canvas, rng)
    return canvas


def draw_extras(canvas: np.ndarray, free: list[tuple[int, int, int, int]], seed: int = 0) -> None:
    """Two kinds of fairies, butterflies, balloons or birds, a few of each, scattered in the `free`
    boxes (x1, y1, x2, y2) the cards and the title leave, so none hides behind a card."""
    rng = np.random.default_rng(seed + 1)
    boxes = [(x1 + 40, y1 + 35, x2 - 40, y2 - 35) for x1, y1, x2, y2 in free if x2 - x1 > 120 and y2 - y1 > 80]
    if not boxes:
        return
    draw = {"fairy": _fairy, "butterfly": _butterfly, "balloon": _balloon, "bird": lambda c, x, y, r: _bird(c, x, y)}
    for kind in rng.choice(EXTRAS, size=2, replace=False):
        for _ in range(int(rng.integers(2, 5))):
            x1, y1, x2, y2 = boxes[int(rng.integers(0, len(boxes)))]
            draw[str(kind)](canvas, int(rng.integers(x1, x2)), int(rng.integers(y1, y2)), rng)


def day_background(data_dir: Path, day: dt.date, occasions: bool = True) -> Path | None:
    """The picture behind the day's cards: the admin's single background if uploaded; else around a
    holiday (occasions.py) its picture, the admin's own in late/backgrounds/<key>/ before the bundled
    ones; else one of the pictures in late/backgrounds (or the bundled old pictures: the Dong Ho
    mice's wedding and rooster, roses, cherry blossoms, parrots), a different one each day.
    None: draw a scene."""
    from .occasions import occasion_on

    own = background_path(data_dir)
    if own.exists():
        return own

    def pictures(folder: Path, key: str = "") -> list[Path]:
        """The pictures in `folder`; with `key`, those named <key>.jpg or <key>-<n>.jpg."""
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
                      and (not key or p.stem == key or p.stem.startswith(key + "-")))

    occasion = occasion_on(day) if occasions else None
    choice = []
    if occasion is not None:
        choice = pictures(data_dir / FOLDER / BACKGROUNDS / occasion.key) or pictures(BUNDLED / OCCASIONS, occasion.key)
        if choice:  # the same picture all through the occasion, another one next year
            return choice[day.year % len(choice)]
    choice = pictures(data_dir / FOLDER / BACKGROUNDS) or pictures(BUNDLED)
    return choice[day.toordinal() % len(choice)] if choice else None


def load_background(path: Path | None, width: int, height: int, seed: int = 0) -> np.ndarray:
    """A picture scaled and cropped to fill the canvas, or the drawn scene."""
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


def _fill(photo: np.ndarray, w: int, h: int) -> np.ndarray:
    """`photo` scaled to cover w×h and cropped to it, keeping its top (where the head is)."""
    import cv2

    ph, pw = photo.shape[:2]
    scale = max(w / pw, h / ph)
    photo = cv2.resize(photo, (max(w, round(pw * scale)), max(h, round(ph * scale))), interpolation=cv2.INTER_AREA)
    x = (photo.shape[1] - w) // 2
    return photo[:h, x:x + w]


def _placeholder(w: int, h: int) -> np.ndarray:
    """A grey figure with a question mark, for someone whose arrival was entered without a photo."""
    import cv2

    image = np.full((h, w, 3), 235, np.uint8)
    grey = (175, 175, 175)
    cv2.circle(image, (w // 2, int(h * 0.28)), int(w * 0.2), grey, -1, cv2.LINE_AA)
    cv2.ellipse(image, (w // 2, int(h * 0.78)), (int(w * 0.36), int(h * 0.3)), 0, 180, 360, grey, -1, cv2.LINE_AA)
    (tw, th), _ = cv2.getTextSize("?", FONT, 1.6, 3)
    cv2.putText(image, "?", ((w - tw) // 2, int(h * 0.28) + th // 2), FONT, 1.6, (255, 255, 255), 3, cv2.LINE_AA)
    return image


def _card(person: Person) -> tuple[np.ndarray, np.ndarray]:
    """The person's camera photo as a card: CARD_W×CARD_H, a white border and rounded corners.
    The whole rectangle is shown, so nobody loses their head to a cut-out."""
    import cv2

    photo = cv2.imread(str(person.photo)) if person.photo and person.photo.exists() else None
    inner_w, inner_h = CARD_W - 2 * BORDER, CARD_H - 2 * BORDER
    inner = _fill(photo, inner_w, inner_h) if photo is not None and photo.size else _placeholder(inner_w, inner_h)
    image = cv2.copyMakeBorder(inner, BORDER, BORDER, BORDER, BORDER, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    alpha = np.zeros((CARD_H, CARD_W), np.float32)
    _rounded(alpha, 0, 0, CARD_W - 1, CARD_H - 1, 1.0, r=16)
    return image, alpha


def _tag(canvas: np.ndarray, cx: int, y: int, name: str, line2: str, width: int) -> None:
    """A rounded name tag centred on cx with its top at y, the name as large as fits `width`."""
    import cv2

    name = ascii_label(name)
    size = 0.66
    while size > 0.42 and _width(name, size, 2) > width - 20:  # shrink a long name before cutting it
        size -= 0.04
    name = _fit(name, width - 20, size, 2)
    line2 = _fit(line2, width - 20, 0.52)
    w = min(width, max(_width(name, size, 2), _width(line2, 0.52)) + 28)
    x1, x2 = cx - w // 2, cx + w // 2
    _rounded(canvas, x1 + 3, y + 3, x2 + 3, y + TAG_H + 3, (40, 40, 40), 10, alpha=0.35)  # shadow
    _rounded(canvas, x1, y, x2, y + TAG_H, (255, 255, 255), 10)
    cv2.putText(canvas, name, (cx - _width(name, size, 2) // 2, y + 25), FONT, size, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.putText(canvas, line2, (cx - _width(line2, 0.52) // 2, y + 47), FONT, 0.52, (0, 110, 230), 1, cv2.LINE_AA)


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


def layout(n: int) -> tuple[int, int]:
    """(rows, people per row): at most PER_ROW in a row, rows as even as possible (7 = 4 + 3)."""
    rows = max(1, math.ceil(n / PER_ROW))
    return rows, math.ceil(n / rows)


def compose(people: list[Person], title: str, background: Path | None = None, seed: int = 0) -> bytes:
    """Everyone's card in rows on the background, each with a name tag, as JPEG."""
    import cv2

    from ..vision.faces import encode_jpeg

    if not people:
        raise ValueError("nobody to show")
    rows, per_row = layout(len(people))
    slot = CARD_W + GAP
    width = max(MIN_WIDTH, 2 * MARGIN + per_row * slot)
    row_h = CARD_H + TAG_GAP + TAG_H + ROW_GAP
    height = max(HEIGHT, TOP + rows * row_h + BOTTOM - ROW_GAP)
    canvas = load_background(background, width, height, seed)
    top = TOP + (height - TOP - BOTTOM - (rows * row_h - ROW_GAP)) // 2  # centre the rows below the banner
    for r in range(rows):
        row = people[r * per_row:(r + 1) * per_row]
        left = (width - len(row) * slot) // 2 + GAP // 2  # a short last row is centred too
        y = top + r * row_h
        for i, person in enumerate(row):
            x = left + i * slot
            image, alpha = _card(person)
            shadow = canvas.copy()
            _rounded(shadow, x + 6, y + 8, x + CARD_W + 6, y + CARD_H + 8, (30, 30, 30), 16)
            cv2.addWeighted(shadow, 0.3, canvas, 0.7, 0, canvas)
            _paste(canvas, image, alpha, x, y)
            _tag(canvas, x + CARD_W // 2, y + CARD_H + TAG_GAP, person.name,
                 f"{person.time}  +{person.minutes} min", slot - 4)
    bottom = top + rows * row_h - ROW_GAP + 8
    banner_right = 24 + _width(_fit(ascii_label(title), width - 160, 0.9, 2), 0.9, 2) + 110
    if background is None or not background.exists():  # fairies and balloons belong to the drawn scenes
        draw_extras(canvas, [(banner_right, 0, width, top - 8), (0, 96, width, top - 8), (0, bottom, width, height)], seed)
    _banner(canvas, title)
    return encode_jpeg(canvas)


# files --------------------------------------------------------------------------------------

LOOK = 4  # raise when the drawing changes, so pictures already made are made again


def fingerprint(people: list[Person], background: Path | None) -> str:
    """Changes when the picture would: someone added or removed, a time corrected, a photo
    replaced, the background or the drawing changed."""
    def stat(p: Path | None):
        s = p.stat() if p and p.exists() else None
        return (str(p), s.st_size if s else 0, s.st_mtime_ns if s else 0)

    parts = [(p.name, p.time, p.minutes, stat(p.photo)) for p in people] + [stat(background), LOOK]
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def write(data_dir: Path, day: dt.date, people: list[Person], title: str, occasions: bool = True) -> str:
    """Make (or refresh, when something changed) the day's picture; returns its path under data_dir.
    `occasions`: holiday backgrounds around holidays."""
    rel = photo_path(day)
    path = data_dir / rel
    key = path.with_suffix(".key")
    background = day_background(data_dir, day, occasions)
    fp = fingerprint(people, background)
    if path.exists() and key.exists() and key.read_text() == fp:
        return rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(compose(people, title, background, seed=day.toordinal()))
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
