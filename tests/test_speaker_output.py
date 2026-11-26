"""Two speakers on the office MacBook: the external one (the Edifier, which switches itself off) and the computer's
own. A check every morning at 07:00 chooses, the Settings page switches by hand. Nothing here plays or switches
anything on the test computer: the outputs and the microphone are replaced."""
import contextlib
import datetime as dt
import json
import time

import pytest

from attendance.core import greeting, outputs
from attendance.core.greeting import Greeter
from conftest import DAY, at
from test_edge import admin, follower, live  # noqa: F401  (live is a fixture)
from test_greeting import make


class FakeOutputs:
    SUPPORTED = True

    def __init__(self, broken=()):
        self.used, self.volumes, self.ensured, self.moments, self.broken = [], [], [], [], set(broken)

    def use(self, kind, volume=None):
        self.used.append(kind)
        self.volumes.append(volume)
        if kind in self.broken:
            return {"ok": False, "message": f"{outputs.NAMES[kind]} is not connected"}
        return {"ok": True, "device": kind, "volume": volume, "message": f"playing on {kind} at volume {volume}%"}

    def ensure(self, kind):
        self.ensured.append(kind)

    @contextlib.contextmanager
    def on(self, kind):
        self.moments.append(kind)
        if kind in self.broken:
            raise OSError(f"{outputs.NAMES[kind]} is not connected")
        yield {"id": 1, "name": kind}


class FakeMicrophone:
    def __init__(self, *answers):
        self.answers, self.played = list(answers), 0

    def play_and_check(self, play, audio, rate, kind, wait_s=0.0, background=False):
        self.played += 1
        heard = self.answers.pop(0)
        corr = {True: 0.8, False: 0.2, None: 0.4, "no mic": None}[heard]
        at = time.time() - (3600 if heard == "stale" else 0)
        if heard == "stale":  # the microphone did not start: its last answer, from long ago
            return {"heard": True, "corr": 0.9, "at": at, "message": "old answer"}
        return {"heard": None if heard == "no mic" else heard, "corr": corr, "at": at, "message": f"answer {heard}"}

    def status(self):
        return None


def setup(core, *answers, broken=()):
    app, clock, g, said, _ = make(core)
    g._outputs, g.listener = FakeOutputs(broken), FakeMicrophone(*answers)
    return app, clock, g


def test_the_morning_check_keeps_the_external_speaker_when_the_microphone_hears_it(core):
    app, clock, g = setup(core, True)
    clock.t = at(6, 59)
    assert not g.check_output()  # not before 07:00
    clock.t = at(7, 0)
    assert g.check_output()
    assert g._outputs.used == ["external"] and g.listener.played == 1
    assert g.output == "external" and g.output_note["heard"] is True and "07:00 check" in g.output_note["why"]
    assert g.loudness() == app.cfg.greeting.loudness
    clock.t = at(7, 30)
    assert not g.check_output()  # once a day
    assert json.loads(app.store.get(outputs.KEY))["output"] == "external"


def test_a_silent_external_speaker_sends_the_greetings_to_the_computer_louder(core):
    app, clock, g = setup(core, None, False)  # not sure, then not heard: the test is played twice
    app.cfg.greeting.loudness = 1.2
    app.cfg.greeting.speaker_awake, app.cfg.greeting.speaker_awake_every = True, 10
    clock.t = at(7, 0)
    assert g.check_output()
    assert g._outputs.used == ["external", "builtin"] and g.listener.played == 2
    assert g.output == "builtin" and "did not hear the external speaker" in g.output_note["why"]
    assert g.loudness() == outputs.BUILTIN_LOUDNESS  # louder on the computer's own speaker
    g._sound_at = None
    clock.t = at(7, 45)
    assert g.awake_due(clock.t) == "again"  # the external speaker still kept on, never the audible wake-up hum
    played = []
    assert g.keep_awake(lambda audio, rate: played.append(len(audio) / rate))
    assert played == [greeting.AWAKE_S] and g._outputs.moments == ["external"]  # on the external speaker, for a moment
    again = Greeter(app.cfg, app.store, clock, lambda d: app.service.window(d).late_from, speak=lambda *a, **k: None,
                    write=lambda people, day: {}, sleep=lambda s: None)  # the app started again: still the computer's
    assert again.output == "builtin" and not again.output_due(clock.t)
    assert again.output_due(at(7, 0) + dt.timedelta(days=1))  # checked again the next morning


def test_switched_back_by_hand_after_the_speaker_button_was_pressed(core):
    app, clock, g = setup(core, False, False, True)
    app.cfg.greeting.speaker_awake, app.cfg.greeting.speaker_awake_every = True, 10
    clock.t = at(7, 0)
    g.check_output()
    assert g.output == "builtin"
    clock.t = at(8, 40)
    result = g.use_output("external", "admin")
    assert result["ok"] and result["heard"] is True and g.output == "external"
    assert g._outputs.volumes == [95, 100, 95]  # each speaker at its own volume, at every switch
    assert "chosen on the Settings page by admin" in g.output_note["why"] and g._outputs.used[-1] == "external"
    assert not g.output_due(clock.t)  # the morning check is not done again today
    clock.t = at(8, 50)
    assert g.awake_due(clock.t) == "first"  # kept awake again: the wake-up hum not played today comes first
    assert not g.use_output("loudspeaker")["ok"]


def test_a_switch_that_is_not_heard_says_so(core):
    _, clock, g = setup(core, False)
    clock.t = at(9, 0)
    result = g.use_output("external")
    assert not result["ok"] and "is the speaker switched on" in result["message"] and g.output == "external"


def test_a_microphone_that_cannot_be_used_changes_nothing(core):
    _, clock, g = setup(core, "no mic")
    clock.t = at(7, 0)
    assert g.check_output()
    assert g.output == "external" and "could not tell" in g.output_note["why"] and g.listener.played == 1


def test_an_unplugged_external_speaker_goes_to_the_computer_at_once(core):
    _, clock, g = setup(core, broken=("external",))
    clock.t = at(7, 0)
    assert g.check_output()
    assert g.output == "builtin" and g.listener.played == 0 and "not connected" in g.output_note["why"]


def test_every_greeting_goes_to_the_speaker_chosen(core):
    _, clock, g = setup(core, False, False)
    clock.t = at(7, 0)
    g.check_output()
    g._outputs.ensured.clear()
    g.uses_zerotts = lambda language: False
    g.voice_ready = lambda language: False  # nothing is said here: the output is chosen before anything else
    with pytest.raises(RuntimeError):
        g._say("Chào anh!", "en")
    assert g._outputs.ensured == ["builtin"]


def test_the_settings_page_switches_the_speaker_on_the_office_computer(live, tmp_path):
    _, core, c = live
    _, edge = follower(live, tmp_path)
    asked = []
    edge.on_output = lambda kind: asked.append(kind) or {"ok": True, "message": f"playing on {kind}"}
    assert c.post("/settings/speaker/output", data={"output": "builtin"}, headers=admin()).status_code == 302
    edge.follow()
    assert asked == ["builtin"] and core.store.edge_jobs() == []
    page = c.get("/audit", headers=admin()).get_data(as_text=True)
    assert "speaker in use" in page and "ok: playing on builtin" in page
    assert c.post("/settings/speaker/output", data={"output": "nope"}, headers=admin()).status_code == 302
    assert core.store.edge_jobs() == []
    settings = c.get("/settings", headers=admin()).get_data(as_text=True)
    assert "Use the external speaker" in settings and "Use the computer" in settings


def test_the_heartbeat_tells_which_speaker_is_in_use():
    from attendance.web.edge_api import _speaker_status

    seen = _speaker_status({"device": "MacBook Pro Speakers", "volume": 100,
                            "output": {"output": "builtin", "why": "07:00 check: not heard", "at": "2026-11-27T07:00",
                                       "heard": False, "name": "x"}})
    assert seen["output"] == {"output": "builtin", "why": "07:00 check: not heard", "at": "2026-11-27T07:00", "heard": False}
    assert _speaker_status({"device": "x", "output": {"output": "<b>"}})["output"]["output"] is None


def test_each_speaker_is_set_to_its_own_volume_from_the_settings(core):
    app, clock, g = setup(core, False, False, True)
    app.cfg.greeting.speaker_volume_external, app.cfg.greeting.speaker_volume_builtin = 80, 90
    clock.t = at(7, 0)
    g.check_output()
    assert g._outputs.used == ["external", "builtin"] and g._outputs.volumes == [80, 90]
    assert g.output_status()["volume"] == 90
    clock.t = at(8, 40)
    assert "at volume 80%" in g.use_output("external")["message"]
    app.cfg.greeting.speaker_volume_external = 101
    with pytest.raises(ValueError):
        app.cfg.validate()


def test_the_speaker_volumes_are_on_the_settings_page_and_saved(core):
    from attendance.core import settings
    from attendance.web import create_app

    app, _, _ = core
    page = create_app(app).test_client().get("/settings").get_data(as_text=True)
    assert 'name="greeting.speaker_volume_external"' in page and 'name="greeting.speaker_volume_builtin"' in page
    assert "speaker_volume_external" in settings.EDITABLE["greeting"] and "speaker_volume_builtin" in settings.EDITABLE["greeting"]


def test_an_old_microphone_answer_is_never_taken_for_the_morning_check(core):
    _, clock, g = setup(core, "stale")
    clock.t = at(7, 0)
    assert g.check_output()
    assert g.output == "external" and "could not tell" in g.output_note["why"] and g.output_note["heard"] is None


def test_the_keep_awake_tone_on_an_unplugged_external_speaker_is_an_error_not_a_crash(core):
    app, clock, g = setup(core, broken=("external",))
    app.cfg.greeting.speaker_awake, app.cfg.greeting.speaker_awake_every = True, 10
    clock.t = at(7, 0)
    g.check_output()
    assert g.output == "builtin"
    g._sound_at = None
    assert g.keep_awake(lambda audio, rate: None) and "not connected" in g.awake_error


def test_a_speaker_volume_below_the_volume_alert_is_refused(core):
    app, _, _ = core
    app.cfg.greeting.speaker_min_volume, app.cfg.greeting.speaker_volume_builtin = 30, 25
    with pytest.raises(ValueError):
        app.cfg.validate()
    app.cfg.greeting.speaker_min_volume = 0
    app.cfg.validate()
