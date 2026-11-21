"""Maintenance mode (camera off, nothing reported, the day not counted) and greetings written only on work days."""
import datetime as dt

from attendance.core import settings
from attendance.web import create_app
from conftest import DAY, at
from test_greeting import make

SATURDAY = dt.date(2026, 10, 17)


def test_greetings_are_written_and_voiced_on_work_days_only(core):
    app, clock, g, _, asked = make(core)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30, day=SATURDAY)
    assert not g.prepares(SATURDAY) and g.write_if_due() is False and asked == []
    assert g.render_next() is False
    app.cfg.greeting.days_off = True  # Settings → Greeting: days off too
    assert g.prepares(SATURDAY) and g.write_if_due() and asked == [["A", "B", "C"]]


def test_the_servers_rules_decide_a_holiday(core):
    from attendance.core.greeting import RULES_KEY
    import json

    app, clock, g, _, asked = make(core)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    app.store.put(RULES_KEY, json.dumps({"day": DAY.isoformat(), "workday": False, "people": {}}))
    assert not g.prepares(DAY) and g.write_if_due() is False and asked == []


def test_maintenance_mode_writes_no_greeting_and_does_not_count_the_day(core):
    app, clock, g, _, asked = make(core)
    service = app.service
    app.cfg.greeting.key = "k"
    clock.t = at(7, 30)
    app.cfg.schedule.maintenance = True
    assert not g.prepares(DAY) and g.write_if_due() is False and asked == []
    assert not service.note_maintenance(at(7, 30))  # before the camera hours: the day still counts
    assert service.is_workday(DAY)
    service.tick(True, "")
    clock.t = at(8, 10)
    service.tick(True, "")  # inside the camera hours: the day is not counted
    assert service.day_info(DAY) == (False, "Maintenance mode")
    assert service.in_window(at(8, 10))  # once it is off, the camera works again
    assert not service.report(DAY).workday and service.not_seen_people(DAY) == [] and service.late_people(DAY) == []
    app.cfg.schedule.maintenance = False
    clock.t = at(11, 30)
    service.tick(True, "")
    assert service.day_info(DAY) == (False, "Maintenance mode") and not service.store.notice_sent(DAY, "summary")
    assert service.is_workday(DAY + dt.timedelta(days=1))


def test_maintenance_mode_is_switched_by_its_button_and_shown_on_every_page(core):
    app, clock, _, _, _ = make(core)
    clock.t = at(9, 0)
    client = create_app(app).test_client()
    assert "maint-dialog" not in client.get("/").get_data(as_text=True)
    client.post("/maintenance", data={"on": "1"})
    assert app.cfg.schedule.maintenance and not app.service.is_workday(DAY)
    for page in ("/", "/settings"):
        html = client.get(page).get_data(as_text=True)
        assert 'id="maint-dialog"' in html and "Turn off maintenance mode" in html and 'class="logo-tile lg"' in html
    # the Settings form, which has no box for it, never switches it off
    client.post("/settings", data={"schedule.window_start": "08:00", "schedule.late_after": "10:00",
                                   "schedule.window_end": "11:00", "schedule.work_days": ["0", "1", "2", "3", "4"]})
    assert app.cfg.schedule.maintenance
    client.post("/maintenance", data={"on": "0"})
    assert not app.cfg.schedule.maintenance
    assert "maint-dialog" not in client.get("/").get_data(as_text=True)
    assert app.service.day_info(DAY) == (False, "Maintenance mode")  # the day it touched stays uncounted


def test_maintenance_mode_reaches_the_office_computer(core):
    from attendance.web.edge_api import MAC_SECTIONS

    app, _, _ = core
    assert "schedule" in MAC_SECTIONS and "maintenance" in settings.current(app.cfg)["schedule"]
