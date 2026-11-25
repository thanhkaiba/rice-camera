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
