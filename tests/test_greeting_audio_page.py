"""The Greetings page plays the audio made ahead for the sentences still to come."""
import json

import pytest

from attendance.core.greeting import LINES_KEY, Greeter
from test_edge import live  # noqa: F401

WAV = b"RIFF\x04\x00\x00\x00WAVEtest"


def written(app, clock):
    a = app.store.add_employee("an@example.com", "An")
    day = clock().date()
    app.store.put(LINES_KEY, json.dumps({"day": day.isoformat(), "people": {
        a: {"on_time": ["[cheerfully] Anh An tới sớm ghê!"], "late": ["Anh An vô thôi!"], "timed": True, "note": ""}}}))
    return a, day


def test_the_audio_made_ahead_is_found_and_never_made(core):
    app, clock, _ = core
    a, day = written(app, clock)
    app.cfg.greeting.engine, app.cfg.greeting.zerotts_voice = "zerotts", "maichi"
    g = Greeter(app.cfg, app.store, clock, lambda d: clock())
    g._synthesize = lambda *args, **kw: pytest.fail("listening must not make audio")
    person = app.store.employee(a)
    path = g.audio_path(g.voiced("Anh An vô thôi!", [person], "vi", day), g.voice_for("vi"), day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(WAV)
    assert g.made_audio(a, "late", 0, day) == WAV
    with pytest.raises(ValueError, match="Not made yet"):
        g.made_audio(a, "on_time", 0, day)
    with pytest.raises(ValueError, match="no longer"):
        g.made_audio(a, "late", 1, day)
    with pytest.raises(ValueError, match="Nothing is written"):
        g.made_audio("nobody", "late", 0, day)


def test_the_server_asks_the_office_computer_for_the_audio(live, tmp_path):
    from test_edge import admin, follower
    _, portal, client = live
    _, edge = follower(live, tmp_path)
    asked = []
    edge.made_audio = lambda *args: asked.append(args) or WAV
    a = portal.store.add_employee("an@example.com", "An")
    response = client.post("/greetings/audio", data={"employee_id": a, "line": "late", "index": "0"}, headers=admin())
    assert response.status_code == 202
    url = response.json["preview_url"]
    assert client.get(url, headers=admin()).status_code == 202
    edge.follow()
    assert asked and asked[0][:3] == (a, "late", 0)
    response = client.get(url, headers=admin())
    assert response.status_code == 200 and response.data == WAV and response.mimetype == "audio/wav"
    assert portal.store.edge_jobs() == []
    assert client.post("/greetings/audio", data={"employee_id": a, "line": "sideways", "index": "0"},
                       headers=admin()).status_code == 400


def test_the_server_says_when_the_audio_is_not_made_yet(live, tmp_path):
    from test_edge import admin, follower
    _, portal, client = live
    _, edge = follower(live, tmp_path)

    def not_yet(*args):
        raise ValueError("Not made yet: the day's audio is made from 90 minutes before the camera starts.")
    edge.made_audio = not_yet
    response = client.post("/greetings/audio", data={"employee_id": "x", "line": "on_time", "index": "0"}, headers=admin())
    edge.follow()
    response = client.get(response.json["preview_url"], headers=admin())
    assert response.status_code == 502 and "Not made yet" in response.json["message"]
