"""Speech boundaries are replaced so these checks never play through the test computer's speakers."""
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from attendance.core import greeting, settings
from attendance.web import create_app


@pytest.fixture
def speech(core, monkeypatch):
    app, _, _ = core
    monkeypatch.setattr(greeting, "ensure_voice_models", lambda cfg: None)
    g = app.greeter
    app.cfg.greeting.engine = "zerotts"
    root = app.cfg.path("models/ZeroTTS")
    for name in ("config.json", "tokenizer.json", "null_voice_emb.npy", "onnx/text_encoder.onnx",
                 "onnx/prefix_step.onnx", "onnx/local_frame_decode.onnx",
                 "onnx/codec/codec_browser_onnx_meta.json",
                 "onnx/codec/moss_audio_tokenizer_decode_full.onnx",
                 "onnx/codec/moss_audio_tokenizer_decode_step.onnx",
                 "onnx/codec/moss_audio_tokenizer_decode_shared.data", "voices/maichi/voice.npz"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    calls = []
    audio = np.ones((1, 4800), dtype=np.float32)

    class Model:
        sample_rate = 48000
        frame_rate = 12.5

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append(("load", path, kwargs))
            return cls()

        def add_voices(self, path):
            calls.append(("voices", path))

        def synthesize(self, text, **kwargs):
            calls.append(("synthesize", text, kwargs))
            return audio

    monkeypatch.setitem(sys.modules, "zerotts", SimpleNamespace(ZeroTTS=Model))
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        play=lambda data, rate, **kwargs: calls.append(("play", data.copy(), rate, kwargs))))
    return app, g, calls


def test_vietnamese_uses_the_local_model_and_reuses_it(speech):
    app, g, calls = speech
    g._say("Xin chào")
    app.cfg.greeting.zerotts_voice = "giahuy"
    path = app.cfg.path("models/ZeroTTS/voices/giahuy/voice.npz")
    path.parent.mkdir(parents=True)
    path.touch()
    g._say("Chào buổi sáng")
    loads = [c for c in calls if c[0] == "load"]
    assert len(loads) == 1
    assert loads[0][1] == app.cfg.path("models/ZeroTTS")
    assert loads[0][2] == {"local_files_only": True, "intra_op_num_threads": 2}
    generated = [c for c in calls if c[0] == "synthesize"]
    assert generated[0][1:2] == ("Xin chào",)
    assert generated[0][2]["voice"] == "maichi"
    assert generated[1][2]["voice"] == "giahuy"
    played = [c for c in calls if c[0] == "play"]
    assert len(played) == 2 and played[0][1].shape == (4800,)
    assert played[0][2:] == (48000, {"blocking": True})


def test_english_keeps_the_macos_voice(speech, monkeypatch):
    _, g, calls = speech
    monkeypatch.setattr(greeting.shutil, "which", lambda _: "/usr/bin/say")
    monkeypatch.setattr(greeting.subprocess, "run", lambda args, **kw:
                        calls.append(("say", args)) or SimpleNamespace(stdout="Samantha  en_US"))
    g._say("Hello", "en")
    assert calls[-1] == ("say", ["say", "-v", "Samantha", "-r", "180", "Hello"])
    assert not any(c[0] == "load" for c in calls)


def test_auto_synthesizes_with_an_installed_voice_name(speech):
    app, g, calls = speech
    app.cfg.greeting.zerotts_voice = 'auto'
    g._say('Xin chào')
    generated = [c for c in calls if c[0] == 'synthesize']
    assert generated[0][2]['voice'] == 'maichi'
    assert g.voice_ready()


def test_custom_voice_directory_and_readiness(speech):
    app, g, calls = speech
    app.cfg.greeting.zerotts_voice = "office"
    app.cfg.greeting.zerotts_voices_dir = "data/voices"
    assert not g.voice_ready()
    voice = app.cfg.path("data/voices/office/voice.npz")
    voice.parent.mkdir(parents=True)
    voice.touch()
    assert g.voice_ready()
    g._say("Xin chào")
    assert ("voices", app.cfg.path("data/voices")) in calls
    assert next(c for c in calls if c[0] == "synthesize")[2]["voice"] == "office"
    app.cfg.path("models/ZeroTTS/onnx/text_encoder.onnx").unlink()
    assert not g.voice_ready()
    with pytest.raises(RuntimeError, match="ZeroTTS"):
        g._say("Xin chào")


def test_synthesis_errors_reach_the_existing_test_result(speech, monkeypatch):
    app, g, _ = speech
    app.cfg.greeting.key = ""
    def broken(*args, **kwargs):
        raise RuntimeError("speaker disconnected")
    monkeypatch.setattr(sys.modules["sounddevice"], "play", broken)
    result = g.say_test()
    assert not result["ok"] and "speaker disconnected" in result["message"]


def test_test_and_arrival_playback_do_not_overlap(speech, monkeypatch):
    _, g, _ = speech
    playing, release, overlap = threading.Event(), threading.Event(), threading.Event()
    def play(*args, **kwargs):
        if playing.is_set():
            overlap.set()
        playing.set()
        assert release.wait(3)
        playing.clear()
    monkeypatch.setattr(sys.modules["sounddevice"], "play", play)
    first = threading.Thread(target=g._say, args=("First",))
    second = threading.Thread(target=g._say, args=("Second",))
    first.start()
    try:
        assert playing.wait(2)
        second.start()
        assert not overlap.wait(0.1)
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive() and not overlap.is_set()


def test_voice_settings_are_editable_and_validated(core):
    app, _, _ = core
    settings.apply(app.cfg, {"greeting": {"engine": "zerotts", "zerotts_voice": "office",
                                         "zerotts_voices_dir": "data/voices"}})
    assert app.cfg.greeting.engine == "zerotts"
    assert app.cfg.greeting.zerotts_voice == "office"
    for fields in ({"engine": "unknown"}, {"zerotts_voice": "../secret"}, {"zerotts_voice": ""}):
        with pytest.raises(ValueError):
            settings.apply(app.cfg, {"greeting": fields})


def test_first_speech_prepares_models_before_readiness(speech, monkeypatch):
    app, g, calls = speech
    path = app.cfg.path('models/ZeroTTS/onnx/text_encoder.onnx')
    path.unlink()
    prepared = []

    def prepare(cfg):
        prepared.append(cfg)
        path.touch()
    monkeypatch.setattr(greeting, 'ensure_voice_models', prepare)
    g._say('Xin chào')
    assert prepared and prepared[0] is app.cfg
    assert any(c[0] == 'play' for c in calls)


def test_greeting_settings_keep_upstream_unknown_and_custom_voice_fields(core):
    app, _, _ = core
    fields = {'unknown': True, 'engine': 'zerotts', 'zerotts_voice': 'office',
              'zerotts_voices_dir': 'data/voices'}
    settings.save(app.cfg, app.store, {'greeting': fields})
    saved = settings.current(app.cfg)['greeting']
    assert all(saved[name] == value for name, value in fields.items())
    page = create_app(app).test_client().get('/settings')
    assert page.status_code == 200
    assert all(f'name="greeting.{name}"' in page.text for name in fields)


def test_text_preview_uses_unsaved_voice_and_returns_wav_without_speaker(speech):
    import io
    import soundfile as sf
    app, g, calls = speech
    folder = app.cfg.path('data/voices/office')
    folder.mkdir(parents=True)
    (folder / 'voice.npz').touch()
    app.cfg.greeting.zerotts_voices_dir = 'data/voices'
    data = g.render_text('Một câu chuyện khác, không phải lời chào.', 'office')
    audio, rate = sf.read(io.BytesIO(data))
    assert rate == 48000 and audio.size == 4800
    assert ('voices', app.cfg.path('data/voices')) in calls
    generated = [c for c in calls if c[0] == 'synthesize']
    assert generated[0][1] == 'Một câu chuyện khác, không phải lời chào.'
    assert generated[0][2]['voice'] == 'office'
    assert app.cfg.greeting.zerotts_voice == 'maichi'
    assert not any(c[0] == 'play' for c in calls)
    assert g.render_text('Đọc tiếp.', 'auto').startswith(b'RIFF')
    with pytest.raises(ValueError, match='not installed'):
        g.render_text('Đọc thử.', 'missing')


def test_english_can_be_read_by_zerotts_with_the_vietnamese_voice(tmp_path):
    from types import SimpleNamespace

    from attendance.core.config import Config
    from attendance.core.greeting import Greeter

    cfg = Config()
    cfg.base_dir = tmp_path
    cfg.greeting.zerotts_voice = "maichi"
    g = Greeter(cfg, SimpleNamespace(get=lambda key: None, put=lambda key, value: None), lambda: None, lambda day: None)
    assert not g.uses_zerotts("vi") and not g.uses_zerotts("en") and g.voice_for("en") == "Samantha"
    cfg.greeting.engine_en = "zerotts"
    assert g.uses_zerotts("en") and not g.uses_zerotts("vi")
    assert g.voice_for("en") == "maichi" and g.voice_for("vi") == "Linh"
    cfg.greeting.engine_en = "siri"
    import pytest
    with pytest.raises(ValueError, match="engine_en"):
        cfg.validate()


def test_zerotts_reads_the_days_sentences_ahead_and_the_door_plays_the_file(core, tmp_path):
    import numpy as np
    from attendance.core.greeting import Greeter
    from conftest import at

    app, clock, _ = core
    cfg = app.cfg
    cfg.greeting.enabled, cfg.greeting.key, cfg.greeting.engine, cfg.greeting.unknown = True, "k", "zerotts", False
    cfg.greeting.zerotts_voice = "maichi"
    written = lambda people, day: {p.id: {"say_as": "", "on_time": [f"Chào {p.name} một!", f"Chào {p.name} hai!"],
                                          "late": [f"{p.name} đến rồi!"]} for p in people}
    g = Greeter(cfg, app.store, clock, lambda d: app.service.window(d).late_from, write=written, sleep=lambda s: None)
    made = []
    g.voice_ready = lambda language="vi": True
    g._synthesize = lambda text, voice: (made.append((text, voice)) or np.full(480, 0.1, dtype=np.float32), 48000)
    import attendance.core.greeting as greeting
    greeting.ensure_voice_models = lambda cfg, **kw: None
    clock.t = at(7, 0)
    assert not g.render_next()  # nothing written yet
    assert g.write_if_due()
    app.service.on_identified("C", at(7, 1), 0.9, None)  # already in: no audio needed for them
    while g.render_next():
        pass
    assert [text for text, _ in made] == ["Chào Alice một!", "Chào Bob một!", "Alice đến rồi!", "Bob đến rồi!"]
    assert g.audio_count(at(7, 0).date()) == 4 and g.status()["audio"] == 4
    person = app.store.employee("A")
    assert {g.sentence(person, at(8, 30)) for _ in range(20)} == {"Chào Alice một!"}  # the variant that has a file
    audio, rate = g._audio("Chào Alice một!", "maichi", at(7, 0).date())
    assert rate == 48000 and len(audio) == 480 and len(made) == 4  # played from the file, not made again
    g._audio("Chào cả nhà!", "maichi", at(7, 0).date())  # a sentence nobody expected is made on the spot, and kept
    assert len(made) == 5 and g.audio_count(at(7, 0).date()) == 5
    g.arrived("A", at(8, 30))
    assert not g.render_next()  # someone is waiting to be greeted: the speaker comes first


def _elevenlabs(app, g):
    app.cfg.greeting.engine = "elevenlabs"
    app.cfg.greeting.elevenlabs_key, app.cfg.greeting.elevenlabs_voice = "sk_test", "VoiceId123"
    asked = []

    def cloud(text, key, voice, model, how=None):
        asked.append((text, key, voice, model))
        return np.full(2400, 8192, dtype="<i2").tobytes(), 7

    g._cloud = cloud
    return asked


def test_elevenlabs_reads_the_greeting_counts_the_credits_and_keeps_the_file(speech):
    app, g, calls = speech
    asked = _elevenlabs(app, g)
    app.cfg.greeting.elevenlabs_prefix = "[excited]"
    assert g.uses_cloud("vi") and g.uses_zerotts("vi") and g.voice_ready("vi") and not g.uses_cloud("en")
    g._say("Xin chào")
    g._say("Xin chào")  # the second time from the file: not paid for again
    assert asked == [("[excited] Xin chào", "sk_test", "VoiceId123", "eleven_v3")]
    played = [c for c in calls if c[0] == "play"]
    assert len(played) == 2 and played[0][2] == 24000 and abs(float(played[0][1][0]) - 0.25) < 1e-6
    assert not [c for c in calls if c[0] in ("load", "synthesize")]  # ZeroTTS was not needed
    assert g.cloud_used()["credits"] == 7 and g.status()["elevenlabs"] == {"credits": 7, "keys": {"1": 7}, "error": None}
    app.cfg.greeting.elevenlabs_model = "eleven_flash_v2_5"  # another model: made again
    g._say("Xin chào")
    assert len(asked) == 2 and g.cloud_used()["credits"] == 14


def test_elevenlabs_without_key_or_voice_is_plain_zerotts(speech):
    app, g, calls = speech
    app.cfg.greeting.engine = "elevenlabs"
    assert g.uses_zerotts("vi") and not g.uses_cloud("vi") and g.voice_for("vi") == "maichi"
    g._say("Xin chào")
    assert [c[2]["voice"] for c in calls if c[0] == "synthesize"] == ["maichi"]


def test_when_elevenlabs_fails_zerotts_reads_and_it_is_left_alone_for_a_while(speech):
    import datetime as dt

    app, g, calls = speech
    _elevenlabs(app, g)
    failures = []

    def down(text, key, voice, model, how=None):
        failures.append(text)
        raise greeting.CloudSpeechError("URLError: no network")

    g._cloud = down
    day = g.now().date()
    with pytest.raises(RuntimeError, match="ElevenLabs"):  # made ahead: nobody waits, it is tried again later
        g._audio("Chào Alice", g.voice_for("vi"), day, ahead=True)
    assert not [c for c in calls if c[0] == "synthesize"]
    g._say("Xin chào")  # at the door: ZeroTTS reads at once, without asking ElevenLabs again
    assert len(failures) == 1 and [c[2]["voice"] for c in calls if c[0] == "synthesize"] == ["maichi"]
    assert "no network" in g.status()["elevenlabs"]["error"]
    assert not g._cloud_open()
    g._cloud_off = {k: (g.now() - dt.timedelta(seconds=1), hard) for k, (_, hard) in g._cloud_off.items()}  # the pause is over
    asked = _elevenlabs(app, g)
    g._say("Chào buổi sáng")
    assert len(asked) == 1 and g.status()["elevenlabs"]["error"] is None


def test_when_the_elevenlabs_credits_are_used_up_zerotts_reads_ahead_until_tomorrow(speech):
    app, g, calls = speech
    _elevenlabs(app, g)

    def empty(text, key, voice, model, how=None):
        raise greeting.CloudSpeechError("HTTP 401 quota_exceeded: no credits", quota=True)

    g._cloud = empty
    audio, rate = g._audio("Chào Alice", g.voice_for("vi"), g.now().date(), ahead=True)
    assert rate == 48000 and [c[2]["voice"] for c in calls if c[0] == "synthesize"] == ["maichi"]
    assert g._cloud_off["sk_test"][0].date() > g.now().date()


def test_a_spare_elevenlabs_account_reads_when_the_first_is_used_up_or_refused(speech):
    app, g, calls = speech
    _elevenlabs(app, g)
    app.cfg.greeting.elevenlabs_key2, app.cfg.greeting.elevenlabs_voice2 = "sk_two", "VoiceTwo"
    app.cfg.greeting.elevenlabs_key3 = "sk_three"  # no voice of its own: the first one's
    assert g.cloud_accounts() == [(1, "sk_test", "VoiceId123"), (2, "sk_two", "VoiceTwo"), (3, "sk_three", "VoiceId123")]
    asked = []

    def cloud(text, key, voice, model, how=None):
        asked.append((key, voice))
        if key == "sk_test":
            raise greeting.CloudSpeechError("HTTP 401 quota_exceeded: no credits", quota=True, answered=True)
        return np.full(2400, 8192, dtype="<i2").tobytes(), 5

    g._cloud = cloud
    g._say("Xin chào")
    g._say("Chào buổi sáng")  # the used-up account is not asked again today
    assert asked == [("sk_test", "VoiceId123"), ("sk_two", "VoiceTwo"), ("sk_two", "VoiceTwo")]
    assert not [c for c in calls if c[0] == "synthesize"]  # ZeroTTS was not needed
    status = g.status()["elevenlabs"]
    assert status["credits"] == 10 and status["keys"] == {"2": 10} and "key 1" in status["error"]
    app.cfg.greeting.elevenlabs_key = "sk_new"  # the key is replaced in Settings: tried again at once
    g._say("Chào cả nhà")
    assert asked[-1] == ("sk_new", "VoiceId123")


def test_without_a_network_the_spare_elevenlabs_accounts_are_not_tried(speech):
    app, g, calls = speech
    _elevenlabs(app, g)
    app.cfg.greeting.elevenlabs_key2 = "sk_two"
    asked = []

    def down(text, key, voice, model, how=None):
        asked.append(key)
        raise greeting.CloudSpeechError("URLError: no network")

    g._cloud = down
    g._say("Xin chào")
    assert asked == ["sk_test"] and not g._cloud_open()
    assert [c[2]["voice"] for c in calls if c[0] == "synthesize"] == ["maichi"]


def test_settings_keep_a_spare_elevenlabs_key_and_remove_it_when_asked(core):
    app, _, _ = core
    client = create_app(app).test_client()
    form = {f"{section}.{name}" if section else name: ("on" if value is True else "" if value is False else str(value))
            for section, names in settings.EDITABLE.items() for name, value in
            ((n, settings.current(app.cfg)[section][n]) for n in names)
            if name not in settings.SECRET and name != "work_days"}
    form = {k: v for k, v in form.items() if v != ""}
    response = client.post("/settings", data={**form, "greeting.elevenlabs_key2": "sk_two", "greeting.elevenlabs_voice2": "VoiceTwo"})
    assert response.status_code in (200, 302) and app.cfg.greeting.elevenlabs_key2 == "sk_two"
    client.post("/settings", data={**form, "greeting.elevenlabs_voice2": "VoiceTwo"})  # empty: kept
    assert app.cfg.greeting.elevenlabs_key2 == "sk_two"
    page = client.get("/settings").get_data(as_text=True)
    assert "sk_two" not in page and 'name="greeting.elevenlabs_key2_off"' in page
    client.post("/settings", data={**form, "greeting.elevenlabs_key2_off": "on"})
    assert app.cfg.greeting.elevenlabs_key2 == ""


def test_elevenlabs_settings_are_checked_and_the_key_is_secret(core):
    from attendance.core import diagnostics

    app, _, _ = core
    assert {"elevenlabs_key2", "elevenlabs_key3"} <= settings.SECRET and "elevenlabs_key" in settings.SECRET and "elevenlabs_key" in settings.EDITABLE["greeting"]
    assert diagnostics.redact_settings({"greeting": {"elevenlabs_key": "sk_x"}})["greeting"]["elevenlabs_key"] == diagnostics.HIDDEN
    app.cfg.greeting.elevenlabs_voice = "../v1/user"
    with pytest.raises(ValueError, match="elevenlabs_voice"):
        app.cfg.validate()


def test_the_elevenlabs_error_names_the_reason_and_never_the_key(monkeypatch):
    import io
    import urllib.error

    def refuse(request, timeout):
        assert request.get_header("Xi-api-key") == "sk_secret" and "VoiceId" in request.full_url
        body = b'{"detail":{"status":"quota_exceeded","message":"You have 3 credits remaining"}}'
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(greeting.urllib.request, "urlopen", refuse)
    with pytest.raises(greeting.CloudSpeechError) as caught:
        greeting.elevenlabs_speech("Xin chào", "sk_secret", "VoiceId", "eleven_v3")
    assert caught.value.answered and caught.value.quota and "quota_exceeded" in str(caught.value) and "sk_secret" not in str(caught.value)


def test_the_settings_page_tries_elevenlabs_values_before_they_are_saved(speech):
    app, g, calls = speech
    _elevenlabs(app, g)
    app.cfg.greeting.elevenlabs_key2 = "sk_two"
    asked = []

    def cloud(text, key, voice, model, how=None):
        asked.append((text, key, voice, model, how))
        if voice == "Wrong":
            raise greeting.CloudSpeechError("HTTP 404 voice_not_found: no such voice", answered=True)
        return np.full(2400, 8192, dtype="<i2").tobytes(), 9

    g._cloud = cloud
    wav = g.render_text("Chào anh", "elevenlabs", {"account": 2, "voice": "Other", "model": "eleven_flash_v2_5",
                                                   "prefix": "[cheerfully]", "stability": 0.0, "speed": 1.1})
    assert wav.startswith(b"RIFF")
    assert asked == [("[cheerfully] Chào anh", "sk_two", "Other", "eleven_flash_v2_5", {"stability": 0.0, "speed": 1.1})]
    assert g.cloud_used()["keys"] == {"2": 9} and not calls  # counted; nothing played, ZeroTTS not loaded
    with pytest.raises(ValueError, match="voice_not_found"):
        g.render_text("Chào anh", "elevenlabs", {"account": 1, "voice": "Wrong"})
    assert len(g._cloud_open()) == 2 and g.cloud_error is None  # a failed try does not take the key from the greetings
    with pytest.raises(ValueError, match="key 3 is not saved"):
        g.render_text("Chào anh", "elevenlabs", {"account": 3})
    g._say("Xin chào")  # a greeting: the saved values
    assert asked[-1] == ("Xin chào", "sk_test", "VoiceId123", "eleven_v3", {"stability": 0.5, "speed": 1.0})
    app.cfg.greeting.elevenlabs_stability = 0.0  # changed in Settings: the sentence is made again
    g._say("Xin chào")
    assert asked[-1][4] == {"stability": 0.0, "speed": 1.0} and len(asked) == 4


def test_the_preview_request_carries_the_elevenlabs_values_and_checks_them(core, monkeypatch):
    app, _, _ = core
    seen = []
    monkeypatch.setattr(app.greeter, "render_text", lambda *args: seen.append(args) or b"RIFFtest")
    client = create_app(app).test_client()
    form = {"text": "Chào anh", "voice": "elevenlabs", "cloud_account": "2", "cloud_voice": "Other",
            "cloud_model": "eleven_v3", "cloud_prefix": "[excited]", "cloud_stability": "0.25", "cloud_speed": "1.1"}
    assert client.post("/settings/greeting/preview-text", data=form).status_code == 200
    assert seen == [("Chào anh", "elevenlabs", {"account": 2, "voice": "Other", "model": "eleven_v3",
                                                 "prefix": "[excited]", "stability": 0.25, "speed": 1.1})]
    for bad in ({"cloud_voice": "../user"}, {"cloud_speed": "3"}, {"cloud_stability": "x"}, {"cloud_account": "9"}):
        assert client.post("/settings/greeting/preview-text", data={**form, **bad}).status_code == 400
    assert len(seen) == 1
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="cloud-account"' in page and 'data-engine="elevenlabs"' in page and 'name="greeting.elevenlabs_stability"' in page


def test_today_offers_to_hear_a_greeting_again_in_a_chosen_voice_but_not_to_a_viewer(core):
    from conftest import at

    app, clock, _ = core
    clock.t = at(8, 30)
    app.service.on_identified("A", at(8, 30), 0.9, None)
    app.store.add_greeting(at(8, 30).date(), "A", at(8, 30), "Chào Alice!", "on_time")
    app.cfg.greeting.elevenlabs_key, app.cfg.greeting.elevenlabs_voice = "sk_test", "VoiceId123"
    page = create_app(app).test_client().get("/").get_data(as_text=True)
    assert 'class="link listen" data-say="Chào Alice!"' in page and 'id="listen-bar"' in page
    assert '<option value="elevenlabs:VoiceId123">ElevenLabs · the saved voice' in page and "ZeroTTS · maichi" in page
    assert '<option value="elevenlabs:6Xq32rHt6oAu98TBDKfr">ElevenLabs · Tram' in page and "sk_test" not in page
    assert '<option value="[surprised]">surprised</option>' in page
    app.cfg.web.password, app.cfg.web.viewer_password = "admin-password", "viewer-password"
    viewer = create_app(app).test_client().get("/", auth=(app.cfg.web.viewer_username, "viewer-password"))
    assert viewer.status_code == 200 and "Chào Alice!" in viewer.get_data(as_text=True)
    assert 'id="listen-bar"' not in viewer.get_data(as_text=True) and "class=\"link listen\"" not in viewer.get_data(as_text=True)


def test_claude_chooses_emotions_only_for_eleven_v3_and_nobody_else_sees_the_tags(speech):
    from conftest import at

    app, g, calls = speech
    clock = g.now
    assert greeting.untagged("[surprised] Ủa, anh Minh! [laughs] Sớm ghê.") == "Ủa, anh Minh! Sớm ghê."
    assert greeting.acted("[surprised] Ủa! [shouting] Sớm ghê.", ["surprised"]) == "[surprised] Ủa! Sớm ghê."
    person = app.store.employee("A")
    assert g.emotion_tags() == [] and "[excited]" not in g.prompt([person], clock().date())[0]  # ZeroTTS: no tags asked
    asked = _elevenlabs(app, g)
    app.cfg.greeting.elevenlabs_tags = "surprised, laughs"
    assert g.emotion_tags("vi") == ["surprised", "laughs"] and g.emotion_tags("en") == []
    system = g.prompt([person], clock().date())[0]
    assert "[surprised] [laughs]" in system and "choosing only from" in system
    line = "[surprised] Ủa, Alice! [shouting] [laughs] Sớm ghê."
    assert g.voiced(line, [person], "vi", clock().date()) == "[surprised] Ủa, Alice! [laughs] Sớm ghê."  # not allowed: dropped
    assert g.voiced(line, [person], "en", clock().date()) == "Ủa, Alice! Sớm ghê."  # English is read by macOS
    app.cfg.greeting.elevenlabs_prefix = "[cheerfully]"
    g._say(g.voiced(line, [person], "vi", clock().date()))
    g._say("Xin chào")
    assert [a[0] for a in asked] == ["[surprised] Ủa, Alice! [laughs] Sớm ghê.", "[cheerfully] Xin chào"]
    app.cfg.greeting.elevenlabs_model = "eleven_flash_v2_5"  # would read the tags aloud
    assert g.emotion_tags() == [] and g.voiced(line, [person], "vi", clock().date()) == "Ủa, Alice! Sớm ghê."
    app.cfg.greeting.elevenlabs_model, app.cfg.greeting.elevenlabs_emotions = "eleven_v3", False
    assert g.emotion_tags() == [] and "[surprised]" not in g.prompt([person], clock().date())[0]


def test_tagged_sentences_are_recorded_without_tags_and_zerotts_never_reads_one(speech):
    app, g, calls = speech
    _elevenlabs(app, g)
    app.cfg.greeting.enabled = True
    day = g.now().date()
    person = app.store.employee("A")
    app.store.put(greeting.LINES_KEY, __import__("json").dumps({"day": day.isoformat(), "model": "m", "people": {"A": {
        "say_as": "", "on_time": ["[excited] Chào Alice! [laughs] Sớm ghê."], "late": ["[surprised] Alice đến rồi!"],
        "language": "vi", "honorific": person.honorific, "called": greeting.called(person), "note": g.note(person, day),
        "emotions": ", ".join(g.emotion_tags("vi"))}}}))
    said = []
    g.on_greeted = lambda ids, ts, text, kind: said.append(text)

    def down(text, key, voice, model, how=None):
        raise greeting.CloudSpeechError("URLError: no network")

    g._cloud = down
    g.arrived("A", g.now())
    assert g.speak_next(timeout=0)
    assert [c[1] for c in calls if c[0] == "synthesize"] == ["Chào Alice! Sớm ghê."]  # ZeroTTS read instead: no tag
    assert app.store.greetings(day)["A"]["text"] == "Chào Alice! Sớm ghê." and said == ["Chào Alice! Sớm ghê."]
    assert g.heard("A", day + __import__("datetime").timedelta(days=1)) == ["Chào Alice! Sớm ghê."]
    app.cfg.greeting.elevenlabs_tags = "excited"  # the emotions allowed changed: today's sentences are written again
    assert g._entry(person, day) == {}
    with pytest.raises(ValueError, match="elevenlabs_tags"):
        app.cfg.greeting.elevenlabs_tags = "ex<cited>"
        app.cfg.validate()


def test_today_can_hear_a_greeting_with_emotions_claude_chooses_without_changing_its_words(speech):
    app, g, calls = speech
    asked = _elevenlabs(app, g)
    day = g.now().date()
    app.cfg.greeting.key, app.cfg.greeting.elevenlabs_tags = "k", "surprised, laughs"
    app.store.add_greeting(day, "A", g.now(), "Ủa, Alice! Sớm ghê.", "on_time")
    app.store.add_greeting(day, "B", g.now(), "Bob đến rồi!", "late")
    requests = []

    def act(sentences, tags):
        requests.append((list(sentences), list(tags)))
        return {"Ủa, Alice! Sớm ghê.": "[surprised] Ủa, Alice! [shouting] [laughs] Sớm ghê.",
                "Bob đến rồi!": "[laughs] Bob đã đến rồi nha!"}  # the second one changes the words: not used
        
    def answer(sentences, tags):
        chosen = act(sentences, tags)
        return [chosen.get(s, "") for s in sentences]

    g._act = answer
    wav = g.render_text("Ủa, Alice! Sớm ghê.", "elevenlabs", {"voice": "Other", "prefix": "", "emotions": "auto"})
    assert wav.startswith(b"RIFF") and asked[-1][0] == "[surprised] Ủa, Alice! [laughs] Sớm ghê."
    assert requests == [(["Ủa, Alice! Sớm ghê.", "Bob đến rồi!"], ["surprised", "laughs"])]  # the day's greetings at once
    g.render_text("Bob đến rồi!", "elevenlabs", {"emotions": "auto"})
    assert asked[-1][0] == "Bob đến rồi!" and len(requests) == 1  # kept for the day; changed words were refused
    with pytest.raises(ValueError, match="Eleven v3"):
        g.render_text("Bob đến rồi!", "elevenlabs", {"emotions": "auto", "model": "eleven_flash_v2_5"})
    app.cfg.greeting.key = ""
    with pytest.raises(ValueError, match="Claude API key"):
        g.render_text("Một câu mới.", "elevenlabs", {"emotions": "auto"})


def test_the_today_listen_request_asks_for_claudes_emotions(core, monkeypatch):
    app, _, _ = core
    seen = []
    monkeypatch.setattr(app.greeter, "render_text", lambda *args: seen.append(args) or b"RIFFtest")
    client = create_app(app).test_client()
    form = {"text": "Chào anh", "voice": "elevenlabs", "cloud_voice": "Other", "cloud_prefix": "", "cloud_emotions": "auto"}
    assert client.post("/settings/greeting/preview-text", data=form).status_code == 200
    assert seen[0][2]["emotions"] == "auto" and seen[0][2]["voice"] == "Other" and seen[0][2]["prefix"] == ""
