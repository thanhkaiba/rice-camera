import datetime as dt
from types import SimpleNamespace

import numpy as np

from attendance.core.crowd import GROUP_MIN, CrowdLog

T0 = dt.datetime(2026, 11, 20, 8, 57, 0, tzinfo=dt.timezone(dt.timedelta(hours=7)))
FRAME = np.zeros((720, 1280, 3), dtype=np.uint8)


def face(x, who=None, score=0.6, ok=True, pid=None):
    f = SimpleNamespace(bbox=(x, 100.0, x + 90.0, 200.0), quality_ok=ok, size_ok=ok)
    candidates = [(who, score), ("other", 0.1)] if who else [("other", 0.2), ("else", 0.15)]
    return f, candidates, pid


def body(n, x):
    return SimpleNamespace(id=n, box=np.array([x, 80.0, x + 150.0, 600.0]))


def log(core):
    app, _, _ = core
    sent = []
    return app, CrowdLog(app.cfg, app.store, app.data_dir, send=sent.append), sent


def test_a_group_is_counted_once_it_has_walked_on(core):
    app, crowd, sent = log(core)
    people = [body(1, 100), body(2, 400), body(3, 700), body(4, 1000)]
    at = lambda s: T0 + dt.timedelta(seconds=s)
    assert crowd.observe(at(0), FRAME, [face(110, "A", pid=1)], people[:2]) is None  # two people: not a group
    assert crowd.observe(at(1), FRAME, [face(110, "A", pid=1), face(410, pid=2)], people[:3]) is None
    assert crowd.observe(at(2), FRAME, [face(110, "A", pid=1), face(410, "B", pid=2), face(710, "C", ok=False, pid=3)], people) is None
    assert crowd.observe(at(3), FRAME, [], people[:1]) is None  # they are leaving the picture
    assert crowd.observe(at(4.5), FRAME, [], []) is None
    row = crowd.observe(at(5.5), FRAME, [], [])
    assert row and (row["people"], row["faces"], row["named"], row["names"], row["bodies"]) == (4, 3, 2, ["A", "B"], True)
    kept = app.store.crowds(T0.date())
    assert len(kept) == 1 and kept[0]["named"] == 2 and kept[0]["image"].startswith("crops/2026-11-20/crowd_085701_")
    assert (app.data_dir / kept[0]["image"]).read_bytes().startswith(b"\xff\xd8")
    assert sent == [row]
    assert app.store.crowd_days(T0.date()) == [{"day": "2026-11-20", "groups": 1, "people": 4, "faces": 3, "named": 2, "bodies": 1}]
    assert crowd.observe(at(8), FRAME, [], []) is None and len(app.store.crowds(T0.date())) == 1


def test_without_bodies_faces_are_counted_and_a_decision_names_too(core):
    app, crowd, _ = log(core)
    faces = [face(100), face(400), face(700)]
    decision = SimpleNamespace(kind="identified", employee_id="A")
    crowd.observe(T0, FRAME, faces, [], [decision])
    row = crowd.observe(T0 + dt.timedelta(seconds=4), FRAME, [], [])
    assert (row["people"], row["named"], row["bodies"]) == (GROUP_MIN, 1, False)


def test_a_group_that_stays_is_closed_and_old_ones_are_forgotten(core):
    app, crowd, _ = log(core)
    people = [body(n, 100 + 300 * n) for n in range(3)]
    row = None
    for s in range(0, 60):
        row = row or crowd.observe(T0 + dt.timedelta(seconds=s), FRAME, [], people)
    assert row and row["named"] == 0
    images = app.store.expire_crowds(T0.date() + dt.timedelta(days=1))
    assert images and app.store.crowd_days(T0.date()) == []


def test_groups_page_and_the_event_from_the_mac(core):
    from attendance.web import create_app
    app, crowd, _ = log(core)
    crowd.observe(T0, FRAME, [face(110, "A", pid=1)], [body(1, 100), body(2, 400), body(3, 700)])
    crowd.observe(T0 + dt.timedelta(seconds=4), FRAME, [], [])
    client = create_app(app).test_client()
    page = client.get("/groups?day=2026-11-20").get_data(as_text=True)
    assert "Groups at the door" in page and "3 people" in page and "1 named" in page and "33%" in page
    assert client.get("/groups?day=2020-01-01").status_code == 200
