"""Nobody on leave or working from home gets sentences written (Claude) or read ahead (ElevenLabs), nor is shown
as still to come; what they have not heard yet is kept, with its audio, for a later day."""
import datetime as dt
import json

from attendance.core.greeting import LINES_KEY, RULES_KEY, called, plain
from conftest import DAY, at
from test_greeting import make
from test_greeting_overhaul import YESTERDAY, entry


def away(app, day, *ids):
    app.store.put(RULES_KEY, json.dumps({"day": day.isoformat(), "workday": True, "people": {}, "away": list(ids)}))


def test_nobody_away_today_gets_sentences_written(core):
    app, clock, g, said, asked = make(core)
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    away(app, DAY, "B")
    assert g.write_if_due() and asked == [["A", "C"]]
    assert not g.write_if_due() and len(asked) == 1  # nor later in the day
    assert set(g.lines(DAY)) == {"A", "C"} and set(g.upcoming(DAY)["people"]) == {"A", "C"}
    bob = app.store.employee("B")
    app.service.on_identified("B", at(8, 40), 0.9, None)  # he comes in after all: a plain hello
    g.speak_next(timeout=0)
    assert said == [plain(called(bob), bob.honorific, False)]


def test_what_someone_away_has_not_heard_is_kept_for_a_later_day(core):
    app, clock, g, said, asked = make(core)
    app.store.put(LINES_KEY, json.dumps({"day": YESTERDAY.isoformat(), "model": "m", "people": {
        "B": entry(app, "B", ["Ơ, anh Bob!"], ["Bob tới rồi!"])}}))
    away(app, DAY, "B")
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due() and asked == [["A", "C"]]  # Bob keeps his: not written again
    assert g.lines(DAY)["B"]["on_time"] == ["Ơ, anh Bob!"] and "B" not in g.upcoming(DAY)["people"]
    tomorrow = DAY + dt.timedelta(days=1)
    assert g.carry_over(tomorrow) == 1 and g.lines(tomorrow)["B"]["on_time"] == ["Ơ, anh Bob!"]  # back: heard then


def test_the_greeting_tab_is_a_list_of_groups(core):
    from attendance.web import create_app

    app, _, _ = core
    page = create_app(app).test_client().get("/settings").get_data(as_text=True)
    for group in ("door", "sentences", "voice", "speaker", "new-voice", "advanced"):
        assert f'id="g-greeting-{group}"' in page
    section = page[page.index('id="greeting"'):page.index('id="camera"')]
    assert section.count("<details") == section.count("</details>") and section.count("<div") == section.count("</div>")
    for name in ("greeting.enabled", "greeting.key", "greeting.engine", "greeting.speaker_volume_builtin",
                 "greeting.speaker_awake", "greeting.zerotts_voices_dir", "greeting.until_people_shown"):
        assert f'name="{name}"' in page
