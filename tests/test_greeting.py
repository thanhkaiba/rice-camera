"""The greeting at the door: spoken once a day per person, late or on time by the server's rules, never in the camera's way."""
import json
import re
import time

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
    assert rules == {"day": DAY.isoformat(), "workday": True, "people": {}, "day_notes": [], "notes": {}}


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
    assert beat["greeting"] == {"enabled": True, "ready": 3, "voice": True, "spoken": 1, "error": None}
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
    assert said == ["Chào chị Alice và anh Bob! Chúc cả nhà một ngày làm việc vui vẻ.",
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

    assert weather.place("21.03, 105.85", fetch) == (21.03, 105.85, "21.03, 105.85") and asked == []
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
