import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

# Make `attendance` and the test helpers importable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from attendance.config import Config  # noqa: E402
from attendance.pipeline import App  # noqa: E402
from attendance.recognizer import DetectedFace  # noqa: E402
from attendance.store import Store  # noqa: E402

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DAY = dt.date(2026, 10, 14)  # Wednesday


def at(hh: int, mm: int, ss: int = 0, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=TZ)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def face_image(person: int, size: int = 200) -> np.ndarray:
    """A fake photo: a flat grey image whose brightness (person * 10) tells FakeRecognizer who it is.
    Flat images survive JPEG compression and cropping, so re-embedding saved photos also works."""
    return np.full((size, size, 3), person * 10, dtype=np.uint8)


def person_vector(person: int) -> np.ndarray:
    v = np.random.default_rng(person).normal(size=512).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeRecognizer:
    """Pretends every image holds one face; its brightness says whose (0 = no face)."""

    def largest_face(self, image, mirror=False):
        person = int(round(float(image.mean()) / 10))
        if person == 0:
            return None
        return DetectedFace((50.0, 50.0, 150.0, 150.0), person_vector(person), 0.9, 100.0, 100.0, 0.0, 0.0, True, 0.8)

    def analyze(self, image):
        face = self.largest_face(image)
        return [face] if face is not None else []

    def analyze_photo(self, image):
        return self.analyze(image)


@pytest.fixture
def core(tmp_path):
    (tmp_path / "employees.csv").write_text("email,name\nalice@example.com,Alice\nbob@example.com,Bob\ncarol@example.com,Carol\n", encoding="utf-8")
    seed = Store(tmp_path / "data" / "attendance.db")
    with seed._db:
        seed._db.executemany("INSERT INTO employees (id,email,name) VALUES (?,?,?)",
                             [("A", "alice@example.com", "Alice"), ("B", "bob@example.com", "Bob"),
                              ("C", "carol@example.com", "Carol")])
    seed.close()
    cfg = Config(employees_file="employees.csv", data_dir="data", base_dir=tmp_path)
    cfg.web.public_url = "http://pc:8000"
    cfg.google_chat.webhook_url = "https://chat.googleapis.com/v1/spaces/X/messages?key=k&token=t"
    cfg.validate()
    clock = Clock(at(8, 0))
    app = App(cfg, now=clock)
    sent = []
    app.chat._post = lambda url, payload: sent.append(payload["text"])
    app._recognizer = FakeRecognizer()
    for i, emp in enumerate("ABC", start=1):  # everyone has a reference photo unless a test removes it
        app.store.add_embedding(cfg.recognition.model_id(), emp, person_vector(i), "upload", clock())
    app.library.reload()
    return app, clock, sent


@pytest.fixture
def env(core):
    app, clock, sent = core
    return app.service, app.store, clock, sent
