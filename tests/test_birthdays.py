import datetime as dt
import json

import pytest

from attendance.core import greeting
from attendance.integrations import base_account
from attendance.store.schema import birthday_shown, normalize_birthday

ANSWER = {"code": 1, "users": [
    {"username": "@Minh", "dob": {"day": "14", "month": "3", "year": "1990"}, "phone": "0900000000", "token": "x"},
    {"username": "lan", "dob": {"day": 29, "month": 2, "year": 1992}},
    {"username": "nodate", "dob": {"day": "0", "month": "0", "year": "0"}},
    {"username": "bad", "dob": {"day": "31", "month": "2"}},
    {"username": "", "dob": {"day": 1, "month": 1}}, "junk"]}


def test_birthday_as_typed_and_shown():
    assert normalize_birthday("14/3") == "03-14" and normalize_birthday(" 14-03-1990 ") == "03-14"
    assert normalize_birthday("29.2.") == "02-29" and normalize_birthday("") == ""
    assert birthday_shown("03-14") == "14/3" and birthday_shown("") == ""
    for wrong in ("31/2", "3", "abc", "14/13"):
        with pytest.raises(ValueError):
            normalize_birthday(wrong)


def test_base_answer_keeps_only_name_and_day_month():
    assert base_account.parse(ANSWER) == {"minh": "03-14", "lan": "02-29"}
    with pytest.raises(ValueError):
        base_account.parse({"code": 0, "message": "access_token_v2_invalid_3"})


def test_sync_fills_empty_keeps_typed_and_follows_base(core):
    app, clock, _ = core
    store = app.store
    a, b = store.add_employee("minh@example.com", "Minh"), store.add_employee("lan@example.com", "Lan")
    store.update_employee(a, "Minh", True, base_username="minh")
    store.update_employee(b, "Lan", True, base_username="lan", birthday="1/1")  # typed on the People page
    app.cfg.base_account_key = "token"
    assert base_account.due(app.cfg, store, clock())
    result = base_account.sync(app.cfg, store, clock(), post=lambda form: ANSWER)
    assert result["ok"] and store.employee(a).birthday == "03-14" and store.employee(b).birthday == "01-01"
    assert "0900000000" not in store.get(base_account.STATUS_KEY) and "1990" not in store.get(base_account.STATUS_KEY)
    assert not base_account.due(app.cfg, store, clock())
    moved = json.loads(json.dumps(ANSWER))
    moved["users"][0]["dob"]["day"] = "15"
    base_account.sync(app.cfg, store, clock(), post=lambda form: moved)
    assert store.employee(a).birthday == "03-15"  # Base gave it, Base may correct it

    def down(form):
        raise OSError("token=secret in the address")
    failed = base_account.sync(app.cfg, store, clock(), post=down)
    assert not failed["ok"] and "secret" not in failed["message"] and store.employee(a).birthday == "03-15"
    app.cfg.base_account_key = ""
    assert not base_account.due(app.cfg, store, clock())


class Service:
    def __init__(self, app, off=()):
        self.cfg, self.store, self.off = app.cfg, app.store, set(off)

    def is_workday(self, day):
        return day.weekday() < 5 and day not in self.off


def test_birthday_notes_today_saturday_on_friday_sunday_on_monday(core):
    app, _, _ = core
    store = app.store
    today, sunday, saturday, thursday = (store.add_employee(f"{n}@example.com", n) for n in ("a", "b", "c", "d"))
    monday, friday = dt.date(2026, 11, 23), dt.date(2026, 11, 20)
    store.set_birthday(today, "11-23")
    store.set_birthday(sunday, "11-22")
    store.set_birthday(saturday, "11-21")
    store.set_birthday(thursday, "11-19")  # a work day: wished then or not at all
    assert greeting.birthday_notes(Service(app), monday) == {
        today: "their birthday is today", sunday: "their birthday was yesterday, Sunday: wished today, a day late"}
    assert greeting.birthday_notes(Service(app), friday) == {
        saturday: "their birthday is tomorrow, Saturday: wished today, a day early"}
    assert greeting.birthday_notes(Service(app), dt.date(2026, 11, 24)) == {}  # not seen on the day: not carried on
    app.cfg.greeting.birthdays = False
    assert greeting.birthday_notes(Service(app), monday) == {}


def test_wished_once_until_the_next_birthday(core):
    app, _, _ = core
    emp = app.store.add_employee("a@example.com", "a")
    app.store.set_birthday(emp, "11-23")
    service, day = Service(app), dt.date(2026, 11, 23)
    greeting.note_birthday_wished(service, [emp, "someone-else"], day)
    assert emp in greeting.birthday_notes(service, day)  # the same day: their sentences stay the wish
    app.store.set_birthday(emp, "11-24")  # the date is corrected the day after: not wished a second time
    assert greeting.birthday_notes(service, dt.date(2026, 11, 24)) == {}
    app.store.set_birthday(emp, "11-23")
    assert emp in greeting.birthday_notes(service, dt.date(2027, 11, 23))  # next year again


def test_29_february_is_wished_on_the_28th_in_other_years(core):
    app, _, _ = core
    leap = app.store.add_employee("leap@example.com", "leap")
    app.store.set_birthday(leap, "02-29")
    assert "29 February" in greeting.birthday_notes(Service(app), dt.date(2030, 2, 28))[leap]
    assert greeting.birthday_notes(Service(app), dt.date(2028, 2, 29))[leap] == "their birthday is today"


def test_people_page_saves_and_shows_the_birthday(core):
    from attendance.web import create_app
    app, _, _ = core
    emp = app.store.add_employee("minh@example.com", "Minh")
    client = create_app(app).test_client()
    page = client.get(f"/employees/{emp}").get_data(as_text=True)
    assert 'name="birthday"' in page
    settings_page = client.get("/settings").get_data(as_text=True)
    assert 'name="base_account_key"' in settings_page and 'name="greeting.birthdays"' in settings_page
    token = __import__("re").search(r'name="csrf_token" value="([^"]+)"', page)
    data = {"name": "Minh", "active": "on", "birthday": "14/3", **({"csrf_token": token.group(1)} if token else {})}
    client.post(f"/employees/{emp}", data=data)
    assert app.store.employee(emp).birthday == "03-14"
    assert 'value="14/3"' in client.get(f"/employees/{emp}").get_data(as_text=True)
