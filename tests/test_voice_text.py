import base64
import threading

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


def test_remote_auto_preview_uses_camera_choice_without_choosing_locally(core, monkeypatch):
    import json
    from attendance.web.edge_api import HEARTBEAT_KEY
    app, clock, _ = core
    app.photos_read_elsewhere = True
    calls = []
    monkeypatch.setattr(app.greeter, 'render_text', lambda text, voice: calls.append(voice) or b'RIFFtest')
    app.store.put(HEARTBEAT_KEY, json.dumps({'greeting': {'daily_voice': {
        'name': 'camera-adam', 'at': clock().isoformat()}}}))
    response = create_app(app).test_client().post('/settings/greeting/preview-text', data={'text': 'Test', 'voice': 'auto'})
    assert response.status_code == 200 and calls == ['camera-adam']
    assert app.store.get('greeting_daily_voice') is None


def test_remote_auto_waits_for_fresh_same_day_camera_report(core, monkeypatch):
    import datetime as dt
    import json
    from attendance.web.edge_api import HEARTBEAT_KEY
    from conftest import at
    app, clock, _ = core
    app.photos_read_elsewhere = True
    monkeypatch.setattr(app.greeter, 'render_text', lambda *a: (_ for _ in ()).throw(AssertionError('must not synthesize')))
    client = create_app(app).test_client()
    for daily in [{}, {'name': 'adam', 'at': (clock() - dt.timedelta(minutes=4)).isoformat()},
                  {'name': 'adam', 'at': 'invalid'}, {'name': 'adam', 'at': '2026-10-14T08:00:00'}]:
        app.store.put(HEARTBEAT_KEY, json.dumps({'greeting': {'daily_voice': daily}}))
        response = client.post('/settings/greeting/preview-text', data={'text': 'Test', 'voice': 'auto'})
        assert response.status_code == 409
    # A report just two minutes old still belongs to yesterday after midnight.
    clock.t = at(0, 1)
    app.store.put(HEARTBEAT_KEY, json.dumps({'greeting': {'daily_voice': {
        'name': 'adam', 'at': (clock() - dt.timedelta(minutes=2)).isoformat()}}}))
    assert client.post('/settings/greeting/preview-text', data={'text': 'Test', 'voice': 'auto'}).status_code == 409


def test_camera_heartbeat_drives_remote_auto_preview(live, tmp_path, monkeypatch):
    from test_edge import admin, follower
    from conftest import at
    from attendance.web.edge_api import heartbeat
    _, portal, client = live
    from attendance.core.config import Config
    from attendance.core.pipeline import App
    cfg = Config()
    cfg.base_dir = tmp_path / "camera"
    cfg.greeting.engine = "zerotts"
    camera = App(cfg, now=lambda: at(10, 30))
    pack = camera.cfg.path('models/ZeroTTS/voices/camera-adam/voice.npz')
    pack.parent.mkdir(parents=True)
    pack.touch()
    _, edge = follower(live, tmp_path)
    edge.status = camera._edge_status
    edge.heartbeat()
    assert heartbeat(portal.store)['greeting']['daily_voice']['name'] == 'camera-adam'
    selected = []
    monkeypatch.setattr(portal.greeter, 'render_text', lambda text, voice: selected.append(voice) or b'RIFFtest')
    response = client.post('/settings/greeting/preview-text', data={'text': 'Đọc thử.', 'voice': 'auto'}, headers=admin())
    assert response.status_code == 200 and selected == ['camera-adam']
