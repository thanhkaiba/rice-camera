"""A conversational brief must not trigger paid rewrites of existing greetings."""
import json

from attendance.core import greeting
from conftest import DAY, at
from test_greeting import make


def test_natural_brief_reaches_generation_with_optional_trends(core):
    app, _, g, _, _ = make(core)
    app.cfg.greeting.trends = "a harmless office phrase"
    g._want_together = True
    system, _, ids = g.prompt([app.store.employee("A")], DAY)
    assert ids == {"1": "A"}
    assert "Everyday conversation comes first" in system
    assert "No teasing, mock" in system and "witty, cheerful personality" in system
    assert "Silently read each draft aloud" in system
    assert "ceiling, not a target" in system
    assert "one thought for the whole group" in system
    assert "Every sentence has at least one laughing tag" not in greeting.ACT_SYSTEM
    assert "Use zero to two tags" in greeting.ACT_SYSTEM


def test_existing_daily_greetings_survive_restart_without_rewriting(core):
    app, clock, g, _, asked = make(core)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due()
    saved = app.store.get(greeting.LINES_KEY)
    assert asked == [["A", "B", "C"]]
    _, _, restarted, _, new_requests = make(core)
    assert not restarted.write_if_due()
    assert new_requests == []
    assert app.store.get(greeting.LINES_KEY) == saved
    assert restarted._entry(app.store.employee("A"), DAY)


def test_previous_delivery_cache_is_kept_and_new_lines_can_have_no_laugh(core):
    app, _, g, _, _ = make(core)
    app.cfg.greeting.key = "k"
    app.cfg.greeting.elevenlabs_tags = "cheerfully, laughs"
    old = "Chào Alice!"
    cached = "[cheerfully] Chào Alice! [laughs]"
    app.store.put(greeting.ACTED_KEY, json.dumps({
        "day": DAY.isoformat(), "tags": "cheerfully, laughs", "v": 3, "lines": {old: cached},
    }))
    calls = []

    def act(sentences, tags):
        calls.append(list(sentences))
        return ["[cheerfully] " + text for text in sentences]

    g._act = act
    assert g.acted_line(old, DAY) == cached
    assert calls == []
    assert g.acted_line("Chào Bob!", DAY) == "[cheerfully] Chào Bob!"
    assert calls == [["Chào Bob!"]]


def test_api_requests_one_main_sentence_and_reuses_it_for_every_arrival(core, monkeypatch):
    import sys
    from types import SimpleNamespace as NS

    app, clock, g, _, _ = make(core)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"people": [
            {"id": "1", "greeting": "Hello Alice!", "short": "Hi Alice!", "say_as": "Alice", "dated": False}
        ]}))])

    monkeypatch.setitem(sys.modules, "anthropic", NS(Anthropic=lambda **kwargs: NS(beta=NS(messages=NS(create=create)))))
    monkeypatch.setattr(g, "fine_intros", lambda day: ["Already written"])
    monkeypatch.setattr(g, "_want_more_together", lambda day: False)
    written = g._ask_claude([app.store.employee("A")], DAY)
    fields = calls[0]["output_config"]["format"]["schema"]["properties"]["people"]["items"]["properties"]
    assert "greeting" in fields and "late" not in fields and "on_time" not in fields
    assert written["A"]["on_time"] == written["A"]["late"] == ["Hello Alice!"]
    g._write = lambda people, day: written
    assert g.write_if_due()
    g.set_rules({"day": DAY.isoformat(), "workday": True, "people": {"A": at(10, 31).isoformat()}})
    alice = app.store.employee("A")
    assert [g.sentence(alice, t) for t in (at(8, 40), at(10, 20), at(10, 31))] == ["Hello Alice!"] * 3


def test_excused_arrivals_never_use_cached_punctuality_praise(core):
    app, clock, g, _, _ = make(core, written={
        p: {"on_time": ["Perfectly on time!"], "late": ["A little late!"]} for p in "ABC"
    })
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due()
    g.set_rules({"day": DAY.isoformat(), "workday": True,
                 "people": {"B": at(10, 31).isoformat(), "C": at(10, 31).isoformat()},
                 "pending": {"B": "10:00-10:30"}})
    for pid in "BC":
        person = app.store.employee(pid)
        assert g.sentence(person, at(10, 15)) == g.short_line(person, DAY)
        assert g.sentence(person, at(10, 30)) == g.short_line(person, DAY)
        assert g.sentence(person, at(10, 31)) == "A little late!"
        assert g.sentence(person, at(8, 40)) == "Perfectly on time!"
