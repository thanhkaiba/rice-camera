"""The short hello: written with the others, kept from day to day, said when the door is busy."""
import datetime as dt
import json
import time

from attendance.core import greeting
from attendance.core.greeting import SHORT_KEY
from conftest import DAY, at
from test_greeting import make


def written_with_short(people, day):
    return {p.id: {"on_time": [f"Chào {p.name}!"], "late": [f"{p.name} đến rồi!"], "short": [f"A, {p.name}!"]}
            for p in people}


def test_the_prompt_asks_for_a_short_hello_and_the_new_rules():
    assert '"short"' in greeting.SYSTEM and "short" in greeting.SCHEMA["properties"]["people"]["items"]["required"]
    assert "Never assume what anyone will do today" in greeting.SYSTEM
    assert "never laugh before the punchline" in greeting.EMOTIONS


def test_a_short_hello_is_written_kept_and_said_at_a_busy_door(core):
    app, clock, g, said, asked = make(core)
    g._write = written_with_short
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due()
    alice = app.store.employee("A")
    assert g.short_line(alice, DAY) == "A, Alice!"
    assert json.loads(app.store.get(SHORT_KEY))["A"]["text"] == "A, Alice!"
    app.service.on_identified("A", at(8, 40), 0.9, None)
    g.speak_next(timeout=0)
    assert said == ["Chào Alice!"]  # nobody else around: the usual greeting
    app.service.on_identified("B", at(8, 40, 5), 0.9, None)
    g.speak_next(timeout=0)  # right after Alice's: Bob hears his short one
    assert said[-1] == "A, Bob!"
    g._last_said = time.monotonic() - 60
    app.service.on_identified("C", at(8, 50), 0.9, None)
    g.speak_next(timeout=0)
    assert said[-1] == "Chào Carol!"


def test_the_short_hello_of_an_earlier_day_wins_over_a_new_one(core):
    app, clock, g, said, asked = make(core)
    app.cfg.greeting.key = "k"
    g.save_short(app.store.employee("A"), "Ê, Alice!")
    g._write = written_with_short
    clock.t = at(8, 30)
    assert g.write_if_due()
    assert g.short_line(app.store.employee("A"), DAY) == "Ê, Alice!"
    assert g.short_line(app.store.employee("B"), DAY) == "A, Bob!"


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
