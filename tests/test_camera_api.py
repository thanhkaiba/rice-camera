import datetime as dt
import json

import pytest

from attendance.core import settings
from attendance.web import create_app
from conftest import DAY, at


def connect(app, monkeypatch, responses):
    from attendance.integrations import camera_api

    app.cfg.camera_api.url = 'https://script.google.com/macros/s/test/exec'
    app.cfg.camera_api.key = 'camera-test-secret'
    calls = []

    def post(url, payload):
        calls.append((url, payload))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(camera_api, 'post', post)
    return calls


def result(status='created', email='alice@example.com'):
    return {'ok': True, 'results': [{'email': email, 'date': DAY.isoformat(), 'status': status}]}


def test_camera_api_waits_until_end_and_remembers_delivery(core, monkeypatch):
    app, clock, _ = core
    assert hasattr(app.cfg, 'camera_api')
    calls = connect(app, monkeypatch, [result()])
    app.service.on_identified('A', at(10, 12), .9, None)
    clock.t = at(10, 59)
    app.camera_sync.sync()
    assert not calls
    clock.t = at(11, 0)
    app.camera_sync.sync()
    assert calls[0][1] == {'action': 'camera.late', 'secret': 'camera-test-secret', 'events': [
        {'email': 'alice@example.com', 'date': DAY.isoformat(), 'arrivedAt': '10:12', 'lateMinutes': 12}]}
    from attendance.integrations.camera_api import CameraSync
    CameraSync(app.cfg, app.store, app.service).sync()
    assert len(calls) == 1


@pytest.mark.parametrize('response', [OSError('offline'), {'ok': False}, {'ok': True, 'results': []},
                                     {'ok': True, 'results': [{'status': 'created'}]}])
def test_camera_api_retries_incomplete_delivery_after_restart(core, monkeypatch, response):
    app, clock, _ = core
    assert hasattr(app.cfg, 'camera_api')
    calls = connect(app, monkeypatch, [response, result('duplicate')])
    app.service.on_identified('A', at(10, 5), .9, None)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    from attendance.integrations.camera_api import CameraSync
    restarted = CameraSync(app.cfg, app.store, app.service)
    restarted.sync()
    restarted.sync()
    assert len(calls) == 2


@pytest.mark.parametrize('status', ['duplicate', 'skipped', 'rejected'])
def test_camera_api_terminal_results_are_not_retried(core, monkeypatch, status):
    app, clock, _ = core
    assert hasattr(app.cfg, 'camera_api')
    calls = connect(app, monkeypatch, [result(status)])
    app.service.on_identified('A', at(10, 5), .9, None)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    app.camera_sync.sync()
    assert len(calls) == 1
    if status == 'rejected':
        assert not json.loads(app.store.get('camera_api_status'))['ok']


def test_camera_api_backfills_only_allowed_days_and_excludes_exempt(core, monkeypatch):
    app, clock, _ = core
    assert hasattr(app.cfg, 'camera_api')
    calls = connect(app, monkeypatch, [result()])
    app.service.on_identified('A', at(10, 5), .9, None)
    app.service.on_identified('B', at(9, 0), .9, None)
    app.store.update_employee('C', 'Carol', True, exempt=True)
    app.service.on_identified('C', at(10, 15), .9, None)
    clock.t = at(11, 0, day=DAY + dt.timedelta(days=7))
    app.camera_sync.sync()
    assert len(calls) == 1 and len(calls[0][1]['events']) == 1
    clock.t += dt.timedelta(days=1)
    app.camera_sync.sync()
    assert len(calls) == 1


def test_camera_api_settings_override_independently_and_hide_key(core):
    app, _, _ = core
    assert hasattr(app.cfg, 'camera_api')
    app.cfg.camera_api.url = 'https://script.google.com/macros/s/file/exec'
    app.cfg.camera_api.key = 'file-secret'
    # Capture this file baseline, as startup normally does.
    getattr(app.cfg, settings.BASELINE)['camera_api'].update(url=app.cfg.camera_api.url, key='file-secret')
    client = create_app(app).test_client()
    client.post('/settings', data={'camera_api.key': 'override-secret', 'schedule.work_days': ['0', '1', '2', '3', '4']})
    assert app.cfg.camera_api.key == 'override-secret'
    assert app.cfg.camera_api.url.endswith('/file/exec')
    html = client.get('/settings').get_data(as_text=True)
    assert 'override-secret' not in html and 'file-secret' not in html
    assert 'camera_api.url' in html and 'camera_api.key' in html
    assert settings.overrides(app.cfg, app.store)['camera_api.key'] == 'set'
    client.post('/settings', data={'camera_api.key': '', 'schedule.work_days': ['0', '1', '2', '3', '4']})
    assert app.cfg.camera_api.key == 'override-secret'
    client.post('/settings/reset', data={'reset': 'camera_api.key'})
    assert app.cfg.camera_api.key == 'file-secret'


def test_camera_api_rejects_invalid_url_without_saving(core):
    app, _, _ = core
    with pytest.raises(ValueError, match='camera_api.url'):
        settings.save(app.cfg, app.store, {'camera_api': {'url': 'file:///etc/passwd'}})


def test_camera_api_waits_for_base_snapshot(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result()])
    app.cfg.base_api_key = 'base-test-key'
    app.service.on_identified('A', at(10, 5), .9, None)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    assert not calls
    assert 'Waiting for HR' in app.camera_sync.status()['message']


def test_camera_api_batch_limit_and_mixed_results(core, monkeypatch):
    app, clock, _ = core
    from attendance.integrations import camera_api
    app.cfg.camera_api.url = 'https://script.google.com/macros/s/test/exec'
    app.cfg.camera_api.key = 'test'
    calls = []
    for i in range(201):
        person = app.store.add_employee(f'person{i}@example.com', f'Person {i}')
        app.store.record_checkin(DAY, person, at(10, 5), 'camera', .9, None)

    def post(url, payload):
        calls.append(payload)
        return {'ok': True, 'results': [dict(event, status='rejected' if event['email'] == 'person0@example.com'
                                           else 'created') for event in payload['events']]}

    monkeypatch.setattr(camera_api, 'post', post)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    app.camera_sync.sync()
    assert [len(call['events']) for call in calls] == [200, 1]
    assert not app.camera_sync.status()['ok']
    assert any('person0@example.com' in details for _, _, _, details in app.store.audit())


def test_camera_api_ping_uses_unsaved_fields_without_creating_fines(core, monkeypatch):
    app, _, _ = core
    calls = connect(app, monkeypatch, [{'ok': True}])
    client = create_app(app).test_client()
    client.post('/settings/test-camera-api', data={'camera_api.url': 'https://other.example/exec',
                                                  'camera_api.key': 'new-key'})
    assert calls == [('https://other.example/exec', {'action': 'camera.ping', 'secret': 'new-key'})]
    assert app.cfg.camera_api.key == 'camera-test-secret'


def test_camera_api_rejected_event_can_be_corrected(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result('rejected'), result('created', email='correct@example.com')])
    app.service.on_identified('A', at(10, 5), .9, None)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    app.store.update_employee('A', 'Alice', True, email='correct@example.com')
    app.camera_sync.sync()
    assert calls[-1][1]['events'][0]['email'] == 'correct@example.com'
    app.camera_sync.sync()
    assert len(calls) == 2


def test_camera_api_permanent_request_error_stops_retrying(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [{'ok': False, 'permanent': True}])
    app.service.on_identified('A', at(10, 5), .9, None)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    app.camera_sync.sync()
    assert len(calls) == 1
    assert not app.camera_sync.status()['ok']


def test_camera_api_http_redirect_uses_get():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from attendance.integrations.camera_api import post

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(('POST', json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            assert self.headers['Content-Type'] == 'text/plain;charset=utf-8'
            self.send_response(302)
            self.send_header('Location', '/result')
            self.end_headers()

        def do_GET(self):
            calls.append(('GET', self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert post(f'http://127.0.0.1:{server.server_port}/exec', {'action': 'camera.ping', 'secret': 'test'}) == {'ok': True}
        assert calls == [('POST', {'action': 'camera.ping', 'secret': 'test'}), ('GET', '/result')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_camera_api_respects_approved_base_permission(core, monkeypatch):
    from test_base_timeoff import connect as connect_base, item

    app, clock, _ = core
    clock.t = at(10, 59)
    connect_base(app, monkeypatch, [item(value='9:00-10:30')], approved=True)
    assert app.leave_sync.sync()['ok']
    calls = connect(app, monkeypatch, [result(email='carol@example.com')])
    app.service.on_identified('B', at(10, 20), .9, None)  # Approved until 10:30.
    app.service.on_identified('C', at(10, 15), .9, None)
    clock.t = at(11, 0)
    app.camera_sync.sync()
    assert len(calls) == 1
    assert [e['email'] for e in calls[0][1]['events']] == ['carol@example.com']


def test_camera_api_never_sends_expired_arrival(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [])
    app.service.on_identified('A', at(10, 5), .9, None)
    clock.t = at(11, 0, day=DAY + dt.timedelta(days=8))
    app.camera_sync.sync()
    assert not calls
