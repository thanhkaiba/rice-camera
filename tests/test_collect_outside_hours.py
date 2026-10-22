"""Collecting reference photos outside the camera hours (collect.py)."""
import numpy as np
import pytest

from attendance.recognizer import DetectedFace
from conftest import DAY, at, face_image, person_vector

FRAME = face_image(1, 300)


def unit(*parts):
    v = sum(parts)
    return (v / np.linalg.norm(v)).astype(np.float32)


def alice(i: int) -> np.ndarray:
    """Alice in another pose or light: clearly her, but not the same picture twice."""
    return unit(person_vector(1), 0.3 * person_vector(50 + i))


def face(vector, quality=0.8, quality_ok=True):
    return DetectedFace((50.0, 50.0, 150.0, 150.0), vector, 0.9, 120.0, 100.0, 0.0, 0.0, quality_ok, quality)


@pytest.fixture
def collect(core):
    app, clock, _ = core
    app.cfg.collect.enabled = True
    clock.t = at(15, 0)
    return app, clock, app.collector


def seen(app, collector, vector, at_s, pid=None, **kw):
    f = face(vector, **kw)
    return collector.observe([(f, app.gallery.match(vector), pid)], FRAME, app.service.now(), at=at_s)


def collected(app, employee="A"):
    return [r for r in app.store.references(app.cfg.recognition.model_id(), employee) if r.source == "collect"]


def test_runs_on_work_days_within_its_hours_outside_the_camera_hours(collect):
    app, clock, c = collect
    assert c.running(at(15, 0), in_window=False)
    assert not c.running(at(9, 0), in_window=True)
    assert not c.running(at(6, 59), in_window=False) and not c.running(at(20, 0), in_window=False)
    saturday = DAY.replace(day=17)
    assert not c.running(at(15, 0, day=saturday), in_window=False)
    app.cfg.collect.enabled = False
    assert not c.running(at(15, 0), in_window=False)


def test_one_clear_photo_per_walk_past_confirmed_by_a_second_look(collect):
    app, clock, c = collect
    assert seen(app, c, alice(0), 0.0) == 0  # the first clear look only says who it is
    assert seen(app, c, alice(1), 0.5) == 1
    assert seen(app, c, alice(2), 1.0) == 0  # one photo from this walk past is enough
    assert seen(app, c, alice(3), 10.0) == 0  # the next walk past: confirmed first again
    assert seen(app, c, alice(4), 10.5) == 1
    refs = collected(app)
    assert len(refs) == 2 and all(r.image_score == 80 and r.quality_ok for r in refs)
    assert all((app.data_dir / r.image).exists() for r in refs)
    assert app.gallery.counts()["A"] == 3
    assert c.status()["kept_today"] == 2


def test_only_clear_faces_of_people_recognised_without_doubt(collect):
    app, clock, c = collect
    for t in (0.0, 0.5):
        seen(app, c, alice(0), t, quality=0.64)  # image score 64, below 65
    for t in (5.0, 5.5):
        seen(app, c, alice(1), t, quality_ok=False)  # blurred or turned away
    between = unit(person_vector(1), person_vector(2))  # half Alice, half Bob
    for t in (10.0, 10.5):
        seen(app, c, between, t)
    assert collected(app) == [] and collected(app, "B") == []


def test_the_confirming_look_must_be_of_the_same_person_and_recent(collect):
    app, clock, c = collect
    seen(app, c, alice(0), 0.0, pid=1)
    assert seen(app, c, alice(1), 0.5, pid=2) == 0  # another followed person: not a second look
    assert seen(app, c, alice(2), 5.0, pid=2) == 0  # too long after
    assert seen(app, c, alice(3), 5.5, pid=2) == 1


def test_a_few_a_day_and_none_for_someone_who_has_enough(collect):
    app, clock, c = collect
    app.cfg.collect.per_day = 2
    for walk in range(4):
        seen(app, c, alice(2 * walk), walk * 10.0)
        seen(app, c, alice(2 * walk + 1), walk * 10.0 + 0.5)
    assert len(collected(app)) == 2

    bob = lambda i: unit(person_vector(2), 0.3 * person_vector(80 + i))  # noqa: E731
    model = app.cfg.recognition.model_id()
    app.store.add_embedding(model, "B", bob(9), "upload", clock(), None, True, 0.9)
    app.cfg.collect.enough = 1
    c._counted = -1e9  # read the counts again now
    seen(app, c, bob(0), 100.0)
    assert seen(app, c, bob(1), 100.5) == 0 and collected(app, "B") == []


def test_settings_and_camera_page(collect):
    from attendance.web import create_app

    app, clock, c = collect
    app.cfg.camera.url = "rtsp://cam/stream"
    client = create_app(app).test_client()
    assert "Collecting reference photos" in client.get("/live").get_data(as_text=True)
    assert client.get("/live.json").get_json()["collect"]["running"] is True
    page = client.get("/settings").get_data(as_text=True)
    assert 'name="collect.min_score"' in page and 'value="65"' in page
    with pytest.raises(ValueError):
        app.cfg.collect.start, app.cfg.collect.end = "20:00", "07:00"
        app.cfg.validate()
