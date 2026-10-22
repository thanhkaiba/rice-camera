import datetime as dt

import pytest

from attendance.evidence import build
from attendance.service import minutes_late
from attendance.web import personal
from conftest import DAY, at
from test_web import client


@pytest.mark.parametrize("seconds,is_late", [(0, False), (59.999999, False), (60, True), (119.999999, True)])
@pytest.mark.parametrize("source", ["camera", "manual"])
def test_entire_configured_minute_is_on_time(core, seconds, is_late, source):
    app, clock, sent = core
    app.cfg.schedule.late_after = "09:00"
    clock.t = at(9, 30)
    arrival = at(9, 0) + dt.timedelta(seconds=seconds)
    if source == "camera":
        app.service.on_identified("A", arrival, .8, None)
    else:
        app.service.set_arrival(DAY, "A", arrival.timetz().replace(tzinfo=None))
    report = app.service.report(DAY)
    assert [a.employee_id for a in report.late] == (["A"] if is_late else [])
    assert [a.employee_id for a in report.on_time] == ([] if is_late else ["A"])
    assert bool(app.service.late_text(DAY)) == is_late
    # Only the camera's arrivals are posted as they happen; one the admin enters is not.
    assert bool([m for m in sent if m.startswith("🟠")]) == (is_late and source == "camera")
    if is_late:
        assert minutes_late(report.late[0], report.window) == 1
        assert source == "manual" or "1 min late" in sent[-1]
    [person] = app.service.period_report(DAY, DAY).people
    assert (person.on_time, person.late, person.late_minutes) == ((0, 1, 1) if is_late else (1, 0, 0))


def test_deadline_notice_and_period_totals_wait_until_next_minute(core):
    app, clock, sent = core
    app.cfg.schedule.late_after = "09:00"
    clock.t = at(9, 0, 59).replace(microsecond=999999)
    app.service.tick()
    assert not app.store.notice_sent(DAY, "deadline")
    assert not app.service.period_report(DAY, DAY).days
    assert not [m for m in sent if m.startswith("⏰")]
    app.service.on_identified("A", clock.t, .8, None)
    clock.t = at(9, 1)
    app.service.tick()
    [notice] = [m for m in sent if m.startswith("⏰")]
    assert "09:01" in notice and "Alice" not in notice
    assert app.service.period_report(DAY, DAY).days


def test_correction_to_configured_minute_counts_as_on_time(core):
    app, clock, sent = core
    app.cfg.schedule.late_after = "09:00"
    clock.t = at(9, 30)
    app.service.tick()
    app.service.on_identified("A", at(9, 1), .8, None)
    app.service.set_arrival(DAY, "A", dt.time(9, 0, 59))
    assert [a.employee_id for a in app.service.report(DAY).on_time] == ["A"]
    assert not sent[-1].startswith("✅ Update:")  # admin changes are not posted to Chat


@pytest.mark.parametrize("minute,second,status,minutes", [(0, 59, "On time", None), (1, 0, "Late", 1)])
def test_personal_page_and_export_use_the_same_boundary(core, minute, second, status, minutes):
    app, clock, _ = core
    app.cfg.schedule.late_after = "09:00"
    clock.t = at(9, 30)
    app.service.on_identified("A", at(9, minute, second), .8, None)
    evidence = build(app.service, DAY, "A")
    assert (evidence.status, evidence.minutes_late) == (status, minutes)
    assert evidence.summary()["rule"]["late_from"] == "09:01"
    c = client(app)
    sig = personal.token(app.store, c.application.secret_key, "A", create=True)
    page = c.get(f"/p/A/{sig}").get_data(as_text=True)
    assert (">Late 1 min<" if minutes else ">On time<") in page
