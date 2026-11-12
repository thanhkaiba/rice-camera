import base64
import io
import threading

import numpy as np
import pytest
import soundfile as sf

from attendance.core import voice_creation
from attendance.web import create_app
from test_edge import admin, follower, live  # noqa: F401


def recording(seconds=4, rate=16000, channels=1, subtype='PCM_16', silence=False):
    audio = np.zeros(int(seconds * rate), dtype=np.float32) if silence else (
        0.2 * np.sin(2 * np.pi * 180 * np.arange(int(seconds * rate)) / rate)).astype(np.float32)
    if channels > 1:
        audio = np.repeat(audio[:, None], channels, axis=1)
    buf = io.BytesIO()
    sf.write(buf, audio, rate, format='WAV', subtype=subtype)
    return buf.getvalue()


@pytest.fixture
def creator(core, monkeypatch):
    core, _, _ = core
    waveforms = []
    def encode(cfg, waveform):
        waveforms.append(waveform)
        return np.ones((1, 10, 768), dtype=np.float32)
    monkeypatch.setattr(voice_creation, 'encode_reference', encode)
    def preview(emb, path):
        assert emb.shape == (1, 10, 768)
        sf.write(path, np.ones(4800, dtype=np.float32) * .1, 48000)
    monkeypatch.setattr(core.greeter, 'render_voice', preview)
    return core, create_app(core).test_client(), waveforms


def start(client, data=None, name='my-voice'):
    return client.post('/settings/greeting/create', data={
        'voice_name': name, 'reference': (io.BytesIO(recording() if data is None else data), 'reference.wav')},
        headers={'X-Requested-With': 'fetch'})


def finish(core):
    core.voice_creator._thread.join(3)
    assert not core.voice_creator._thread.is_alive()
    return core.voice_creator.status()


def test_create_preview_and_explicitly_use_a_voice(creator):
    core, client, waveforms = creator
    before = core.cfg.greeting.zerotts_voice
    response = start(client)
    assert response.status_code == 202
    result = finish(core)
    assert result['state'] == 'ready' and core.cfg.greeting.zerotts_voice == before
    assert waveforms[0].shape == (1, 2, 192000) and waveforms[0].dtype == np.float32
    assert client.get('/settings/greeting/creation').json['state'] == 'ready'
    assert client.get(f"/settings/greeting/creation/{result['id']}/preview.wav").status_code == 200
    assert client.post('/settings/greeting/creation/use', data={'job_id': result['id']}).status_code == 302
    assert core.cfg.greeting.zerotts_voice == 'my-voice'
    assert core.cfg.path('data/voices/my-voice/voice.npz').is_file()
    assert core.voice_creator.status()['state'] == 'used'
    # A double click neither overwrites the installed voice nor queues duplicate transfers.
    assert client.post('/settings/greeting/creation/use', data={'job_id': result['id']}).status_code == 302
    assert len([r for r in core.store.audit() if r[2] == 'uploaded voice']) == 1


@pytest.mark.parametrize('data,name', [
    (b'bad wav', 'voice'), (recording(seconds=1), 'voice'), (recording(seconds=31), 'voice'),
    (recording(channels=3), 'voice'), (recording(silence=True), 'voice'),
    (recording(), '../escape'), (b'x' * (25 * 1024 * 1024), 'voice'),
], ids=['invalid', 'short', 'long', 'channels', 'silence', 'name', 'size'])
def test_invalid_recordings_cannot_start_a_job(creator, data, name):
    core, client, waveforms = creator
    assert start(client, data, name).status_code in (400, 413)
    assert waveforms == [] and core.voice_creator.status().get('state') != 'ready'


def test_creation_is_bounded_and_failures_preserve_the_active_voice(creator, monkeypatch):
    core, client, _ = creator
    entered, release = threading.Event(), threading.Event()
    def fail(cfg, waveform):
        entered.set()
        assert release.wait(3)
        raise RuntimeError('encoder unavailable')
    monkeypatch.setattr(voice_creation, 'encode_reference', fail)
    before = core.cfg.greeting.zerotts_voice
    assert start(client).status_code == 202
    try:
        assert entered.wait(2)
        assert start(client, name='another').status_code == 409
    finally:
        release.set()
    result = finish(core)
    assert result['state'] == 'error' and 'encoder unavailable' in result['message']
    assert core.cfg.greeting.zerotts_voice == before
    assert client.get(f"/settings/greeting/creation/{result['id']}/preview.wav").status_code == 404


def test_drafts_survive_restart_and_interrupted_jobs_are_reported(creator):
    core, client, _ = creator
    assert start(client).status_code == 202
    result = finish(core)
    restored = voice_creation.VoiceCreator(core.cfg, core.store, core.greeter)
    assert restored.status() == result
    core.store.put(voice_creation.STATUS_KEY, '{"state":"working","id":"interrupted"}')
    restored = voice_creation.VoiceCreator(core.cfg, core.store, core.greeter)
    assert restored.status()['state'] == 'error'


def test_reference_uploads_do_not_spool_recordings_to_disk(creator, monkeypatch):
    import werkzeug.formparser

    core, client, _ = creator
    def forbidden(*args, **kwargs):
        raise AssertionError('Raw reference recording was spooled to disk')
    monkeypatch.setattr(werkzeug.formparser, 'SpooledTemporaryFile', forbidden)
    data = recording(rate=48000, channels=2)
    assert len(data) > 500 * 1024
    assert start(client, data).status_code == 202
    assert finish(core)['state'] == 'ready'


def test_creation_preview_and_use_require_admin(creator):
    core, client, _ = creator
    core.cfg.web.password = 'admin-password'
    core.cfg.web.viewer_password = 'viewer-password'
    assert start(client).status_code == 401
    assert client.get('/settings/greeting/creation').status_code == 401
    assert client.get('/settings/greeting/creation/' + 'a'*32 + '/preview.wav').status_code == 401
    headers = {'Authorization': 'Basic ' + base64.b64encode(b'viewer:viewer-password').decode()}
    assert client.post('/settings/greeting/creation/use', headers=headers).status_code == 403


def test_a_created_voice_uses_the_existing_server_to_camera_transfer(live, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from attendance.core.config import Config
    from attendance.core.remote_voice_creation import run_on_camera
    from attendance.core.voice import install_voice
    application, core, client = live
    mac, edge = follower(live, tmp_path)
    cfg = Config()
    cfg.base_dir = tmp_path / 'camera'
    entered, release = threading.Event(), threading.Event()
    def encode(where, waveform):
        assert where is cfg, 'Voice models must only run on the camera'
        entered.set()
        assert release.wait(5)
        return np.ones((1, 10, 768), dtype=np.float32)
    monkeypatch.setattr(voice_creation, 'encode_reference', encode)
    monkeypatch.setattr(core.greeter, 'render_voice', lambda *a: pytest.fail('Server rendered a voice'))
    greeter = SimpleNamespace(render_voice=lambda emb, path: sf.write(path, np.ones(1000) * .1, 48000))
    camera = SimpleNamespace(edge=edge, voice_creator=voice_creation.VoiceCreator(cfg, mac, greeter))
    edge.on_create_voice = lambda payload: run_on_camera(camera, payload)
    response = client.post('/settings/greeting/create', data={
        'voice_name': 'office', 'reference': (io.BytesIO(recording()), 'sample.wav')}, headers=admin())
    assert response.status_code == 302
    pending = core.voice_creator.status()
    assert pending['state'] == 'working'
    assert (core.data_dir / pending['image']).is_file()
    assert client.post('/settings/greeting/creation/use', data={'job_id': pending['id']}, headers=admin()).status_code == 302
    assert core.cfg.greeting.zerotts_voice == 'maichi'
    edge.follow()
    worker = edge._creation_jobs[str(pending['job_id'])]['thread']
    try:
        assert entered.wait(2)
        edge.heartbeat()
        edge.alert('Camera remains responsive during voice creation')
        assert edge.deliver_batch() == 1
        edge.follow()
        assert core.voice_creator.status()['state'] == 'working'
        assert edge._creation_jobs[str(pending['job_id'])]['thread'] is worker
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    edge.follow()
    result = core.voice_creator.status()
    assert result['state'] == 'ready'
    assert core.cfg.greeting.zerotts_voice == 'maichi'
    assert not (core.data_dir / pending['image']).exists()
    assert result['id'] not in application.extensions['voice_previews']
    assert client.get(f"/settings/greeting/creation/{result['id']}/preview.wav", headers=admin()).status_code == 200
    assert not cfg.path('data/voices/office').exists()
    assert client.post('/settings/greeting/creation/use', data={'job_id': result['id']}, headers=admin()).status_code == 302
    assert core.cfg.greeting.zerotts_voice == 'maichi'
    edge.add_voice = lambda name, data: install_voice(tmp_path/'camera-voices', name, data)
    edge.follow()
    assert core.cfg.greeting.zerotts_voice == 'office' and core.store.edge_jobs() == []
    assert not edge._creation_jobs


def test_remote_creation_rejects_invalid_audio_and_expires_without_selecting(live, monkeypatch):
    import time

    application, core, client = live
    for data in (b'bad WAV', recording(seconds=1), recording(silence=True)):
        response = client.post('/settings/greeting/create', data={
            'voice_name': 'office', 'reference': (io.BytesIO(data), 'sample.wav')},
            headers={**admin(), 'X-Requested-With': 'fetch'})
        assert response.status_code == 400
    assert not core.store.edge_jobs()
    status = core.voice_creator.start('office', recording())
    with pytest.raises(BlockingIOError):
        core.voice_creator.start('another', recording())
    monkeypatch.setattr(time, 'time', lambda: status['expires'] + 1)
    assert core.voice_creator.status()['state'] == 'error'
    assert not (core.data_dir / status['image']).exists()
    assert status['id'] not in application.extensions['voice_previews']
    assert not core.store.edge_jobs()
    core.voice_creator.complete(status, {'ok': True, 'voice_pack': 'late'})
    assert core.voice_creator.status()['state'] == 'error'
    assert core.cfg.greeting.zerotts_voice == 'maichi'


@pytest.mark.parametrize('result,preview', [
    ({'ok': False, 'message': 'Camera encoder failed'}, True),
    ({'ok': True, 'voice_pack': 'not base64!'}, True),
    ({'ok': True, 'voice_pack': ''}, False),
])
def test_remote_creation_failures_discard_uploads_and_preserve_active_voice(live, result, preview):
    application, core, _ = live
    status = core.voice_creator.start('office', recording())
    if preview:
        application.extensions['voice_previews'][status['id']]['result'] = {'ok': True, 'audio': recording()}
    core.voice_creator.complete(status, result)
    assert core.voice_creator.status()['state'] == 'error'
    assert not (core.data_dir / status['image']).exists()
    assert not (core.voice_creator.root / status['id']).exists()
    assert status['id'] not in application.extensions['voice_previews']
    assert core.cfg.greeting.zerotts_voice == 'maichi'
    assert not core.store.edge_jobs()


def test_expiry_holds_the_lifecycle_lock_until_the_old_status_is_saved(live, monkeypatch):
    _, core, _ = live
    creator = core.voice_creator
    expired = creator.start('old', recording())
    creator._save({**expired, 'expires': 0})
    entered, release, started, completed = (threading.Event() for _ in range(4))
    cleanup = creator._cleanup
    def paused_cleanup(status):
        if threading.current_thread() is poller:
            entered.set()
            assert release.wait(3)
        cleanup(status)
    monkeypatch.setattr(creator, '_cleanup', paused_cleanup)
    poller = threading.Thread(target=creator.status)
    def start_new():
        started.set()
        creator.start('new', recording())
        completed.set()
    starter = threading.Thread(target=start_new)
    poller.start()
    try:
        assert entered.wait(2)
        starter.start()
        assert started.wait(2)
        assert not completed.wait(.1)
    finally:
        release.set()
        poller.join(3)
        starter.join(3)
    assert completed.is_set()
    assert creator.status()['state'] == 'working'
    assert creator.status()['name'] == 'new'


def test_background_creation_result_retries_without_restarting_worker(live, tmp_path):
    import json

    from attendance.core.edge import JOBS_KEY

    _, core, _ = live
    mac, edge = follower(live, tmp_path)
    calls = []
    edge.on_create_voice = lambda payload: calls.append(payload) or {'ok': False, 'message': 'encoder failed'}
    status = core.voice_creator.start('office', recording())
    edge.follow()
    worker = edge._creation_jobs[str(status['job_id'])]['thread']
    worker.join(3)
    assert not worker.is_alive()
    send = edge._send
    edge._send = lambda method, path, body, content_type: ((503, b'') if path.endswith('/result')
                                                         else send(method, path, body, content_type))
    with pytest.raises(ConnectionError):
        edge.follow()
    assert len(calls) == 1
    assert json.loads(mac.get(JOBS_KEY))[str(status['job_id'])] == {'ok': False, 'message': 'encoder failed'}
    edge._send = send
    edge.follow()
    assert len(calls) == 1 and not edge._creation_jobs
    assert json.loads(mac.get(JOBS_KEY)) == {}
    assert core.voice_creator.status()['state'] == 'error'


def test_expired_creation_never_starts_a_worker(live, tmp_path):
    _, core, _ = live
    _, edge = follower(live, tmp_path)
    status = core.voice_creator.start('office', recording())
    edge.on_create_voice = lambda payload: pytest.fail('Expired job started voice creation')
    job = core.store.edge_jobs()[0]
    job['payload']['expires'] = 0
    edge._do_job(job)
    assert not edge._creation_jobs
    assert core.voice_creator.status()['state'] == 'error'


def test_finished_background_creation_is_forgotten_if_server_job_disappears(live, tmp_path):
    _, core, _ = live
    _, edge = follower(live, tmp_path)
    edge.on_create_voice = lambda payload: {'ok': False, 'message': 'encoder failed'}
    status = core.voice_creator.start('office', recording())
    edge.follow()
    edge._creation_jobs[str(status['job_id'])]['thread'].join(3)
    core.voice_creator.complete(status, {'ok': False, 'message': 'cancelled'})
    edge.follow()
    assert not edge._creation_jobs
