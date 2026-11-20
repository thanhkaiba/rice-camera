"""The greeting at the door: spoken once a day per person, late or on time by the server's rules, never in the camera's way."""
import datetime as dt
import json
import re
import time

import pytest

from attendance import server
from attendance.core import greeting
from attendance.core.greeting import Greeter, called, plain, rules_for
from conftest import DAY, at
from test_edge import AUTH, HASH, PASSWORD, admin, follower, live  # noqa: F401  (live is a fixture)


def make(core, written=None, fail=False):
    app, clock, _ = core
    app.cfg.greeting.enabled = True
    said, asked = [], []

    def write(people, day):
        asked.append([p.id for p in people])
        if fail:
            raise ConnectionError("no network")
        return {p.id: {"on_time": [f"Chào {p.name}!"], "late": [f"{p.name} đến rồi!"]} for p in people} if written is None else written

    g = Greeter(app.cfg, app.store, clock, lambda d: app.service.window(d).late_from,
                speak=lambda text, language="vi": said.append(text if language == "vi" else (language, text)), write=write,
                sleep=lambda s: None)
    app.service.greeter = g
    return app, clock, g, said, asked


def test_built_in_greetings_use_anh_chi_or_the_name():
    assert plain("Minh", "anh", False) == "Chào anh Minh! Chúc một ngày làm việc vui vẻ."
    assert plain("Lan", "chi", True) == "Chị Lan đến rồi! Vào làm việc vui vẻ nhé."
    assert plain("Bob", "", False).startswith("Chào Bob!")


def test_people_are_called_by_their_email():
    from types import SimpleNamespace as P

    assert called(P(email="alan@offspring.com", name="Bùi Quang Huy")) == "Alan"
    assert called(P(email="thanh.nguyen2@x.com", name="x")) == "Thanh"
    assert called(P(email="bach@x.com", name="Hoàng Minh Bách")) == "Bách"
    assert called(P(email="chien@x.com", name="Đường Minh Chiến")) == "Chiến"
    assert called(P(email="", name="Nguyễn Văn Minh")) == "Minh"
    assert called(P(email="", name="Alvin Vu")) == "Alvin"


def test_the_first_arrival_of_the_day_is_greeted_once(core):
    app, clock, g, said, _ = make(core)
    app.store.set_greetings({"A": {"honorific": "chi"}})
    app.service.on_identified("A", at(8, 40), 0.9, None)
    app.service.on_identified("A", at(8, 45), 0.9, None)
    assert g.speak_next(timeout=0) and not g.speak_next(timeout=0)
    assert said == ["Chào chị Alice! Chúc một ngày làm việc vui vẻ."]


def test_a_late_arrival_gets_the_late_line_and_written_lines_win(core):
    app, clock, g, said, asked = make(core)
    clock.t = at(8, 30)
    assert g.write_if_due() is False  # no key: built-in greetings only
    app.cfg.greeting.key = "k"
    assert g.write_if_due() and asked == [["A", "B", "C"]]
    assert not g.write_if_due()  # everyone has theirs for today
    app.service.on_identified("B", at(10, 5), 0.9, None)
    g.speak_next(timeout=0)
    assert said == ["Bob đến rồi!"]


def test_the_servers_rules_move_the_late_time(core):
    app, clock, g, said, _ = make(core)
    person = app.store.employee("A")
    g.set_rules({"day": DAY.isoformat(), "workday": True, "people": {"A": at(9, 31).isoformat(), "B": None}})
    assert not g.is_late(person, at(9, 20)) and g.is_late(person, at(9, 31))
    assert not g.is_late(app.store.employee("B"), at(10, 0))  # on leave and came anyway
    assert not g.is_late(app.store.employee("C"), at(10, 0)) and g.is_late(app.store.employee("C"), at(10, 1))
    g.set_rules({"day": DAY.isoformat(), "workday": False, "people": {}})
    assert not g.is_late(app.store.employee("C"), at(10, 30))


def test_a_greeting_nobody_heard_in_time_is_dropped(core, monkeypatch):
    app, clock, g, said, _ = make(core)
    g.arrived("A", at(8, 40))
    real = time.monotonic
    monkeypatch.setattr(greeting.time, "monotonic", lambda: real() + greeting.STALE_S + 1)
    assert not g.speak_next(timeout=0) and said == []


def test_the_camera_never_waits_for_a_crowd_or_a_broken_speaker(core):
    app, clock, g, said, _ = make(core)
    g._speak = lambda text, language="vi": (_ for _ in ()).throw(RuntimeError("no speaker"))
    for _ in range(greeting.MAX_WAITING + 5):
        g.arrived("A", at(8, 40))  # never blocks, the extra ones are dropped
    assert g._queue.qsize() == greeting.MAX_WAITING
    assert not g.speak_next(timeout=0) and "no speaker" in g.error


def test_writing_fails_quietly_and_waits_before_trying_again(core):
    app, clock, g, said, asked = make(core, fail=True)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert not g.write_if_due() and "no network" in g.error
    assert not g.write_if_due() and len(asked) == 1
    clock.t = at(5, 0)
    g._failed_at = 0
    assert not g.write_if_due() and len(asked) == 1  # too early in the day


def test_switched_off_says_nothing(core):
    app, clock, g, said, _ = make(core)
    app.cfg.greeting.enabled = False
    app.service.on_identified("A", at(8, 40), 0.9, None)
    assert not g.speak_next(timeout=0)


def test_rules_from_base_requests(core):
    app, clock, _ = core
    app.store.put(f"base_timeoffs:{DAY}", "{}")
    rules = rules_for(app.service, DAY)
    assert rules == {"day": DAY.isoformat(), "workday": True, "people": {}, "day_notes": [], "notes": {}, "habits": {}, "pending": {}}


def test_honorifics_are_set_on_the_server_and_reach_the_mac(live, tmp_path):  # noqa: F811
    _, core, c = live
    page = c.get("/employees/honorifics", headers=admin()).get_data(as_text=True)
    assert 'name="h_A"' in page
    form = {"shown_A": "1", "h_A": "chi", "l_A": "en", "shown_nobody": "1", "h_nobody": "anh"}
    assert c.post("/employees/honorifics", data=form, headers=admin()).status_code == 302
    a = core.store.employee("A")
    assert (a.honorific, a.greet, a.greet_language) == ("chi", False, "en")  # Greet unticked
    assert c.post("/employees/honorifics", data={**form, "g_A": "on", "h_A": "ong"}, headers=admin()).status_code == 302
    assert core.store.employee("A").honorific == "chi" and not core.store.employee("A").greet  # refused as a whole
    assert c.post("/employees/honorifics", data={**form, "g_A": "on"}, headers=admin()).status_code == 302
    assert core.store.employee("A").greet
    mac, edge = follower(live, tmp_path)
    edge.on_config = lambda config: None
    edge.follow()
    assert (mac.employee("A").honorific, mac.employee("A").greet_language) == ("chi", "en")


def test_the_mac_gets_the_settings_and_the_days_rules_and_says_the_test(live, tmp_path):  # noqa: F811
    _, core, c = live
    mac, edge = follower(live, tmp_path)
    configs, rules = [], []
    edge.on_config, edge.on_rules = configs.append, rules.append
    edge.follow()
    assert "greeting" in configs[0] and rules == []  # off: no rules sent
    core.cfg.greeting.enabled = True
    page = c.get("/settings", headers=admin()).get_data(as_text=True)
    assert 'name="greeting.key"' in page and "Say a test greeting" in page
    assert c.post("/settings/greeting/test", headers=admin()).status_code == 302
    edge.on_say = lambda: {"ok": True, "message": "Xin chào"}
    edge.follow()
    assert rules[0]["day"] == DAY.isoformat() and core.store.edge_jobs() == []
    assert "said: Xin chào" in c.get("/audit", headers=admin()).get_data(as_text=True)


def test_the_heartbeat_carries_the_greeting_state(live, tmp_path):  # noqa: F811
    _, core, c = live
    mac, edge = follower(live, tmp_path)
    edge.status = lambda: {"camera_ok": True, "greeting": {"enabled": True, "ready": 3, "voice": True, "spoken": 1, "error": None}}
    edge.heartbeat()
    beat = json.loads(core.store.get("edge_heartbeat"))
    assert beat["greeting"] == {"enabled": True, "ready": 3, "voice": True, "spoken": 1, "audio": 0, "error": None}
    assert "sentences for 3 people today" in c.get("/settings", headers=admin()).get_data(as_text=True)


def test_greeting_settings_are_validated(core):
    app, _, _ = core
    from attendance.core import settings

    settings.save(app.cfg, app.store, {"greeting": {"enabled": True, "rate": 200, "style": "  vui  "}})
    assert app.cfg.greeting.rate == 200 and app.cfg.greeting.style == "vui"
    try:
        settings.save(app.cfg, app.store, {"greeting": {"rate": 1000}})
    except ValueError:
        pass
    else:
        raise AssertionError("rate 1000 accepted")


def test_server_build_has_no_greeter(tmp_path):
    application, core = server.build(tmp_path, PASSWORD, HASH, shadow=False, now=lambda: at(10, 30))
    assert core.service.greeter is None


def test_english_and_switched_off_people(core):
    app, clock, g, said, asked = make(core)
    app.store.set_greetings({"A": {"language": "en"}, "B": {"greet": False}})
    assert plain("Alvin", "anh", True, "en") == "Alvin is here! Let's have a great day."
    for emp in "AB":
        app.service.on_identified(emp, at(8, 40), 0.9, None)
    g.speak_next(timeout=0), g.speak_next(timeout=0)
    assert said == [("en", "Good morning, Alice! Have a great day.")]  # Bob is not greeted
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    g.write_if_due()
    assert asked == [["A", "C"]]


def test_a_new_language_or_address_gets_new_sentences(core):
    app, clock, g, said, asked = make(core)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    g.write_if_due()
    app.store.set_greetings({"C": {"language": "en"}})
    app.store.set_greetings({"A": {"honorific": "anh"}})
    g.write_if_due()
    assert asked[1] == ["A", "C"]
    assert g.lines(DAY)["C"]["language"] == "en"


def test_the_employee_page_sets_the_greeting(live):  # noqa: F811
    _, core, c = live
    page = c.get("/employees/A", headers=admin()).get_data(as_text=True)
    assert 'name="greet_language"' in page and 'name="greet"' in page
    form = {"name": "Alice", "email": "a@example.com", "active": "on", "greet_shown": "1", "greet_language": "en", "honorific": "chi"}
    assert c.post("/employees/A", data=form, headers=admin()).status_code == 302
    a = core.store.employee("A")
    assert (a.greet, a.greet_language, a.honorific) == (False, "en", "chi")


def test_people_arriving_together_get_one_greeting_per_language(core, monkeypatch):
    app, clock, g, said, _ = make(core)
    monkeypatch.setattr(greeting.random, "choice", lambda options: options[0])
    app.store.set_greetings({"A": {"honorific": "chi"}, "B": {"honorific": "anh"}, "C": {"language": "en"}})
    for emp in "ABC":
        app.service.on_identified(emp, at(10, 20), 0.9, None)  # late, but together: one cheerful line
    assert g.speak_next(timeout=0)
    assert said == [f"Chị Alice! {greeting.PART} Anh Bob! {greeting.PART} {greeting.TOGETHER_PLAIN_VI[0]}",
                    ("en", "Carol is here! Let's have a great day.")]  # alone in English: her own (late) line
    assert g.spoken == {DAY.isoformat(): 3}


def test_a_big_group_is_split_and_each_name_said_once(core):
    app, clock, g, said, _ = make(core)
    from types import SimpleNamespace as P

    people = [P(email=f"{n}@x.com", name="x", honorific="", greet_language="vi") for n in ("an", "binh", "chi", "dung")]
    assert "An, Binh, Chi và Dung!" in greeting.together(people, "vi")
    many = [(P(id=str(i), email=f"p{i}@x.com", name="x", honorific="", greet_language="vi", greet=True, not_counted=False), at(8, 40), time.monotonic())
            for i in range(greeting.GROUP_NAMES + 1)]
    assert len(g._plan(many)) == 2
    g.arrived("A", at(8, 40)), g.arrived("A", at(8, 40))
    g.speak_next(timeout=0)
    assert said == ["Chào Alice! Chúc một ngày làm việc vui vẻ."]


def test_people_never_counted_late_get_the_happy_greeting(core):
    app, clock, g, said, _ = make(core)
    app.store.update_employee("A", "Alice", True, exempt=True)
    app.service.on_identified("A", at(10, 45), 0.9, None)
    g.speak_next(timeout=0)
    assert said == ["Chào Alice! Chúc một ngày làm việc vui vẻ."]


def test_the_test_greeting_asks_claude_any_time(core):
    app, clock, g, said, asked = make(core)
    app.cfg.greeting.enabled = False  # off and in the evening: the test still checks the key
    clock.t = at(19, 0)
    result = g.say_test()
    assert result["ok"] and "no API key" in result["message"] and said == [greeting.TEST_TEXT] and asked == []
    app.cfg.greeting.key = "k"
    said.clear()
    result = g.say_test()
    assert result["ok"] and re.match(rf"Claude \({app.cfg.greeting.model}, \d+\.\d s\) works: Chào ", result["message"])
    assert said[0] == greeting.TEST_TEXT and len(said) == 2 and len(asked[0]) == 1
    assert g.lines(clock.t.date()) == {}  # a test writes nothing for the day


def test_the_test_greeting_says_why_claude_failed(core):
    app, clock, g, said, asked = make(core, fail=True)
    app.cfg.greeting.key = "k"
    result = g.say_test()
    assert not result["ok"] and re.search(r"failed after \d+\.\d s: ConnectionError: no network", result["message"])
    assert said == [greeting.TEST_TEXT]


def test_the_test_greeting_works_with_nobody_to_greet(core):
    app, clock, g, said, asked = make(core)
    app.cfg.greeting.key = "k"
    for p in app.store.all_employees():
        app.store.set_greetings({p.id: {"greet": False}})
    assert g.say_test()["ok"] and asked == [["greeting-test"]]


def test_occasions_near_say_which_and_how_far():
    import datetime as dt

    from attendance.core.greeting import occasions_near

    assert occasions_near(dt.date(2026, 11, 17)) == ["Teachers' Day (20/11, in 3 days)"]
    assert occasions_near(dt.date(2026, 11, 20)) == ["Teachers' Day (20/11, today)"]
    assert occasions_near(dt.date(2026, 7, 1)) == []


def test_the_weather_at_the_office():
    import datetime as dt

    from attendance.integrations import weather

    asked = []

    def fetch(url, params):
        asked.append((url, params))
        if url == weather.GEOCODE:
            return {"results": [{"latitude": 21.03, "longitude": 105.85, "name": "Hà Nội"}]}
        return {"hourly": {"time": ["2026-11-12T07:00", "2026-11-12T08:00"], "temperature_2m": [21.4, 22.6],
                           "weather_code": [3, 61]},
                "daily": {"temperature_2m_min": [20.2], "temperature_2m_max": [26.7], "precipitation_probability_max": [80]}}

    assert weather.place("21.03, 105.85", fetch) == (21.03, 105.85, "the office's area") and asked == []
    lat, lon, name = weather.place("Ha Noi", fetch)
    assert (lat, lon, name) == (21.03, 105.85, "Hà Nội")
    text = weather.forecast(lat, lon, name, dt.date(2026, 11, 12), 8, fetch)
    assert text == "Weather in Hà Nội today: light rain around 08:00, 23°C; 20-27°C over the day, 80% chance of rain."
    assert asked[-1][1]["start_date"] == "2026-11-12"


def test_claude_is_told_the_day_and_what_each_person_heard_lately(core, monkeypatch):
    app, clock, g, said, _ = make(core)
    app.cfg.greeting.location = "Hà Nội"
    monkeypatch.setattr("attendance.integrations.weather.place", lambda location: (21.0, 105.8, "Hà Nội"))
    monkeypatch.setattr("attendance.integrations.weather.forecast", lambda *a: "Weather in Hà Nội today: rain.")
    alice = app.store.employee("A")
    for days_ago, text in ((9, "Too old"), (2, "Chào chị Alice hôm kia!"), (1, "Chào chị Alice hôm qua!")):
        g._note_heard("A", clock.t.date() - __import__("datetime").timedelta(days=days_ago), text)
    system, user, ids = g.prompt([alice], clock.t.date())
    assert "Weather in Hà Nội today: rain." in user and "Today is" in user and '"recent"' in system
    people = json.loads(user.split("People:\n")[1])
    assert people[0]["recent"] == ["Chào chị Alice hôm kia!", "Chào chị Alice hôm qua!"] and ids == {"1": "A"}


def test_a_spoken_greeting_is_remembered_and_no_weather_does_not_stop_the_writing(core, monkeypatch):
    app, clock, g, said, _ = make(core)
    app.service.on_identified("A", at(8, 40), 0.9, None)
    assert g.speak_next(timeout=0)
    assert json.loads(app.store.get(greeting.HEARD_KEY))["A"] == [[DAY.isoformat(), said[0]]]
    app.cfg.greeting.location = "Nowhere"

    def broken(location):
        raise LookupError("no place called Nowhere")

    monkeypatch.setattr("attendance.integrations.weather.place", broken)
    assert "Weather" not in g.day_context(DAY) and g.day_context(DAY).startswith("Today is")


def test_long_breaks_are_told_on_the_last_and_the_first_work_day(core):
    import datetime as dt

    from attendance.core.greeting import day_notes

    app, clock, _ = core
    friday = DAY + dt.timedelta(days=2)  # DAY is a Wednesday
    app.store.set_calendar_day(friday + dt.timedelta(days=3), "holiday", "Nghỉ bù")  # Monday off: 3 days
    assert day_notes(app.service, friday) == ["Today is the last work day before 3 days off (Nghỉ bù); work starts again on 20/10."]
    assert day_notes(app.service, friday + dt.timedelta(days=4)) == ["Today is the first work day after 3 days off (Nghỉ bù)."]
    assert day_notes(app.service, DAY) == []  # a usual weekend is no long break


def test_back_from_leave_and_first_day_are_noticed(core):
    import datetime as dt

    from attendance.core.greeting import person_notes

    app, clock, _ = core
    monday, tuesday = DAY - dt.timedelta(days=2), DAY - dt.timedelta(days=1)
    app.store.add_leave("A", monday, tuesday, "annual")
    app.store.add_leave("B", tuesday, tuesday, "annual")
    app.store.record_checkin(tuesday, "B", at(8, 30, day=tuesday), "camera", 0.9, None)  # came after all: not away
    assert person_notes(app.service, DAY) == {"A": "back at work after 2 work days of leave"}
    old = DAY - dt.timedelta(days=20)
    for emp in ("A", "B"):
        app.store.record_checkin(old, emp, at(8, 30, day=old), "camera", 0.9, None)
    notes = person_notes(app.service, DAY)  # C never came, and arrivals go back 20 days: C is new
    assert notes == {"A": "back at work after 2 work days of leave", "C": "their first day at the company"}


def test_claude_is_told_what_is_special_today(core):
    app, clock, g, said, asked = make(core)
    g.set_rules({"day": DAY.isoformat(), "workday": True, "people": {},
                 "day_notes": ["Today is the first work day after 9 days off (Tết)."],
                 "notes": {"A": "back at work after 3 work days of leave"}})
    system, user, _ = g.prompt([app.store.employee("A"), app.store.employee("B")], DAY)
    people = json.loads(user.split("People:\n")[1])
    assert "first work day after 9 days off (Tết)" in user and people[0]["today"].startswith("back at work")
    assert "today" not in people[1] and "Never say or guess why" in system
    app.cfg.greeting.key = "k"
    clock.t = at(8, 0)
    assert g.write_if_due() and sorted(asked[-1]) == ["A", "B", "C"]
    g.set_rules({"day": DAY.isoformat(), "workday": True, "people": {}, "notes": {"B": "their first day at the company"}})
    assert g.write_if_due() and sorted(asked[-1]) == ["A", "B"]  # their note changed: new sentences


def test_a_face_nobody_can_name_gets_a_hello_at_most_every_two_minutes(core, monkeypatch):
    app, clock, g, said, _ = make(core)
    clock.t = at(8, 40)
    app.service.on_unknown(at(8, 40), None, None, "A", 0.4, None, 0.1)
    assert g.speak_next(timeout=0) and said[0] in greeting.HELLO_VI
    app.service.on_unknown(at(8, 41), None, None, "A", 0.4, None, 0.1)
    assert not g.speak_next(timeout=0) and len(said) == 1  # too soon after the last one
    real = time.monotonic
    monkeypatch.setattr(greeting.time, "monotonic", lambda: real() + greeting.UNKNOWN_GAP_S + 1)
    g.arrived("A", at(8, 43))
    g.unknown(at(8, 43))
    g.speak_next(timeout=0)
    assert said[-1].startswith("Chào Alice") and len(said) == 2  # a named greeting wins over the hello


def test_no_hello_outside_camera_hours_or_when_switched_off(core):
    app, clock, g, said, _ = make(core)
    g.unknown(at(14, 0))
    app.cfg.greeting.unknown = False
    g.unknown(at(8, 40))
    assert not g.speak_next(timeout=0) and said == []


def test_a_small_face_votes_only_on_a_clear_match():
    from types import SimpleNamespace as P
    from attendance.vision.tracker import vote_for

    params = P(accept_score=0.35, min_margin=0.08)
    clear, close = [("N", 0.59), ("B", 0.41)], [("N", 0.45), ("B", 0.40)]
    assert vote_for(clear, False, True, params) == "N"  # small face (size_ok, not quality_ok)
    assert vote_for(close, False, True, params) is None
    assert vote_for(clear, False, False, params) is None  # below small_face_px


def test_a_foreign_name_is_respelled_for_the_vietnamese_voice_only_when_spoken(core, caplog):
    import datetime as dt
    import logging

    app, clock, g, said, _ = make(core, written={
        "A": {"say_as": "Bờ rai ần", "on_time": ["Chào anh Brian, Brian đến sớm quá!"], "late": ["Anh Brian đến rồi!"]},
        "B": {"say_as": "Brian", "on_time": ["Good morning, Brian!"], "late": ["Brian is here!"]}})
    caplog.set_level(logging.INFO, logger="attendance.core.greeting")
    with app.store._db:
        app.store._db.execute("UPDATE employees SET email = 'brian@example.com' WHERE id = 'A'")
        app.store._db.execute("UPDATE employees SET email = 'brian.b@example.com' WHERE id = 'B'")
    app.store.set_greetings({"A": {"honorific": "anh"}, "B": {"language": "en"}})
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due()
    app.service.on_identified("A", at(8, 40), 0.9, None)
    assert g.speak_next(timeout=0)
    app.service.on_identified("B", at(8, 41), 0.9, None)
    assert g.speak_next(timeout=0)
    assert said == ["Chào anh Bờ rai ần, Bờ rai ần đến sớm quá!", ("en", "Good morning, Brian!")]
    assert any("Chào anh Brian, Brian đến sớm quá! (said as: Chào anh Bờ rai ần" in r.getMessage() for r in caplog.records)
    assert "Brian" in g.heard("A", DAY + dt.timedelta(days=1))[0]  # remembered as written


def test_habits_come_from_the_last_work_days_and_only_the_label_reaches_claude(core):
    """Often late lately, or on time many days in a row: worked out on the server, a label without any time."""
    import datetime as dt

    from attendance.core.greeting import person_habits

    app, clock, g, _, _ = make(core)
    days, d = [], DAY
    while len(days) < 6:
        d -= dt.timedelta(days=1)
        if app.service.is_workday(d):
            days.append(d)
    for n, d in enumerate(days):
        app.store.mark_notice(d, "camera_ok", at(8, 0, day=d))
        app.service.on_identified("A", at(8, 40, day=d), 0.9, None, None, None)  # Alice: on time every day
        app.service.on_identified("B", at(10, 20, day=d) if n < 4 else at(8, 50, day=d), 0.9, None, None, None)  # Bob: late on 4
    assert person_habits(app.service, DAY) == {"A": "on time 6 work days in a row", "B": "often late lately"}
    g.set_rules(rules_for(app.service, DAY))
    people = [app.store.employee(i) for i in "ABC"]
    system, user, ids = g.prompt(people, DAY)
    listed = {e["name"]: e for e in json.loads(user.split("People:\n")[1])}
    assert listed["Alice"]["habit"] == "on time 6 work days in a row" and listed["Bob"]["habit"] == "often late lately"
    assert "habit" not in listed["Carol"] and "10:20" not in user and "08:40" not in user
    assert '"habit"' in system and "never mentions the habit" in system
    # Switched off in Settings: nothing about anyone's attendance is worked out or sent.
    app.cfg.greeting.tease = False
    app.service._greeting_habits = None
    assert rules_for(app.service, DAY)["habits"] == {}
    g.set_rules(rules_for(app.service, DAY))
    assert "habit" not in g.prompt(people, DAY)[1]


def test_one_sentence_of_each_kind_is_asked_for(core):
    app, clock, g, _, _ = make(core)
    system, _, _ = g.prompt([app.store.employee("A")], DAY)
    assert greeting.VARIANTS == 1 and "write 1 different greetings" in system


def test_a_coming_late_request_still_waiting_moves_the_late_time_for_the_door_only(core):
    """Asked for 10:00-10:30, not approved yet: the door does not greet them as late; attendance still does."""
    import datetime as dt

    app, clock, _ = core
    app.store.import_base_usernames("Email,Username\nbob@example.com,bob\ncarol@example.com,carol\n")
    entry = {"kind": "Coming late", "absence": False, "shift": "10:00-10:30"}
    app.store.put(f"base_timeoffs:{DAY}", json.dumps({"bob": [{**entry, "approved": False, "approvers": ["boss"]}],
                                                       "carol": [{**entry, "approved": True}]}))
    rules = rules_for(app.service, DAY)
    assert rules["people"] == {"B": at(10, 31).isoformat(), "C": at(10, 31).isoformat()}
    assert rules["pending"] == {"B": "10:00-10:30"}  # only the one still waiting
    from attendance.integrations.base_timeoff import permissions

    assert "B" not in permissions(app.store, DAY)[0] and "C" in permissions(app.store, DAY)[0]  # the record: approved only


def test_someone_whose_request_waits_for_approval_hears_their_usual_greeting(core):
    """Inside the interval they asked for they are not late, and the door no longer says that the request
    still waits: the office found that out of place at the door."""
    app, clock, g, said, _ = make(core)
    app.store.import_base_usernames("Email,Username\nbob@example.com,bob\n")
    app.store.update_employee("B", "Bob", True)
    entry = {"kind": "Coming late", "absence": False, "shift": "10:00-10:30", "approved": False, "approvers": ["boss"]}
    app.store.put(f"base_timeoffs:{DAY}", json.dumps({"bob": [entry]}))
    g.set_rules(rules_for(app.service, DAY))
    bob = app.store.employee("B")
    assert g.sentence(bob, at(9, 50)) == plain("Bob", "", False)
    assert not g.waits_for_approval(bob, at(10, 20)) and not g.is_late(bob, at(10, 20))
    text = g.sentence(bob, at(10, 20))
    assert text == plain("Bob", "", False) and "chờ" not in text and "duyệt" not in text
    assert g.is_late(bob, at(10, 31)) and g.sentence(bob, at(10, 31)) == plain("Bob", "", True)  # after the interval: late
    # Arriving with others, they share the group's sentence like anyone else.
    plan = g._plan([(bob, at(10, 20), 0.0), (app.store.employee("A"), at(10, 20), 0.0), (app.store.employee("C"), at(10, 20), 0.0)])
    assert [sorted(p.id for p, _, _ in group) for _, _, group in plan] == [["A", "B", "C"]]


def test_what_the_door_said_is_kept_and_sent_to_the_server(core):
    """Each greeting is recorded with its kind, for the Today page; a shared one under every person in it."""
    app, clock, g, said, _ = make(core)
    told = []
    g.on_greeted = lambda ids, ts, text, kind: told.append((ids, text, kind))
    clock.t = at(9, 30)
    g.arrived("A", clock.t)
    assert g.speak_next(timeout=0)
    kept = app.store.greetings(DAY)
    assert kept["A"]["text"] == said[0] and kept["A"]["kind"] == "on_time" and told == [(["A"], said[0], "on_time")]
    clock.t = at(10, 20)
    g.arrived("B", clock.t)
    g.arrived("C", clock.t)
    assert g.speak_next(timeout=0)
    kept = app.store.greetings(DAY)
    assert kept["B"]["kind"] == kept["C"]["kind"] == "together" and kept["B"]["text"] == kept["C"]["text"] == said[1].replace(f" {greeting.PART}", "")
    from attendance.web import create_app

    page = create_app(app).test_client().get(f"/?day={DAY}").get_data(as_text=True)
    assert said[0] in page and "on-time greeting" in page and "greeted with others" in page


def test_the_server_keeps_the_greetings_the_mac_reports(live):  # noqa: F811
    _, core, c = live
    event = {"id": "g1", "kind": "greeted", "ts": at(9, 30).isoformat(), "employee_ids": ["A", "nobody"],
             "text": "Chào Alice!", "said": "on_time"}
    r = c.post("/api/edge/events", json={"events": [event, {**event, "id": "g2", "said": "shouted"}]}, headers=AUTH)
    assert r.get_json()["accepted"] == ["g1"] and [x["id"] for x in r.get_json()["rejected"]] == ["g2"]
    kept = core.store.greetings(DAY)
    assert list(kept) == ["A"] and kept["A"]["text"] == "Chào Alice!" and kept["A"]["kind"] == "on_time"
    assert "Chào Alice!" in c.get(f"/?day={DAY}", headers=admin()).get_data(as_text=True)


def test_zerotts_speed_shortens_the_audio_and_keeps_the_pitch():
    import numpy as np

    from attendance.core.greeting import quicker

    rate = 24000
    tone = np.sin(2 * np.pi * 200 * np.arange(rate * 3) / rate).astype(np.float32)
    assert quicker(tone, rate, 1.0) is tone
    fast = quicker(tone, rate, 1.2)
    assert abs(len(fast) / len(tone) - 1 / 1.2) < 0.03
    middle = fast[rate // 2:rate * 2]
    crossings = np.sum((middle[:-1] < 0) & (middle[1:] >= 0)) / (len(middle) / rate)
    assert abs(crossings - 200) < 4 and 0.9 < np.abs(middle).max() < 1.1  # still 200 Hz, no gaps or doubled pieces


def test_zerotts_speed_and_liveliness_are_settings_and_remake_the_audio(core):
    import pytest

    from attendance.core import settings

    app, clock, _ = core
    g = Greeter(app.cfg, app.store, clock, lambda d: app.service.window(d).late_from)
    before = g.audio_path("Chào anh Bình!", "maichi", DAY)
    settings.save(app.cfg, app.store, {"greeting": {"zerotts_speed": "1.15", "zerotts_liveliness": "1.0"}})
    assert (app.cfg.greeting.zerotts_speed, app.cfg.greeting.zerotts_liveliness) == (1.15, 1.0)
    assert g.audio_path("Chào anh Bình!", "maichi", DAY) != before
    for bad in ({"zerotts_speed": "2"}, {"zerotts_liveliness": "1.5"}):
        with pytest.raises(ValueError):
            settings.save(app.cfg, app.store, {"greeting": bad})

    class Model:
        sample_rate, frame_rate = 24000, 12.5

        def synthesize(self, text, voice=None, max_frames=0, audio_temperature=None):
            self.temperature = audio_temperature
            import numpy as np
            return (2 * np.sin(np.arange(24000 * 2) / 20)).astype(np.float32).reshape(1, -1)

    model = Model()
    audio = g._read_aloud(model, "Chào!", "maichi")
    assert model.temperature == 1.0 and abs(len(audio) / 48000 - 1 / 1.15) < 0.03 and abs(audio).max() <= 1.0


def test_keep_greeting_after_the_camera_hours_for_chosen_people_says_hello_once_and_records_nothing(core):
    import datetime as dt

    import pytest

    from attendance.core import settings

    app, clock, _ = core
    queued = []

    class Door:
        def arrived(self, employee_id, ts):
            queued.append(employee_id)

    app.service.greeter = Door()
    app.cfg.greeting.enabled = True
    end = app.service.window(DAY).end
    after = end + dt.timedelta(minutes=30)
    until = (end + dt.timedelta(hours=2)).strftime("%H:%M")
    assert app.cfg.greeting.until == "12:00" and not app.service.greets_after(after)  # nobody chosen: off
    settings.save(app.cfg, app.store, {"greeting": {"until": until, "until_people": "A, B,B"}})
    assert app.cfg.greeting.until_people == "A,B"
    assert app.service.greets_after(after) and not app.service.greets_after(end - dt.timedelta(minutes=1))
    assert not app.service.greets_after(end + dt.timedelta(hours=2))
    app.service.on_identified("A", end - dt.timedelta(minutes=20), 0.8, None)  # arrived in the camera hours
    queued.clear()
    for _ in range(2):
        for who in "ABC":  # C is not ticked: greeted in the camera hours only
            app.service.greet_only(who, after)
    assert queued == ["B"] and "B" not in app.store.checkins(DAY) and "C" not in app.store.checkins(DAY)
    assert not app.service.greets_after(after + dt.timedelta(minutes=1))  # A arrived, B greeted: the camera can rest
    app.service._greeted_after = set()  # the program restarted: what was said today is on disk
    app.store.add_greeting(DAY, "B", after, "Chào!", "late")
    app.service.greet_only("B", after + dt.timedelta(minutes=1))
    assert queued == ["B"]
    settings.save(app.cfg, app.store, {"greeting": {"until": "", "until_people": "B"}})
    assert not app.service.greets_after(after + dt.timedelta(minutes=2))
    with pytest.raises(ValueError):
        settings.save(app.cfg, app.store, {"greeting": {"until": "soon"}})


def test_only_two_or_three_of_the_longest_streaks_are_cheered_each_day(core):
    """Counting days for everybody bored the office: a few a day, from the longest streaks, the same all day."""
    import datetime as dt

    from attendance.core.greeting import STREAK_POOL, person_habits

    app, clock, g, _, _ = make(core)
    fresh = [app.store.add_employee(f"p{n}@example.com", f"P{n}") for n in range(10)]
    days, d = [], DAY
    while len(days) < 9:
        d -= dt.timedelta(days=1)
        if app.service.is_workday(d):
            days.append(d)
    length = {emp: 5 + k // 2 for k, emp in enumerate(fresh)}  # streaks of 5 to 9 days, two people each
    for n, d in enumerate(days):
        app.store.mark_notice(d, "camera_ok", at(8, 0, day=d))
        for emp in fresh:
            app.service.on_identified(emp, at(8, 40, day=d) if n < length[emp] else at(10, 30, day=d), 0.9, None, None, None)
    habits = person_habits(app.service, DAY)
    cheered = {e for e, h in habits.items() if h.startswith("on time")}
    assert 2 <= len(cheered) <= 3
    longest = sorted(fresh, key=lambda e: -length[e])[:STREAK_POOL]
    assert cheered <= set(longest)  # never someone with a shorter streak than the longest few
    for e in cheered:
        assert habits[e] == f"on time {length[e]} work days in a row"
    app.service._greeting_habits = None
    assert person_habits(app.service, DAY) == habits  # the same few all day


def test_the_speaker_is_kept_awake_between_greetings(core):
    """A mains speaker that sleeps without a signal: a hum from "from" on, again whenever it had no sound for
    "every" minutes, only on work days, only while the door greets, and never while someone waits."""
    import numpy as np

    app, clock, g, said, _ = make(core)
    s = app.cfg.greeting
    played = []
    play = lambda audio, rate: played.append((len(audio) / rate, float(np.abs(audio).max())))
    clock.t = at(7, 45)
    assert g.awake_due(clock.t) is None and not g.keep_awake(play)  # switched off
    s.speaker_awake, s.speaker_awake_from, s.speaker_awake_every, s.speaker_awake_level = True, "07:30", 5, 6
    clock.t = at(7, 29)
    assert not g.keep_awake(play)
    clock.t = at(7, 30)
    assert g.keep_awake(play) and played == [(greeting.AWAKE_FIRST_S, pytest.approx(0.18, abs=0.02))]  # the wake-up: longer, louder
    clock.t = at(7, 34)
    assert not g.keep_awake(play)
    clock.t = at(7, 35)
    assert g.keep_awake(play) and played[-1] == (greeting.AWAKE_S, pytest.approx(0.18, abs=0.02))  # the tone nobody hears
    g._sound_at = at(7, 38)  # a greeting was said: the speaker had its signal
    clock.t = at(7, 41)
    assert not g.keep_awake(play)
    clock.t = at(7, 43)
    g.arrived("A", clock.t)  # someone waits to be greeted: the greeting goes first
    assert not g.keep_awake(play)
    assert g.speak_next(timeout=0)
    end = app.cfg.schedule.clocks()[2]
    clock.t = at(end.hour, end.minute)  # the camera has stopped
    assert g.awake_due(clock.t) is None
    clock.t = at(end.hour - 1, 59)
    assert g.awake_due(clock.t) == "again"
    g.set_rules({"day": DAY.isoformat(), "workday": False, "people": {}})
    clock.t = at(8, 30)
    assert g.awake_due(clock.t) is None  # a day off

    def broken(audio, rate):
        raise OSError("no output device")
    g.set_rules({"day": DAY.isoformat(), "workday": True, "people": {}})
    g._sound_at = None
    assert g.keep_awake(broken) and "no output device" in g.awake_error and not g.keep_awake(broken)  # not tried again at once


def test_the_speaker_is_not_woken_twice_when_the_app_starts_again(core):
    app, clock, g, _, _ = make(core)
    s = app.cfg.greeting
    s.speaker_awake, s.speaker_awake_from, s.speaker_awake_every = True, "07:30", 5
    played = []
    clock.t = at(8, 0)
    assert g.keep_awake(lambda audio, rate: played.append(len(audio) / rate)) and played == [greeting.AWAKE_FIRST_S]
    again = Greeter(app.cfg, app.store, clock, lambda d: app.service.window(d).late_from, speak=lambda *a, **k: None,
                    write=lambda people, day: {}, sleep=lambda s: None)  # the app has started again
    clock.t = at(8, 2)
    assert again.awake_due(clock.t) is None
    clock.t = at(8, 5)
    assert again.awake_due(clock.t) == "again"  # the short hum, not the wake-up
    clock.t = at(8, 0) + dt.timedelta(days=1)
    assert again.awake_due(clock.t) == "first"  # a new day: woken again


def test_only_the_wake_up_can_be_heard():
    import numpy as np

    def loudest_hz(audio):
        spectrum = np.abs(np.fft.rfft(audio))
        return float(np.fft.rfftfreq(len(audio), 1 / greeting.AWAKE_RATE)[spectrum.argmax()])

    assert loudest_hz(greeting.awake_signal(6, first=True)) == pytest.approx(55, abs=2)
    silent = greeting.awake_signal(6)
    assert loudest_hz(silent) == pytest.approx(20, abs=1) and abs(float(silent[0])) < 1e-6 and abs(float(silent[-1])) < 1e-3
    spectrum = np.abs(np.fft.rfft(silent)) ** 2
    hz = np.fft.rfftfreq(len(silent), 1 / greeting.AWAKE_RATE)
    assert spectrum[hz > 60].sum() / spectrum.sum() < 1e-4  # nothing of it where a small speaker plays


def test_awake_settings_are_checked(core):
    app, _, _ = core
    app.cfg.greeting.speaker_awake_every = 481
    with pytest.raises(ValueError):
        app.cfg.validate()
    app.cfg.greeting.speaker_awake_every, app.cfg.greeting.speaker_awake_level = 5, 80
    with pytest.raises(ValueError):
        app.cfg.validate()
