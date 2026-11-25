"""The short hello: one of three fixed lines in Vietnamese, Claude's (kept from day to day) in English; said when
the door is busy. And call names set in Settings."""
import datetime as dt
import json
import time

from attendance.core import greeting
from attendance.core.greeting import SHORT_KEY, SHORT_VI, called, untagged
from conftest import DAY, at
from test_greeting import make


def written_with_short(people, day):
    return {p.id: {"on_time": [f"Chào {p.name}!"], "late": [f"{p.name} đến rồi!"], "short": [f"Hi, {p.name}!"]}
            for p in people}


def test_the_prompt_asks_for_a_short_hello_and_the_new_rules():
    assert '"short"' in greeting.SYSTEM and "short" in greeting.SCHEMA["properties"]["people"]["items"]["required"]
    assert "Never assume what anyone will do today" in greeting.SYSTEM
    assert "never laugh before the punchline" in greeting.EMOTIONS


def test_a_vietnamese_short_hello_is_one_of_three_and_the_same_every_day(core):
    app, clock, g, said, asked = make(core)
    app.store.set_greetings({"A": {"honorific": "chi"}})
    alice = app.store.employee("A")
    line = g.short_line(alice, DAY)
    assert line in [s.format(who="chị Alice", Who="Chị Alice") for s in SHORT_VI]
    assert g.short_line(alice, DAY + dt.timedelta(days=1)) == line
    picked = {g.short_line(app.store.employee(i), DAY).split("]")[0] for i in "ABC"}
    assert picked <= {"[cheerfully", "[happily", "[excited"}


def test_a_short_hello_is_said_at_a_busy_door(core):
    app, clock, g, said, asked = make(core)
    g._write = written_with_short
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due()
    app.service.on_identified("A", at(8, 40), 0.9, None)
    g.speak_next(timeout=0)
    assert said == ["Chào Alice!"]  # nobody else around: the usual greeting
    app.service.on_identified("B", at(8, 40, 5), 0.9, None)
    g.speak_next(timeout=0)  # right after Alice's: Bob hears his short one
    assert said[-1] == untagged(g.short_line(app.store.employee("B"), DAY))
    g._last_said = time.monotonic() - 60
    app.service.on_identified("C", at(8, 50), 0.9, None)
    g.speak_next(timeout=0)
    assert said[-1] == "Chào Carol!"


def test_an_english_short_hello_is_claudes_and_the_one_of_an_earlier_day_wins(core):
    app, clock, g, said, asked = make(core)
    app.store.set_greetings({"A": {"language": "en"}, "B": {"language": "en"}})
    app.cfg.greeting.key = "k"
    g.save_short(app.store.employee("A"), "Hey, Alice!")
    g._write = written_with_short
    clock.t = at(8, 30)
    assert g.write_if_due()
    assert g.short_line(app.store.employee("A"), DAY) == "Hey, Alice!"
    assert g.short_line(app.store.employee("B"), DAY) == "Hi, Bob!"
    assert json.loads(app.store.get(SHORT_KEY))["B"]["text"] == "Hi, Bob!"


def test_busy_seconds_zero_uses_the_short_hello_only_while_others_wait(core):
    app, clock, g, said, asked = make(core)
    g._write = written_with_short
    app.cfg.greeting.key = "k"
    app.cfg.greeting.busy_seconds = 0
    clock.t = at(8, 30)
    g.write_if_due()
    app.service.on_identified("A", at(8, 40), 0.9, None)
    g.speak_next(timeout=0)
    app.service.on_identified("B", at(8, 40, 2), 0.9, None)
    g.speak_next(timeout=0)
    assert said == ["Chào Alice!", "Chào Bob!"]
    assert not g.busy()


def test_call_names_from_settings_win(core):
    app, clock, g, said, asked = make(core)
    alice = app.store.employee("A")
    assert called(alice) == "Alice"
    app.cfg.greeting.call_names = "alice@example.com = Alís\nBob = Bốp"
    assert called(alice) == "Alís" and called(app.store.employee("B")) == "Bốp"
    app.cfg.greeting.call_names = "alice = Ali"
    assert called(alice) == "Ali"
    app.cfg.greeting.call_names = ""
    assert called(alice) == "Alice"
