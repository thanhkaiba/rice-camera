import base64
import threading

import pytest

from attendance.web import create_app
from test_edge import live  # noqa: F401


def test_text_preview_returns_private_audio_and_keeps_settings(core, monkeypatch):
    app, _, _ = core
    calls = []
    monkeypatch.setattr(app.greeter, 'render_text', lambda text, voice: calls.append((text, voice)) or b'RIFFtest')
    client = create_app(app).test_client()
    response = client.post('/settings/greeting/preview-text', data={'text': '  Nội dung khác.  ', 'voice': 'auto'})
    assert response.status_code == 200 and response.data == b'RIFFtest'
    assert response.mimetype == 'audio/wav' and response.headers['Cache-Control'] == 'private, no-store'
    assert calls == [('Nội dung khác.', 'auto')]
    assert app.cfg.greeting.zerotts_voice == 'maichi'
    for text in ['', '   ', 'a' * 501]:
        assert client.post('/settings/greeting/preview-text', data={'text': text}).status_code == 400
    assert len(calls) == 1
    assert client.post('/settings/greeting/preview-text', data={'text': 'a' * 9000}).status_code == 413


def test_text_preview_requires_admin(core):
    app, _, _ = core
    app.cfg.web.password = 'admin-password'
    app.cfg.web.viewer_password = 'viewer-password'
    client = create_app(app).test_client()
    assert client.post('/settings/greeting/preview-text').status_code == 401
    headers = {'Authorization': 'Basic ' + base64.b64encode(b'viewer:viewer-password').decode()}
    assert client.post('/settings/greeting/preview-text', headers=headers).status_code == 403


def test_failed_preview_releases_slot_and_concurrent_previews_are_rejected(core, monkeypatch):
    app, _, _ = core
    web = create_app(app)
    entered, release = threading.Event(), threading.Event()
    def render(text, voice):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('encoder error')
    monkeypatch.setattr(app.greeter, 'render_text', render)
    results = []
    worker = threading.Thread(target=lambda: results.append(web.test_client().post(
        '/settings/greeting/preview-text', data={'text': 'Test'})))
    worker.start()
    try:
        assert entered.wait(3)
        assert web.test_client().post('/settings/greeting/preview-text', data={'text': 'Test'}).status_code == 409
    finally:
        release.set()
        worker.join(5)
    assert results[0].status_code == 500
    monkeypatch.setattr(app.greeter, 'render_text', lambda *args: b'RIFFtest')
    assert web.test_client().post('/settings/greeting/preview-text', data={'text': 'Retry'}).status_code == 200



def test_remote_preview_runs_on_camera_and_returns_audio(live, tmp_path, monkeypatch):
    from test_edge import admin, follower
    _, portal, client = live
    _, edge = follower(live, tmp_path)
    wav = b'RIFF\x04\x00\x00\x00WAVEtest'
    def render(text, voice):
        assert (text, voice) == ('Đọc thử.', 'auto')
        return wav
    edge.render_text = render
    monkeypatch.setattr(portal.greeter, 'render_text', lambda *a: (_ for _ in ()).throw(AssertionError('server must not synthesize')))
    response = client.post('/settings/greeting/preview-text', data={'text': 'Đọc thử.', 'voice': 'auto'}, headers=admin())
    assert response.status_code == 202
    url = response.json['preview_url']
    assert client.get(url).status_code == 401
    assert client.get(url, headers=admin()).status_code == 202
    assert client.post('/settings/greeting/preview-text', data={'text': 'Other'}, headers=admin()).status_code == 409
    edge.follow()
    response = client.get(url, headers=admin())
    assert response.status_code == 200 and response.data == wav
    assert response.mimetype == 'audio/wav' and response.headers['Cache-Control'] == 'private, no-store'
    assert portal.greeter._tts is None
    assert portal.store.edge_jobs() == []
    assert not portal.cfg.path('models/ZeroTTS').exists()
    assert client.get(url, headers=admin()).status_code == 404


def test_remote_preview_reports_camera_failure_and_can_retry(live, tmp_path):
    from test_edge import admin, follower
    _, _, client = live
    _, edge = follower(live, tmp_path)
    def render(*args):
        raise RuntimeError('encoder failed')
    edge.render_text = render
    response = client.post('/settings/greeting/preview-text', data={'text': 'Test'}, headers=admin())
    assert response.status_code == 202
    url = response.json['preview_url']
    edge.follow()
    result = client.get(url, headers=admin())
    assert result.status_code == 502 and 'encoder failed' in result.json['message']
    assert client.post('/settings/greeting/preview-text', data={'text': 'Retry'}, headers=admin()).status_code == 202


def test_remote_preview_timeout_discards_job_and_late_result(live, monkeypatch):
    from test_edge import admin, AUTH
    import attendance.web.settings as routes
    _, portal, client = live
    now = [1000.0]
    monkeypatch.setattr(routes.time, 'time', lambda: now[0])
    response = client.post('/settings/greeting/preview-text', data={'text': 'Test'}, headers=admin())
    assert response.status_code == 202
    url = response.json['preview_url']
    job = portal.store.edge_jobs()[0]
    now[0] += 121
    assert client.get(url, headers=admin()).status_code == 504
    assert portal.store.edge_jobs() == []
    assert client.post(f"/api/edge/jobs/{job['id']}/result", json={'ok': False, 'message': 'late'}, headers=AUTH).status_code == 200
    assert client.post('/settings/greeting/preview-text', data={'text': 'Retry'}, headers=admin()).status_code == 202



@pytest.mark.parametrize('audio', [b'not a WAV', b''])
def test_remote_preview_rejects_invalid_camera_audio(live, audio):
    from test_edge import admin, AUTH
    _, portal, client = live
    response = client.post('/settings/greeting/preview-text', data={'text': 'Test'}, headers=admin())
    token = response.json['preview_url'].rsplit('/', 1)[-1]
    result = client.put(f'/api/edge/previews/{token}?offset=0&total={len(audio)}', data=audio, content_type='audio/wav', headers=AUTH)
    assert result.status_code == 400


def test_large_camera_wav_is_uploaded_below_nginx_default_limit(live, tmp_path):
    from test_edge import admin, follower
    _, _, client = live
    _, edge = follower(live, tmp_path)
    wav = b'RIFF\x04\x00\x00\x00WAVE' + b'\x00' * 1900000
    edge.render_text = lambda *args: wav
    send = edge._send
    puts = []
    def bounded(method, path, body, content_type):
        assert len(body) < 1024 * 1024
        if method == 'PUT':
            puts.append(len(body))
        return send(method, path, body, content_type)
    edge._send = bounded
    response = client.post('/settings/greeting/preview-text', data={'text': 'Test'}, headers=admin())
    edge.follow()
    assert len(puts) > 1
    assert client.get(response.json['preview_url'], headers=admin()).data == wav


def test_preview_upload_requires_camera_auth_and_sequential_chunks(live):
    from test_edge import admin, AUTH
    _, _, client = live
    response = client.post('/settings/greeting/preview-text', data={'text': 'Test'}, headers=admin())
    token = response.json['preview_url'].rsplit('/', 1)[-1]
    url = f'/api/edge/previews/{token}'
    assert client.put(url, data=b'RIFF').status_code == 401
    assert client.put(url+'?offset=4&total=20', data=b'WAVE', headers=AUTH).status_code == 409
    assert client.put(url+'?offset=0&total=99999999', data=b'RIFF', headers=AUTH).status_code == 400

def test_camera_retries_delivery_without_synthesizing_again(live, tmp_path):
    from test_edge import admin, follower
    _, _, client = live
    _, edge = follower(live, tmp_path)
    calls = []
    wav = b'RIFF\x04\x00\x00\x00WAVEtest'
    edge.render_text = lambda *args: calls.append(args) or wav
    response = client.post('/settings/greeting/preview-text', data={'text': 'Test', 'voice': 'maichi'}, headers=admin())
    assert response.status_code == 202
    send = edge._send
    def unavailable(method, path, body, content_type):
        if path.endswith('/result'):
            return 503, b'unavailable'
        return send(method, path, body, content_type)
    edge._send = unavailable
    with pytest.raises(ConnectionError, match='503'):
        edge.follow()
    edge._send = send
    edge.follow()
    result = client.get(response.json['preview_url'], headers=admin())
    assert result.data == wav and calls == [('Test', 'maichi')]


def test_camera_does_not_synthesize_expired_job(live, tmp_path):
    from test_edge import follower
    _, portal, client = live
    _, edge = follower(live, tmp_path)
    edge.render_text = lambda *args: pytest.fail('expired preview must not run')
    portal.store.edge_job_add('preview_text', {'kind': 'preview_text', 'preview_id': 'gone',
        'text': 'Test', 'voice': 'auto', 'expires': 0}, portal.service.now())
    edge.follow()
    assert portal.store.edge_jobs() == []


def test_remote_preview_validates_input_before_queueing(live):
    from test_edge import admin
    _, portal, client = live
    for text in ['', '   ', 'a' * 501]:
        assert client.post('/settings/greeting/preview-text', data={'text': text}, headers=admin()).status_code == 400
    assert portal.store.edge_jobs() == []


def test_expired_preview_does_not_block_sync_when_browser_leaves(live, monkeypatch):
    from test_edge import admin, AUTH
    import attendance.web.settings as routes
    _, portal, client = live
    now = [1000.0]
    monkeypatch.setattr(routes.time, 'time', lambda: now[0])
    assert client.post('/settings/greeting/preview-text', data={'text': 'Test'}, headers=admin()).status_code == 202
    portal.store.edge_job_add('say', {'kind': 'say'}, portal.service.now())
    now[0] += 121
    jobs = client.get('/api/edge/sync', headers=AUTH).json['jobs']
    assert [job['kind'] for job in jobs] == ['say']
    assert [job['kind'] for job in portal.store.edge_jobs()] == ['say']


def test_camera_forgets_audio_if_expired_server_job_disappears(live, tmp_path):
    from test_edge import admin, follower
    import json
    _, portal, client = live
    mac, edge = follower(live, tmp_path)
    edge.render_text = lambda *args: b'RIFF\x04\x00\x00\x00WAVEtest'
    client.post('/settings/greeting/preview-text', data={'text': 'Private text'}, headers=admin())
    job = portal.store.edge_jobs()[0]
    send = edge._send
    def unavailable(method, path, body, content_type):
        if path.endswith('/result'):
            return 503, b'unavailable'
        return send(method, path, body, content_type)
    edge._send = unavailable
    with pytest.raises(ConnectionError):
        edge.follow()
    assert edge._preview_results
    assert not json.loads(mac.get('edge_jobs_unsent') or '{}')
    portal.store.edge_job_take(job['id'])
    edge._send = send
    edge.follow()
    assert edge._preview_results == {}
