"""Starting without reference photos: the camera collects faces and the admin names them once."""
import datetime as dt

import numpy as np
import pytest

from attendance.web import create_app
from conftest import DAY, at, person_vector
from test_service import run_until


def near(person: int, seed: int) -> np.ndarray:
    """Another photo of the same person: their vector plus a little noise."""
    v = person_vector(person) + 0.25 * np.random.default_rng(seed).normal(size=512).astype(np.float32) / 22.6
    return v / np.linalg.norm(v)


def test_photos_of_the_same_person_are_grouped(env):
    service, store, clock, _ = env
    a1 = service.on_unknown(at(9, 1), None, near(30, 1), None, None)
    a2 = service.on_unknown(at(9, 20), None, near(30, 2), None, None)
    b1 = service.on_unknown(at(9, 21), None, near(31, 3), None, None)
    service.on_unknown(at(9, 30, day=DAY + dt.timedelta(days=1)), None, near(30, 4), None, None)
    groups = store.pending_groups()
    assert sorted(len(g) for g in groups) == [1, 3]
    assert store.unknown(a2).group_id == store.unknown(a1).group_id != store.unknown(b1).group_id
    assert store.pending_group_count() == 2 and store.pending_group_count(DAY) == 2


def test_confirming_a_group_learns_several_photos_and_records_every_day(env):
    service, store, clock, sent = env
    clock.t = at(9, 0, day=DAY + dt.timedelta(days=1))
    ids = [service.on_unknown(at(9, m), None, near(30, m), None, None) for m in (5, 6, 7, 8, 9, 10, 11)]
    service.on_unknown(at(9, 40, day=DAY + dt.timedelta(days=1)), None, near(30, 99), None, None)
    service.resolve_unknown(ids[3], "B")
    assert store.pending_group_count() == 0
    assert len([r for r in store.references(service.model_id, "B") if r.source == "admin"]) == 5
    assert store.checkins(DAY)["B"].first_seen == at(9, 5)  # earliest sighting of that day
    assert store.checkins(DAY + dt.timedelta(days=1))["B"].first_seen == at(9, 40, day=DAY + dt.timedelta(days=1))
    assert not any("Update" in m and "2026-10-14" in m for m in sent)  # past days are corrected quietly


def test_new_employee_can_be_created_from_a_face(env):
    service, store, clock, _ = env
    uid = service.on_unknown(at(9, 5), None, near(40, 1), None, None)
    service.on_unknown(at(9, 6), None, near(40, 2), None, None)
    service.add_employee_from_unknown(uid, "dana@example.com", "Dana")
    dana = store.employee_by_email("dana@example.com")
    assert dana.name == "Dana"
    assert service.gallery.counts()[dana.id] == 2
    assert dana.id in store.checkins(DAY)
    with pytest.raises(ValueError):
        service.add_employee_from_unknown(uid, "eve@example.com", "Eve")  # already handled
    with pytest.raises(ValueError):
        u2 = service.on_unknown(at(9, 7), None, near(41, 1), None, None)
        service.add_employee_from_unknown(u2, "alice@example.com", "Duplicate email")
    assert store.unknown(u2).status == "pending"  # nothing changed when the email was taken


def test_ignoring_hides_the_whole_group(env):
    service, store, _, _ = env
    uid = service.on_unknown(at(9, 5), None, near(50, 1), None, None)
    service.on_unknown(at(9, 6), None, near(50, 2), None, None)
    service.ignore_unknown(uid)
    assert store.pending_groups() == []


def test_review_page_groups_and_new_employee_form(core):
    app, clock, _ = core
    for m in (5, 6, 7):
        app.service.on_unknown(at(9, m), None, near(60, m), None, None)
    flask_app = create_app(app)
    flask_app.testing = True
    c = flask_app.test_client()
    page = c.get("/unknowns").get_data(as_text=True)
    assert "Seen 3 times" in page and "New employee" in page and 'value="D"' not in page
    uid = app.store.pending_groups()[0][-1].id
    resp = c.post(f"/unknowns/{uid}/new-employee", data={"email": "linh@example.com", "name": "Linh"}, follow_redirects=True)
    assert "Linh added as a new employee" in resp.get_data(as_text=True)
    assert app.store.employee_by_email("linh@example.com") and app.store.pending_group_count() == 0
    assert '<span class="count">' not in c.get("/").get_data(as_text=True)


def test_ignore_lasts_only_for_the_day_and_can_be_undone(env):
    service, store, clock, sent = env
    clock.t = at(9, 10)
    uid = service.on_unknown(at(9, 5), None, near(70, 1), None, None)
    service.ignore_unknown(uid)
    assert store.pending_groups() == []

    # The visitor walks past again later that day: hidden, and no chat notice about it.
    again = service.on_unknown(at(10, 30), None, near(70, 2), None, None)
    assert store.unknown(again).status == "ignored" and store.pending_groups() == []
    assert len(store.ignored_groups(DAY)[0]) == 2
    run_until(service, clock, at(10, 45))
    assert not any(m.startswith("❓") for m in sent)

    # Undo by mistake: the whole group comes back for review.
    service.restore_unknown(uid)
    assert len(store.pending_groups()[0]) == 2 and store.ignored_groups(DAY) == []
    service.ignore_unknown(uid)

    # The next day the same person is shown for review again.
    tomorrow = DAY + dt.timedelta(days=1)
    clock.t = at(9, 0, day=tomorrow)
    nxt = service.on_unknown(at(9, 5, day=tomorrow), None, near(70, 3), None, None)
    assert store.unknown(nxt).status == "pending" and store.pending_group_count() == 1
    with pytest.raises(ValueError):
        service.restore_unknown(uid)  # yesterday's ignore can no longer be undone


def test_review_page_shows_ignored_today_with_restore(core):
    app, clock, _ = core
    clock.t = at(9, 10)
    uid = app.service.on_unknown(at(9, 5), None, near(80, 1), None, None)
    flask_app = create_app(app)
    flask_app.testing = True
    c = flask_app.test_client()
    page = c.post(f"/unknowns/{uid}/ignore", follow_redirects=True).get_data(as_text=True)
    assert "Ignored for today" in page and "Ignored today (1)" in page
    page = c.post(f"/unknowns/{uid}/restore", follow_redirects=True).get_data(as_text=True)
    assert "Restored" in page and "Ignored today" not in page and app.store.pending_group_count() == 1


def test_review_page_shows_the_whole_person(core):
    from attendance.pipeline import save_crop
    from attendance.recognizer import crop_body
    from attendance.tracker import Decision

    frame = np.zeros((500, 1000, 3), dtype=np.uint8)
    body = crop_body(frame, (450, 100, 500, 160))  # a 50x60 face: 4 face widths, down to the bottom of the picture
    assert body.shape[:2] == (500 - 40, 200)
    app, clock, _ = core
    d = Decision("unknown", at(9, 5), None, None, frame[100:160, 450:500], near(90, 1), body)
    image = save_crop(app, d, at(9, 5))
    assert (app.data_dir / image.replace(".jpg", "_body.jpg")).exists()
    app.service.on_unknown(at(9, 5), image, near(90, 1), None, None)
    flask_app = create_app(app)
    flask_app.testing = True
    page = flask_app.test_client().get("/unknowns").get_data(as_text=True)
    assert image.replace(".jpg", "_body.jpg") in page and image in page
