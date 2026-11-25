"""Hearing the door speaker with the computer's microphone: the comparison, the answers it gives, the alert after
two greetings it did not hear, and that a greeting still plays when the microphone cannot be used."""
import time

import numpy as np

from attendance.core import listen, speaker
from attendance.core.listen import RATE, Listener, compare, chime, verdict


def speechlike(seconds, rate, seed):
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    syllables = np.clip(np.sin(2 * np.pi * rng.uniform(3, 5) * t + rng.uniform(0, 6)), 0, None) ** 2
    words = (rng.random(int(seconds * 3) + 1) > 0.3).repeat(rate // 3 + 1)[:n]
    return (rng.standard_normal(n) * syllables * words * 0.3).astype(np.float32)


def heard_in_room(played, rate, delay_s, seed, gain=0.05, talk=0.0):
    rng = np.random.default_rng(seed)
    x = np.interp(np.arange(int(len(played) * RATE / rate)) * (rate / RATE), np.arange(len(played)), played)
    room = rng.standard_normal(int(0.15 * RATE)) * np.exp(-np.arange(int(0.15 * RATE)) / (0.04 * RATE))
    x = np.convolve(x, room / np.abs(room).sum())[: len(x)] * gain * 8
    out = np.zeros(int((len(played) / rate + 3.0) * RATE))
    start = int(delay_s * RATE)
    out[start:start + len(x)] += x
    out += rng.standard_normal(len(out)) * 0.002
    if talk:
        out += speechlike(len(out) / RATE, RATE, seed + 99) * talk
    return out.astype(np.float32)


def test_a_played_greeting_is_found_in_the_recording_even_with_people_talking():
    played = speechlike(3.0, 24000, 1)
    found = compare(played, 24000, heard_in_room(played, 24000, 1.35, 2), offset_s=1.2)
    assert found["corr"] >= 0.5 and verdict(found["corr"]) is True
    assert abs(found["lag_s"] - 0.15) < 0.05
    noisy = compare(played, 24000, heard_in_room(played, 24000, 1.35, 3, talk=0.01), offset_s=1.2)
    assert verdict(noisy["corr"]) is True


def test_a_silent_speaker_is_not_heard_while_people_talk():
    played = speechlike(3.0, 24000, 1)
    room = heard_in_room(played, 24000, 1.35, 4, gain=0.0, talk=0.05)
    assert verdict(compare(played, 24000, room, offset_s=1.2)["corr"]) is False
    quiet = heard_in_room(played, 24000, 1.35, 5, gain=0.0)
    assert verdict(compare(played, 24000, quiet, offset_s=1.2)["corr"]) is False


def test_the_test_sound_is_heard_and_a_recording_that_missed_it_gives_no_answer():
    audio, rate = chime()
    assert 1.5 < len(audio) / rate < 3.0 and np.abs(audio).max() <= 0.31
    assert verdict(compare(audio, rate, heard_in_room(audio, rate, 0.6, 6), offset_s=0.5)["corr"]) is True
    late = compare(audio, rate, np.zeros(RATE // 2, dtype=np.float32), offset_s=-5.0)
    assert late["corr"] is None and verdict(None) is None


class FakeRec:
    def __init__(self, samples=None, error=None, began=100.0, seconds=1.0):
        self._samples, self._error, self.began, self.seconds, self.started = samples, error, began, seconds, began

    def samples(self, timeout):
        if self._error:
            raise listen.MicError(self._error)
        return self._samples, self.began

    def wait(self, timeout, until=()):
        return "recording"


def test_two_unheard_greetings_tell_the_system_space_and_a_heard_one_clears_it(tmp_path):
    played = speechlike(3.0, 24000, 1)
    lst = Listener(tmp_path)
    silent = heard_in_room(played, 24000, 1.35, 7, gain=0.0, talk=0.05)
    for _ in range(3):
        lst.judge(FakeRec(silent), played, 24000, 101.2, "greeting")
    state = {"device": "External Headphones", "devices": ["External Headphones"], "volume": 80, "muted": False}
    assert "speaker_silent" in speaker.problems(state, "", 30, heard=lst.status())
    lst.judge(FakeRec(heard_in_room(played, 24000, 1.35, 8)), played, 24000, 101.2, "greeting")
    assert lst.status()["heard"] is True and lst.status()["at_text"]
    assert "speaker_silent" not in speaker.problems(state, "", 30, heard=lst.status())
    assert speaker.problems(state, "", 30) == {}


def test_a_denied_microphone_is_told_and_greetings_stop_listening(tmp_path, monkeypatch):
    lst = Listener(tmp_path)
    monkeypatch.setattr(listen.sys, "platform", "darwin")
    monkeypatch.setattr(lst, "ready", lambda: True)
    assert lst.may_listen()
    lst.judge(FakeRec(error="denied"), np.zeros(100), 24000, 0, "test")
    assert lst.allowed is False and not lst.may_listen()
    assert "speaker_mic" in speaker.problems({"volume": 80, "muted": False}, "", 30, heard=lst.status())
    other = Listener(tmp_path)
    monkeypatch.setattr(other, "ready", lambda: True)
    other.judge(FakeRec(error="asking"), np.zeros(100), 24000, 0, "greeting")
    assert other.allowed is None and not other.may_listen() and "click OK" in other.last["message"]


def test_the_sound_plays_when_the_microphone_cannot_start(tmp_path, monkeypatch):
    lst = Listener(tmp_path)
    monkeypatch.setattr(lst, "begin", lambda seconds: (_ for _ in ()).throw(listen.MicError("no helper")))
    played = []
    out = lst.play_and_check(lambda a, r: played.append(len(a)), np.zeros(2400), 24000, "test", wait_s=1.0)
    assert played == [2400] and "no helper" in out["message"]
    assert lst.play_and_check(lambda a, r: played.append(1), np.zeros(10), 24000, "greeting", background=True) is None
    assert played == [2400, 1]


def test_the_test_listens_and_says_the_speaker_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(speaker.sys, "platform", "darwin")
    monkeypatch.setattr(speaker, "read", lambda: {"device": "External Headphones", "devices": [], "volume": 80, "muted": False})

    class Heard:
        def __init__(self, heard):
            self.heard = heard

        def play_and_check(self, play, audio, rate, kind, wait_s=0.0, background=False):
            assert kind == "test" and wait_s > 0
            play(audio, rate)
            return {"heard": self.heard, "message": "NOT heard by the microphone (match 0.05)" if not self.heard
                    else "heard by the microphone (match 0.81)"}

    plays = []
    silent = speaker.test(listener=Heard(False), play=lambda a, r: plays.append(r))
    assert not silent["ok"] and silent["heard"] is False and "switched off" in silent["message"] and plays == [24000]
    works = speaker.test(listener=Heard(True), play=lambda a, r: None)
    assert works["ok"] and "the speaker works" in works["message"]


def test_a_greeting_listens_only_when_it_is_on(core, monkeypatch):
    from test_greeting import make

    app, clock, g, said, asked = make(core)
    assert g.listener.home == app.cfg.base_dir
    calls = []
    monkeypatch.setattr(g.listener, "may_listen", lambda: True)
    monkeypatch.setattr(g.listener, "play_and_check", lambda *a, **k: calls.append((a[3], k)))
    app.cfg.greeting.speaker_listen = False
    assert not (app.cfg.greeting.speaker_listen and g.listener.may_listen())
    app.cfg.greeting.speaker_listen = True
    assert app.cfg.greeting.speaker_listen and g.listener.may_listen()
