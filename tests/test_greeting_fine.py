"""Late: the name, then one of ten fixed lines about the office's fine. Both pieces are read once and kept."""
import attendance.core.greeting as greeting
from attendance.core.greeting import LATE_FINE_VI, PART, untagged
from conftest import DAY, at
from test_greeting_credits import zerotts


def fined(core):
    app, clock, g, made = zerotts(core)
    app.cfg.greeting.late_fine = "hai mươi nghìn"
    return app, clock, g, made


def test_someone_late_hears_their_name_then_a_fine_line(core):
    app, clock, g, made = fined(core)
    clock.t = at(7, 0)
    assert g.write_if_due()
    alice = app.store.employee("A")
    assert g.sentence(alice, at(8, 30)) == "Chào Alice!"  # on time: the greeting written for them
    late = g.sentence(alice, at(10, 15))
    name, line = [part.strip() for part in late.split(PART)]
    assert name == g.name_called(alice)
    assert line in g.fine_lines(g.pronoun(alice)) and "hai mươi nghìn" in line
    assert len(g.fine_lines()) == len(LATE_FINE_VI) == 10
    assert any("chị" in l for l in g.fine_lines("chị")) and not any("{" in l for l in g.fine_lines("chị"))
    assert g.sentence(alice, at(10, 20)) == late  # the same line all day


def test_no_fine_for_a_boss_or_when_it_is_off(core):
    from types import SimpleNamespace

    app, clock, g, made = fined(core)
    clock.t = at(7, 0)
    g.write_if_due()
    boss = SimpleNamespace(id="B", not_counted=True, greet_language="vi", honorific="anh")
    assert g.fine_line(boss, DAY) == "" and g.fine_line(app.store.employee("A"), DAY)
    app.cfg.greeting.late_fine = ""
    assert g.fine_lines() == [] and g.sentence(app.store.employee("A"), at(10, 15)) == "Chào Alice!"


def test_the_fine_lines_are_read_once_and_written_late_lines_are_not_read(core):
    app, clock, g, made = fined(core)
    clock.t = at(7, 0)
    assert g.write_if_due()
    while g.render_next():
        pass
    fines = [untagged(line) for line in g.fine_lines()]
    assert all(f in made for f in fines)
    path = g.audio_path(g.voiced(g.fine_lines()[0], [], "vi", DAY), "maichi", DAY)
    assert path.parent.name == greeting.NAMES_DIR and path.is_file()
    made.clear()
    # a late arrival is played from the kept files: nothing is read
    audio, rate = g._joined(g.voiced(g.sentence(app.store.employee("A"), at(10, 15)), [app.store.employee("A")], "vi", DAY),
                            "maichi", DAY)
    assert rate == 48000 and made == []


def test_no_english_group_lines_for_a_single_english_speaker(core):
    app, clock, g, made = fined(core)
    assert "en" not in g.together_languages()
    g._note_together(DAY, [{"language": "en", "text": "Morning, everyone!"}, {"language": "vi", "text": "Chào cả nhà!"}])
    assert [l["language"] for l in g.together_lines(DAY)] == ["vi"]
