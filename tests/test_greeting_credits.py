"""Fewer ElevenLabs credits: a fixed short hello is read once and kept from day to day, and nobody on leave or
working from home that day has audio read ahead."""
import datetime as dt
import json

import numpy as np

import attendance.core.greeting as greeting
from attendance.core.greeting import RULES_KEY, SHORT_VI, Greeter, fixed_short, untagged
from conftest import DAY, at


def zerotts(core):
    app, clock, _ = core
    cfg = app.cfg
    cfg.greeting.enabled, cfg.greeting.key, cfg.greeting.engine, cfg.greeting.unknown = True, "k", "zerotts", False
    cfg.greeting.zerotts_voice, cfg.greeting.mask_nudge = "maichi", False
    written = lambda people, day: {p.id: {"say_as": "", "on_time": [f"Chào {p.name}!"], "late": [f"Chào {p.name}!"],
                                          "short": []} for p in people}
    g = Greeter(cfg, app.store, clock, lambda d: app.service.window(d).late_from, write=written, sleep=lambda s: None)
    made = []
    g.voice_ready = lambda language="vi": True
    g._synthesize = lambda text, voice: (made.append(untagged(text)) or np.full(480, 0.1, dtype=np.float32), 48000)
    greeting.ensure_voice_models = lambda cfg, **kw: None
    return app, clock, g, made


def test_a_fixed_short_hello_is_kept_from_day_to_day():
    for line in SHORT_VI:
        said = line.format(who="chị Rose", Who="Chị Rose")
        assert fixed_short(said) and fixed_short(untagged(said))
    assert not fixed_short("[cheerfully] Chào buổi sáng chị Rose, hôm nay trời đẹp ghê!")
    assert not fixed_short("Chào anh Minh!")


def test_the_short_hellos_are_read_once_not_every_morning(core):
    app, clock, g, made = zerotts(core)
    clock.t = at(7, 0)
    assert g.write_if_due()
    while g.render_next():
        pass
    shorts = [untagged(g.short_line(app.store.employee(i), DAY)) for i in "ABC"]
    assert all(s in made for s in shorts)
    path = g.audio_path(g.voiced(g.short_line(app.store.employee("A"), DAY), [app.store.employee("A")], "vi", DAY),
                        "maichi", DAY)
    assert path.parent.name == greeting.NAMES_DIR and path.is_file()
    made.clear()
    clock.t = at(7, 0) + dt.timedelta(days=1)
    tomorrow = clock.t.date()
    if not app.service.is_workday(tomorrow):
        return
    g.write_if_due()
    while g.render_next():
        pass
    assert not any(s in made for s in shorts)  # tomorrow: only the new greetings are read
    assert any(m.startswith("Chào") for m in made)


def test_nobody_away_today_gets_audio_read_ahead(core):
    app, clock, g, made = zerotts(core)
    clock.t = at(7, 0)
    app.store.put(RULES_KEY, json.dumps({"day": DAY.isoformat(), "workday": True, "people": {}, "away": ["B"]}))
    assert g.write_if_due()
    while g.render_next():
        pass
    assert "Chào Alice!" in made and "Chào Carol!" in made and "Chào Bob!" not in made
    assert not any("Bob" in m for m in made)  # nor his short hello, nor his name for groups
    # Bob comes in after all: his greeting is read at the door
    audio, rate = g._audio(g.voiced("Chào Bob!", [app.store.employee("B")], "vi", DAY), "maichi", DAY)
    assert rate == 48000 and "Chào Bob!" in made


def test_away_from_an_older_server_and_from_the_leave_list(core):
    app, clock, g, made = zerotts(core)
    clock.t = at(7, 0)
    app.store.put(RULES_KEY, json.dumps({"day": DAY.isoformat(), "workday": True,
                                         "people": {"A": None, "C": "2026-01-01T09:31:00+07:00"}}))
    assert g.away(DAY) == {"A"}  # never late today: on leave or away; a later late time is not away
    app.store.add_leave("C", DAY, DAY, "remote")
    assert g.away(DAY) == {"A", "C"}


def test_the_server_tells_the_mac_who_is_away(core):
    app, clock, _ = core
    app.store.add_leave("B", DAY, DAY, "leave")
    rules = greeting.rules_for(app.service, DAY)
    assert "B" in rules["away"] and "A" not in rules["away"]
