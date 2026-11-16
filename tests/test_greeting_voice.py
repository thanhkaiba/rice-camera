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
