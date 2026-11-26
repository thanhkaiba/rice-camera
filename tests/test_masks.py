import datetime as dt
from types import SimpleNamespace

import pytest

from attendance.core import masks
from attendance.core.config import Config

T0 = dt.datetime(2026, 11, 25, 8, 55, 55)


EMB = object()  # the face's own track holds it as its latest face


def face():
    return SimpleNamespace(size=120, landmarks=object(), embedding=EMB)


def run(monkeypatch, covered=True, named=False, frames=6, step=0.4):
    monkeypatch.setattr(masks, "looks_covered", lambda frame, lm: covered)
    asked = []
    cfg = Config()
    cfg.greeting.enabled = cfg.greeting.mask_nudge = True
    watch = masks.MaskWatch(cfg, asked.append)
    tracks = [SimpleNamespace(person_id=7, decided="someone" if named else None, embedding=EMB)]
    for i in range(frames):
        watch.observe(tracks, [(face(), [], 7)], None, T0 + dt.timedelta(seconds=step * i))
    return asked


def test_a_masked_face_not_named_is_asked_once(monkeypatch):
    assert len(run(monkeypatch)) == 1


@pytest.mark.parametrize("kw", [dict(covered=False), dict(named=True), dict(frames=2), dict(step=0.1, frames=4)])
def test_not_asked(monkeypatch, kw):
    assert run(monkeypatch, **kw) == []


