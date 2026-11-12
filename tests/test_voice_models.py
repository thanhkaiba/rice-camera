import hashlib
from types import SimpleNamespace

import pytest

from attendance.core import voice_models


@pytest.fixture
def assets(tmp_path, monkeypatch):
    data = b'verified model'
    files = {'config.json': (len(data), hashlib.sha256(data).hexdigest()),
             'onnx/codec/moss_audio_tokenizer_encode.data': (len(data), hashlib.sha256(data).hexdigest())}
    monkeypatch.setattr(voice_models, 'FILES', files)
    calls = []

    def fetch(url, dest, progress):
        calls.append(url)
        dest.write_bytes(data)
    monkeypatch.setattr(voice_models, '_fetch_to', fetch)
    cfg = SimpleNamespace(path=lambda name: tmp_path / name)
    return cfg, calls, data


def test_downloads_pinned_assets_and_reuses_them_offline(assets, monkeypatch):
    cfg, calls, data = assets
    custom = cfg.path('data/voices/adam/voice.npz')
    custom.parent.mkdir(parents=True)
    custom.write_bytes(b'custom voice')
    voice_models.ensure(cfg)
    assert calls == [voice_models.ZERO_BASE + 'config.json']
    voice_models.ensure(cfg, creation=True)
    assert calls[-1] == voice_models.MOSS_BASE + 'moss_audio_tokenizer_encode.data'
    monkeypatch.setattr(voice_models, '_fetch_to', lambda *a: pytest.fail('offline reuse downloaded'))
    voice_models.ensure(cfg, creation=True)
    assert custom.read_bytes() == b'custom voice'
    assert cfg.path('models/ZeroTTS/config.json').read_bytes() == data


def test_bad_checksum_keeps_old_file_and_retry_succeeds(assets, monkeypatch):
    cfg, calls, data = assets
    dest = cfg.path('models/ZeroTTS/config.json')
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b'old')
    monkeypatch.setattr(voice_models, '_fetch_to', lambda url, staged, progress: staged.write_bytes(b'x' * len(data)))
    with pytest.raises(RuntimeError, match='checksum'):
        voice_models.ensure(cfg)
    assert dest.read_bytes() == b'old'
    assert not list(dest.parent.glob('*.download*'))
    monkeypatch.setattr(voice_models, '_fetch_to', lambda url, staged, progress: staged.write_bytes(data))
    voice_models.ensure(cfg)
    assert dest.read_bytes() == data


def test_network_failure_cleans_partial_and_can_retry(assets, monkeypatch):
    cfg, _, data = assets

    def fail(url, dest, progress):
        dest.with_name(dest.name + '.part').write_bytes(b'partial')
        raise OSError('offline')
    monkeypatch.setattr(voice_models, '_fetch_to', fail)
    with pytest.raises(OSError, match='offline'):
        voice_models.ensure(cfg)
    assert not list(cfg.path('models/ZeroTTS').glob('*'))
    monkeypatch.setattr(voice_models, '_fetch_to', lambda url, dest, progress: dest.write_bytes(data))
    voice_models.ensure(cfg)
    assert cfg.path('models/ZeroTTS/config.json').read_bytes() == data
