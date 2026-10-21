import datetime as dt
import json

from attendance import leave_api
from conftest import DAY, at
from test_service import run_until

HR = {
    "holidays": [{"date": "2026-10-20", "name": "Women's Day"}, {"date": "bad"}],
    "workdays": [{"date": "2026-10-24T00:00:00", "name": "Make-up day"}],
    "leaves": [
        {"email": " BOB@Example.com ", "start": "2026-10-14", "end": "2026-10-15", "type": "sick", "note": "flu"},
        {"email": "carol@example.com", "start": "2026-10-14", "period": "afternoon"},  # mornings still count
        {"email": 7, "start": "2026-10-16"},
        {"start": "2026-10-16"},  # missing employee
    ],
}


def connect(app, responses):
    calls = []

    def fetch(url, token):
        calls.append((url, token))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    app.cfg.leave_api.url = "https://hr.example/api/calendar?site=hq"
    app.cfg.leave_api.token = "tok"
    app.leave_sync._fetch = fetch
    return calls


def test_parse_skips_invalid_rows_and_afternoon_leave():
    days, leaves, skipped = leave_api.parse(HR)
    assert days == [(dt.date(2026, 10, 20), "holiday", "Women's Day"), (dt.date(2026, 10, 24), "workday", "Make-up day")]
    assert leaves == [("bob@example.com", DAY, DAY + dt.timedelta(days=1), "sick", "flu")]
    assert skipped == 3


def test_sync_stores_data_and_leave_affects_attendance(core):
    app, clock, sent = core
    calls = connect(app, [HR])
    assert app.leave_sync.due()
    status = app.leave_sync.sync()
    assert status["ok"] and "3 invalid rows skipped" in status["message"]
    url, token = calls[0]
    assert token == "tok" and url.startswith("https://hr.example/api/calendar?site=hq&from=2026-10-07&to=2026-12-13")
    assert app.store.calendar_day(dt.date(2026, 10, 20)) == ("holiday", "Women's Day")
    assert not app.service.is_workday(dt.date(2026, 10, 20))
    assert app.service.is_workday(dt.date(2026, 10, 24))  # Saturday make-up day

    run_until(app.service, clock, at(10, 1))
    deadline = sent[-1]
    assert "• @Bob" not in deadline and "Bob (Sick)" in deadline and "• @Carol" in deadline


def test_sync_replaces_cancelled_leave(core):
    app, clock, _ = core
    connect(app, [HR, {"holidays": [], "workdays": [], "leaves": []}])
    app.leave_sync.sync()
    assert app.store.leaves_on(DAY) == {"B": "sick"}
    app.leave_sync.sync()
    assert app.store.leaves_on(DAY) == {}


def test_failed_sync_keeps_old_data_retries_and_warns(core):
    app, clock, sent = core
    connect(app, [HR, OSError("connection refused"), HR])
    app.leave_sync.sync()
    clock.t = at(8, 20)
    status = app.leave_sync.sync()
    assert not status["ok"] and "connection refused" in status["message"]
    assert app.store.leaves_on(DAY) == {"B": "sick"}  # previous copy still used
    assert "latest update failed" in leave_api.warning(app.cfg, app.store, clock())

    clock.t = at(8, 21)
    assert not app.leave_sync.due()
    clock.t = at(8, 23)
    assert app.leave_sync.due()  # retried two minutes after a failure
    app.leave_sync.sync()
    assert leave_api.warning(app.cfg, app.store, clock()) is None


def test_refresh_schedule_and_extra_refresh_before_the_late_time(core):
    app, clock, _ = core
    connect(app, [HR] * 5)
    app.leave_sync.sync()  # 08:00
    clock.t = at(8, 10)
    assert not app.leave_sync.due()
    clock.t = at(8, 15)
    assert app.leave_sync.due()  # refresh_minutes = 15
    clock.t = at(9, 50)
    app.leave_sync.sync()
    clock.t = at(9, 56)
    assert app.leave_sync.due()  # just before the 10:01 late list
    app.leave_sync.sync()
    clock.t = at(9, 58)
    assert not app.leave_sync.due()


def test_never_loaded_warning_appears_in_chat(core):
    app, clock, sent = core
    connect(app, [OSError("down")])
    app.leave_sync.sync()
    run_until(app.service, clock, at(10, 1))
    assert "could not be loaded from the HR system yet" in sent[-1]
    assert json.loads(app.store.get("leave_sync"))["ok"] is False


def test_disabled_without_url(core):
    app, _, _ = core
    assert not app.leave_sync.enabled and not app.leave_sync.due()
    assert leave_api.warning(app.cfg, app.store, at(9, 0)) is None
