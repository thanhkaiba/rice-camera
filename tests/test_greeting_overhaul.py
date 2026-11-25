"""18/11 greeting changes: unheard sentences kept for later days, Claude's group lines, "about", office facts,
what the others already got, and a first day only for someone new."""
import datetime as dt
import json

from attendance.core import greeting
from attendance.core.greeting import CARRY_DAYS, HEARD_KEY, LINES_KEY, PART, called, person_notes
from conftest import DAY, at
from test_greeting import make
from test_web import client

YESTERDAY = DAY - dt.timedelta(days=1)


def entry(app, emp, on_time, late, dated=False, written=None, note=""):
    p = app.store.employee(emp)
    return {"say_as": "", "on_time": on_time, "late": late, "dated": dated, "language": p.greet_language,
            "honorific": p.honorific, "called": called(p), "note": note, "emotions": "",
            "written": (written or YESTERDAY).isoformat()}


def test_unheard_undated_sentences_are_kept_for_today_and_only_the_rest_are_written(core):
    app, clock, g, said, asked = make(core)
    app.store.put(LINES_KEY, json.dumps({"day": YESTERDAY.isoformat(), "model": "m", "people": {
        "A": entry(app, "A", ["Ơ kìa chị Alice!"], ["Vô nhanh nhanh nha!"]),
        "B": entry(app, "B", ["Thứ Ba vui ghê!"], ["Bob tới rồi!"], dated=True),  # about yesterday: written again
        "C": entry(app, "C", ["Carol heard this"], ["Carol late"]),
    }}))
    app.store.put(HEARD_KEY, json.dumps({"C": [[YESTERDAY.isoformat(), "Carol heard this"]]}))  # only one left: again
    assert g.carry_over(DAY) == 1
    assert g.lines(DAY)["A"]["on_time"] == ["Ơ kìa chị Alice!"] and set(g.lines(DAY)) == {"A"}
    assert g.carry_over(DAY) == 0  # once a day
    app.cfg.greeting.key = "k"
    clock.t = at(8, 30)
    assert g.write_if_due() and asked == [["B", "C"]]
    assert g.lines(DAY)["A"]["on_time"] == ["Ơ kìa chị Alice!"]


def test_old_or_noted_sentences_are_not_kept(core):
    app, _, g, _, _ = make(core)
    old = DAY - dt.timedelta(days=CARRY_DAYS + 1)
    app.store.put(LINES_KEY, json.dumps({"day": YESTERDAY.isoformat(), "people": {
        "A": entry(app, "A", ["a"], ["b"], written=old),
        "B": entry(app, "B", ["c"], ["d"], note="back at work after 1 work day of leave"),
        "C": {"on_time": ["e"], "late": ["f"]},  # written before "dated" existed: not kept
    }}))
    assert g.carry_over(DAY) == 0 and g.lines(DAY) == {}


def test_kept_sentences_keep_their_audio(core, monkeypatch):
    app, _, g, _, _ = make(core)
    app.cfg.greeting.engine = "zerotts"
    monkeypatch.setattr(g, "voice_ready", lambda language="vi": True)
    monkeypatch.setattr(g, "voice_for", lambda language: "doan")
    app.store.put(LINES_KEY, json.dumps({"day": YESTERDAY.isoformat(), "people": {
        "A": entry(app, "A", ["Ơ kìa chị Alice!"], ["Vô nhanh nhanh nha!"])}}))
    person = app.store.employee("A")
    old = g.audio_path(g.voiced("Ơ kìa chị Alice!", [person], "vi", YESTERDAY), "doan", YESTERDAY)
    old.parent.mkdir(parents=True)
    old.write_bytes(b"RIFF")
    g.carry_over(DAY)
    new = g.audio_path(g.voiced("Ơ kìa chị Alice!", [person], "vi", DAY), "doan", DAY)
    assert new.read_bytes() == b"RIFF" and not old.exists()


def test_a_group_hears_their_names_then_one_of_claudes_lines(core, monkeypatch):
    app, _, g, said, _ = make(core)
    g.together_languages = lambda: {"vi", "en"}  # an office with English speakers
    monkeypatch.setattr(greeting.random, "choice", lambda options: options[0])
    app.store.set_greetings({"A": {"honorific": "chi"}, "B": {"honorific": "anh"}})
    g._note_together(DAY, [{"language": "vi", "text": "[gasps] Ui giời ơi, cả hội tới cùng lúc luôn!"},
                           {"language": "vi", "text": "ơ kìa, {names} đi cùng nhau luôn hả!"},  # old form: dropped
                           {"language": "vi", "text": "Trời ơi, đi chung vậy luôn!"},
                           {"language": "en", "text": "Look who's here!"}])
    assert len(g.together_lines(DAY)) == 3
    people = [app.store.employee("A"), app.store.employee("B")]
    assert g.together(people, "vi", DAY) == f"Chị Alice! {PART} Anh Bob! {PART} [gasps] Ui giời ơi, cả hội tới cùng lúc luôn!"
    assert g.together(people, "vi", DAY).endswith("Trời ơi, đi chung vậy luôn!")  # the one nobody heard yet
    assert g.together(people, "en", DAY) == f"Alice! {PART} Bob! {PART} Look who's here!"
    assert g.together(people, "vi", YESTERDAY).endswith(greeting.TOGETHER_PLAIN_VI[0])  # none written: a plain one


def test_unheard_group_lines_are_kept_for_the_next_days_and_topped_up(core):
    app, _, g, _, _ = make(core)
    g.together_languages = lambda: {"vi", "en"}  # an office with English speakers
    g._note_together(YESTERDAY, [{"language": "vi", "text": f"Câu {n}!"} for n in range(5)]
                     + [{"language": "en", "text": "Hi all!"}, {"language": "en", "text": "Hey team!"}])
    g.together([app.store.employee("A"), app.store.employee("B")], "vi", YESTERDAY)  # one heard yesterday
    kept = g.together_lines(DAY)
    assert len([line for line in kept if line["language"] == "vi"]) == 4 and not any(line.get("used") for line in kept)
    assert not g._want_more_together(DAY)
    g.together([app.store.employee("A"), app.store.employee("B")], "en", DAY)
    assert g._want_more_together(DAY)  # one English line left unheard: Claude writes more with the next people


def test_a_group_greeting_is_joined_from_names_kept_for_good_and_the_line(core, monkeypatch):
    import numpy as np

    app, _, g, _, _ = make(core)
    app.cfg.greeting.engine = "zerotts"
    monkeypatch.setattr(g, "voice_ready", lambda language="vi": True)
    monkeypatch.setattr(g, "voice_for", lambda language: "doan")
    made = []

    def synthesize(text, voice, ahead=False):
        made.append(text)
        return np.full(2400, 0.3, dtype=np.float32), 24000

    monkeypatch.setattr(g, "_synthesize", synthesize)
    g._note_together(DAY, [{"language": "vi", "text": "Cả hội tới rồi!"}])
    app.store.put(LINES_KEY, json.dumps({"day": DAY.isoformat(), "people": {
        e: {**entry(app, e, ["x"], ["y"], written=DAY), "timed": True} for e in "AB"}}))
    wanted = g._wanted_group_audio([app.store.employee("A"), app.store.employee("B")], DAY)
    assert ("Alice!", "doan", True) in wanted and ("Cả hội tới rồi!", "doan") in wanted
    for item in wanted:
        g._audio(item[0], item[1], DAY, keep=len(item) > 2)
    assert g.audio_path("Alice!", "doan", DAY, keep=True).parent.name == greeting.NAMES_DIR
    made.clear()
    audio, rate = g._joined(f"Alice! {PART} Bob! {PART} Cả hội tới rồi!", "doan", DAY)
    assert made == [] and rate == 24000  # nothing read at the door: every piece was made ahead
    assert len(audio) > 3 * 2400 * 0.9 + 24000 * (greeting.NAME_GAP_S + greeting.LINE_GAP_S) * 0.9


def test_bosses_are_never_greeted_about_the_time(core):
    app, clock, g, _, _ = make(core)
    app.store._exec("UPDATE employees SET exempt = 1 WHERE id = 'A'")
    g._want_together = False
    _, user, _ = g.prompt([app.store.employee("A"), app.store.employee("B")], DAY)
    people = json.loads(user.split("People:\n")[1].split("\n\n")[0])
    assert people[0].get("not_timed") is True and "not_timed" not in people[1]
    assert "not_timed" in greeting.SYSTEM
    app.store.put(LINES_KEY, json.dumps({"day": DAY.isoformat(), "people": {
        "A": {**entry(app, "A", ["Ơ kìa chị Alice!"], ["Chúc chị một ngày vui!"], written=DAY), "timed": False}}}))
    seen = {g.sentence(app.store.employee("A"), at(8, 0)) for _ in range(60)}
    assert seen == {"Ơ kìa chị Alice!", "Chúc chị một ngày vui!"}  # both kinds, on any arrival
    app.store._exec("UPDATE employees SET exempt = 0 WHERE id = 'A'")
    assert g._entry(app.store.employee("A"), DAY) == {}  # no longer exempt: written again


def test_claude_is_told_about_the_person_the_office_and_the_others_lines(core):
    app, _, g, _, _ = make(core)
    app.store.update_employee("A", "Alice", True, about="chạy bộ mỗi sáng")
    app.cfg.greeting.office = "Không có máy in"
    g._taken = ["Ơ kìa anh Bob, tên lửa riêng hả!"]
    g._want_together = True
    system, user, _ = g.prompt([app.store.employee("A")], DAY)
    assert "chạy bộ mỗi sáng" in user and "already_written_today" in user and "tên lửa riêng" in user
    assert "Không có máy in" in system and '"together"' in system and "Never invent events" in system


def test_fun_facts_are_saved_from_the_person_page(core):
    app, _, _ = core
    response = client(app).post("/employees/A", data={"name": "Alice", "active": "on", "about": "  team Design,  hay nói ổn áp "})
    assert response.status_code in (302, 303)
    assert app.store.employee("A").about == "team Design, hay nói ổn áp"
    client(app).post("/employees/A", data={"name": "Alice", "active": "on"})  # a form without the field keeps it
    assert app.store.employee("A").about == "team Design, hay nói ổn áp"


def test_someone_the_camera_never_recognised_is_not_new(core):
    app, _, _ = core
    old = DAY - dt.timedelta(days=20)
    for emp in ("A", "B"):
        app.store.record_checkin(old, emp, at(8, 30, day=old), "camera", 0.9, None)
    assert person_notes(app.service, DAY) == {"C": "their first day at the company"}  # C's photo is new
    app.store._exec("UPDATE embeddings SET created_at = ? WHERE employee_id = 'C'", (at(8, 0, day=old).isoformat(),))
    assert person_notes(app.service, DAY) == {}  # C has been here for weeks: the camera just never knew the face
