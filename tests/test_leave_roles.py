import base64
import datetime as dt
import re

from attendance.store import Store
from attendance.web import create_app, personal
from conftest import DAY, at
from test_leave_api import connect


def client(core_app, password="", viewer=""):
    core_app.cfg.web.password = password
    core_app.cfg.web.viewer_password = viewer
    app = create_app(core_app)
    app.testing = True
    return app.test_client()


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


ADMIN = basic("admin", "s3cret")
VIEWER = basic("viewer", "look")


# manual leave and holidays -----------------------------------------------------------------------

def test_manual_entries_survive_the_hr_update_and_win_over_its_days(core):
    app, clock, _ = core
    store = app.store
    manual = store.add_leave("A", DAY, DAY + dt.timedelta(days=1), "business_trip", "client visit")
    store.set_calendar_day(dt.date(2026, 10, 20), "workday", "office party prep")  # HR says holiday
    store.set_calendar_day(dt.date(2026, 10, 21), "holiday", "office closed")
    connect(app, [{"holidays": [{"date": "2026-10-20", "name": "Women's Day"}],
                   "leaves": [{"email": "bob@example.com", "start": "2026-10-14"}]}])
    assert app.leave_sync.sync()["ok"]
    assert store.calendar_day(dt.date(2026, 10, 20)) == ("workday", "office party prep")
    assert store.calendar_day(dt.date(2026, 10, 21)) == ("holiday", "office closed")
    assert {(x.employee_id, x.source) for x in store.leaves()} == {("A", "manual"), ("B", "hr")}

    connect(app, [{"leaves": []}])  # the HR leave was cancelled; the manual one stays
    app.leave_sync.sync()
    assert [(x.id, x.note) for x in store.leaves()] == [(manual, "client visit")]

    hr = store.leaves()  # only hand-entered rows can be removed here
    store.replace_external(DAY, DAY, [], [("B", DAY, DAY, "leave", "")])
    hr_id = next(x.id for x in store.leaves() if x.source == "hr")
    assert not store.delete_leave(hr_id) and store.delete_leave(manual)
    assert hr and not store.delete_calendar_day(dt.date(2026, 12, 1))
    assert store.delete_calendar_day(dt.date(2026, 10, 21)) and store.calendar_day(dt.date(2026, 10, 21)) is None


def test_manual_leave_counts_like_hr_leave(env):
    service, store, clock, sent = env
    store.add_leave("A", DAY, DAY, "sick")
    store.add_leave("B", DAY, DAY, "remote")
    store.set_calendar_day(DAY + dt.timedelta(days=1), "holiday", "")
    clock.t = at(11, 30)
    late = service.window(DAY).late_after + dt.timedelta(minutes=20)
    service.on_identified("B", late, 0.8, None)  # came in late anyway: not in the late message
    rep = service.report(DAY)
    assert rep.on_leave == [("A", "Alice", "Sick")] and [x[0] for x in rep.not_seen] == ["C"]
    assert "B" not in [a.employee_id for a in service.late_people(DAY)]
    assert not service.is_workday(DAY + dt.timedelta(days=1))


def test_leave_and_holidays_from_the_pages_are_logged(core):
    app, clock, _ = core
    c = client(app)
    assert c.get("/calendar").status_code == 200
    r = c.post("/leave", data={"employee_id": "C", "start": "2026-10-14", "end": "2026-10-16", "kind": "leave",
                               "note": "family", "back": "/?day=2026-10-14"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/?day=2026-10-14")
    page = c.get("/?day=2026-10-14").get_data(as_text=True)
    assert 'data-status="on_leave" data-name="carol' in page
    assert "Leave &amp; holidays" in page and 'id="form-leave"' in page
    assert app.service.report(DAY).on_leave == [("C", "Carol", "Leave")]

    bad = c.post("/leave", data={"employee_id": "C", "start": "2026-10-16", "end": "2026-10-14"}, follow_redirects=True)
    assert "ends before it starts" in bad.get_data(as_text=True)
    assert c.post("/calendar/day", data={"day": "2026-10-23", "kind": "holiday", "note": "Trip"}).status_code == 302
    page = c.get("/calendar").get_data(as_text=True)
    assert "Trip" in page and "family" in page and "Manual" in page

    leave_id = app.store.leaves()[0].id
    assert c.post(f"/leave/{leave_id}/delete").status_code == 302
    assert c.post("/calendar/day/2026-10-23/delete").status_code == 302
    assert not app.store.leaves() and app.store.calendar_day(dt.date(2026, 10, 23)) is None
    actions = [a for _, _, a, _ in app.store.audit()]
    assert {"add leave", "delete leave", "add calendar day", "delete calendar day"} <= set(actions)

    page = c.get("/employees/C").get_data(as_text=True)
    assert "Add leave" in page and "Personal page" in page


# read-only login ----------------------------------------------------------------------------------

def test_viewer_can_look_but_not_change_anything(core):
    app, clock, _ = core
    clock.t = at(10, 30)
    uid = app.service.on_unknown(at(9, 20), "crops/u.jpg", None, "B", 0.4)
    c = client(app, password="s3cret", viewer="look")
    for path in ("/", "/unknowns", "/report", "/accuracy", "/audit", "/report/summary.csv"):
        assert c.get(path, headers=VIEWER).status_code == 200, path
    page = c.get("/", headers=VIEWER).get_data(as_text=True)
    assert "Read-only" in page and 'class="editable"' not in page and ">People<" not in page
    for path in ("/employees", "/employees/A", "/settings", "/live", "/calendar", "/setup"):
        assert c.get(path, headers=VIEWER).status_code == 403, path
    posts = [f"/unknowns/{uid}/assign", f"/unknowns/{uid}/ignore", "/unknowns/delete", "/day/2026-10-14/A/set",
             "/leave", "/calendar/day", "/settings", "/employees", "/employees/A/personal-link", "/leave/sync"]
    for path in posts:
        assert c.post(path, data={"employee_id": "B", "time": "09:00"}, headers=VIEWER).status_code == 403, path
    assert app.store.unknown(uid).status == "pending" and "A" not in app.store.checkins(DAY)

    assert c.get("/", headers=basic("viewer", "wrong")).status_code == 401
    assert c.get("/employees", headers=ADMIN).status_code == 200  # the admin login is unchanged
    assert c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"}, headers=ADMIN).status_code == 302


def test_viewer_login_needs_a_password_and_its_own_name(core):
    app, _, _ = core
    c = client(app, password="s3cret")  # no viewer password: the viewer name cannot log in
    assert c.get("/", headers=basic("viewer", "")).status_code == 401
    form = {"web.public_url": "http://pc:8000", "web.viewer_username": "admin", "web.viewer_password": "x"}
    page = c.post("/settings", data=form, headers=ADMIN, follow_redirects=True).get_data(as_text=True)
    assert "different from the admin" in page and app.cfg.web.viewer_password == ""
    form["web.viewer_username"] = "boss"
    c.post("/settings", data=form, headers=ADMIN)
    assert app.cfg.web.viewer_username == "boss" and app.cfg.web.viewer_password == "x"
    assert c.get("/", headers=basic("boss", "x")).status_code == 200
    c.post("/settings", data={"web.public_url": "http://pc:8000", "web.viewer_off": "on"}, headers=ADMIN)
    assert app.cfg.web.viewer_password == "" and c.get("/", headers=basic("boss", "x")).status_code == 401


# personal page ------------------------------------------------------------------------------------

def link_of(c, employee_id):
    page = c.get(f"/employees/{employee_id}", headers=ADMIN).get_data(as_text=True)
    return re.search(r'id="personal-link" value="http://pc:8000(/p/[^"]+)"', page).group(1)


def test_personal_page_shows_only_that_person_without_the_admin_login(core):
    app, clock, _ = core
    clock.t = at(10, 30)
    app.service.on_identified("A", at(10, 15), 0.8, "crops/a.jpg")
    app.service.on_identified("B", at(8, 40), 0.8, "crops/b.jpg")
    app.store.add_leave("A", DAY + dt.timedelta(days=3), DAY + dt.timedelta(days=4), "business_trip")
    c = client(app, password="s3cret")
    link = link_of(c, "A")
    page = c.get(link).get_data(as_text=True)  # no login header
    assert "Alice" in page and "10:15" in page and "Late 15 min" in page and "Business trip" in page
    assert "Bob" not in page and "08:40" not in page and "/media/" not in page and "bob@" not in page

    _, _, emp, sig = link.split("/")
    assert c.get(f"/p/B/{sig}").status_code == 404  # Alice's signature does not open Bob's page
    assert c.get(f"/p/A/{sig[:-1]}0" if sig[-1] != "0" else f"/p/A/{sig[:-1]}1").status_code == 404
    assert c.get(f"/p/C/{personal.token(app.store, 'wrong-secret', 'C', create=True)}").status_code == 404
    assert c.get("/media/crops/b.jpg").status_code == 401 and c.get("/").status_code == 401


def test_personal_note_shows_on_today_and_a_new_link_revokes_the_old(core):
    app, clock, _ = core
    clock.t = at(10, 30)
    c = client(app, password="s3cret")
    link = link_of(c, "B")
    r = c.post(link + "/note", data={"day": DAY.isoformat(), "note": "  at the   client "})
    assert r.status_code == 302 and app.store.day_notes(DAY) == {"B": "at the client"}
    assert "at the client" in c.get("/", headers=ADMIN).get_data(as_text=True)
    assert ("bob@example.com", "personal note") in [(u, a) for _, u, a, _ in app.store.audit()]
    far = c.post(link + "/note", data={"day": "2027-01-01", "note": "x"}, follow_redirects=True)
    assert "Notes can be written" in far.get_data(as_text=True) and dt.date(2027, 1, 1) not in app.store.notes_of(
        "B", DAY, dt.date(2027, 1, 2))
    c.post(link + "/note", data={"day": DAY.isoformat(), "note": ""})
    assert app.store.day_notes(DAY) == {}

    assert c.post("/employees/B/personal-link", headers=ADMIN).status_code == 302
    assert c.get(link).status_code == 404 and c.post(link + "/note", data={"day": DAY.isoformat()}).status_code == 404
    assert c.get(link_of(c, "B")).status_code == 200


def test_review_page_has_one_at_a_time_mode(core):
    app, clock, _ = core
    clock.t = at(10, 30)
    uid = app.service.on_unknown(at(9, 20), "crops/u.jpg", None, "B", 0.4)
    page = client(app).get("/unknowns").get_data(as_text=True)
    assert 'id="focus-toggle"' in page and 'id="focus-bar"' in page
    assert f'action="/unknowns/{uid}/ignore" class="focus-only"' in page


def test_old_databases_get_the_source_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE calendar (day TEXT PRIMARY KEY, kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');"
                     "CREATE TABLE leaves (id INTEGER PRIMARY KEY, employee_id TEXT NOT NULL, start_day TEXT NOT NULL,"
                     " end_day TEXT NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');"
                     "INSERT INTO leaves (employee_id, start_day, end_day, kind) VALUES ('A', '2026-10-14', '2026-10-14', 'sick');")
    db.commit()
    db.close()
    store = Store(path)
    assert [x.source for x in store.leaves()] == ["hr"]
    store.set_calendar_day(DAY, "holiday", "")
    assert store.calendar_days() == [(DAY, "holiday", "", "manual")]
