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


@pytest.mark.parametrize('hour,minute', [(10, 30), (12, 0)])
def test_camera_api_uses_its_own_send_time_before_or_after_camera_end(core, monkeypatch, hour, minute):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result()])
    settings.save(app.cfg, app.store, {'camera_api': {'send_at': f'{hour:02d}:{minute:02d}'}})
    app.service.on_identified('A', at(10, 12), .9, None)
    clock.t = at(hour, minute) - dt.timedelta(seconds=1)
    waiting = app.camera_sync.sync()
    assert not calls
    assert f'{hour:02d}:{minute:02d}' in waiting['message']
    clock.t = at(hour, minute)
    assert app.camera_sync.sync()['ok'] is True
    app.camera_sync.sync()
    assert len(calls) == 1


@pytest.mark.parametrize('send_at', ['25:00', 'not-a-time', '09:30', '10:00'])
def test_camera_api_refuses_invalid_send_time_without_saving(core, send_at):
    app, _, _ = core
    before = app.store.get(settings.KEY)
    with pytest.raises(ValueError, match='camera_api.send_at'):
        settings.save(app.cfg, app.store, {'camera_api': {'send_at': send_at}})
    assert app.store.get(settings.KEY) == before


def test_camera_api_send_time_saved_on_page_survives_restart_and_can_follow_camera_end(core):
    from attendance.core.config import Config

    app, _, _ = core
    client = create_app(app).test_client()
    response = client.post('/settings', data={'camera_api.send_at': '12:30'}, follow_redirects=True)
    assert response.status_code == 200
    assert app.cfg.camera_api.send_at == '12:30'
    cfg = Config()
    settings.load(cfg, app.store)
    assert cfg.camera_api.send_at == '12:30'
    client.post('/settings', data={'camera_api.send_at': ''})
    assert app.cfg.camera_api.send_at == ''


def test_manual_camera_api_honors_saved_send_time(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result()])
    settings.save(app.cfg, app.store, {'camera_api': {'send_at': '12:30'}})
    app.service.on_identified('A', at(10, 12), .9, None)
    client = create_app(app).test_client()
    clock.t = at(11, 0)
    response = client.post('/settings/send-camera-api', follow_redirects=True)
    assert '12:30' in response.get_data(as_text=True)
    assert not calls
    clock.t = at(12, 30)
    client.post('/settings/send-camera-api')
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


def test_camera_api_partial_delivery_keeps_blocked_day_visible(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result()])
    previous = DAY - dt.timedelta(days=1)
    app.store.record_checkin(previous, 'B', at(10, 15, day=previous), 'camera', .9, None)
    app.store.record_checkin(DAY, 'A', at(10, 12), 'camera', .9, None)
    app.cfg.base_api_key = 'test-base-key'
    for person in app.store.all_employees():
        app.store.update_employee(person.id, person.name, True, base_username=person.id.lower())
    clock.t = at(11, 0)
    app.store.put(f'base_timeoffs:{DAY}', '{}')
    app.store.put('leave_sync', json.dumps({'ok': True, 'day': str(DAY), 'last_success': clock.t.isoformat()}))

    outcome = app.camera_sync.sync()
    assert len(calls) == 1
    assert app.store.notice_sent(DAY, 'camera_api:alice@example.com')
    assert not app.store.notice_sent(previous, 'camera_api:bob@example.com')
    assert outcome['ok'] is False
    assert '1 processed' in outcome['message'] and 'Waiting for HR' in outcome['message']
    assert str(previous) in outcome['message']
    assert app.camera_sync.status() == outcome


def test_camera_api_does_not_send_provisional_report_if_base_refreshes_during_report(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result()])
    app.cfg.base_api_key = 'test-base-key'
    for person in app.store.all_employees():
        app.store.update_employee(person.id, person.name, True, base_username=person.id.lower())
    app.store.record_checkin(DAY, 'A', at(10, 12), 'camera', .9, None)
    clock.t = at(11, 0)
    report = app.service.report

    def report_while_base_refreshes(day):
        provisional = report(day)
        if day == DAY:
            app.store.put(f'base_timeoffs:{DAY}', json.dumps({'a': [
                {'kind': 'Holiday', 'shift': 'Full day', 'approved': True, 'absence': True}]}))
            app.store.put('leave_sync', json.dumps({'ok': True, 'day': str(DAY),
                                                  'last_success': clock.t.isoformat()}))
        return provisional

    monkeypatch.setattr(app.service, 'report', report_while_base_refreshes)
    assert app.camera_sync.sync()['ok'] is False
    assert not calls
    assert app.camera_sync.sync()['ok'] is True
    assert not calls


@pytest.mark.parametrize('state', ['empty', 'on_time', 'delivered'])
def test_camera_api_does_not_require_base_without_unsent_events(core, monkeypatch, state):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [])
    app.cfg.base_api_key = 'test-base-key'
    clock.t = at(11, 0)
    if state != 'empty':
        app.store.record_checkin(DAY, 'A', at(9, 0) if state == 'on_time' else at(10, 12), 'camera', .9, None)
    if state == 'delivered':
        app.store.mark_notice(DAY, 'camera_api:alice@example.com', clock.t)

    outcome = app.camera_sync.sync()
    assert not calls
    assert outcome['ok'] is True
    assert 'No unsent late arrivals' in outcome['message']
    assert 'Waiting for HR' not in app.camera_sync.status().get('message', '')


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


def test_manual_camera_api_sends_saved_settings_and_does_not_resend(core, monkeypatch):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [result()])
    app.service.on_identified('A', at(10, 12), .9, None)
    clock.t = at(11, 0)
    client = create_app(app).test_client()
    response = client.post('/settings/send-camera-api', data={
        'camera_api.url': 'https://unsaved.example/exec', 'camera_api.key': 'unsaved-secret'},
        follow_redirects=True)
    assert response.status_code == 200
    assert '1 processed, 0 rejected' in response.get_data(as_text=True)
    assert calls[0][0] == 'https://script.google.com/macros/s/test/exec'
    assert calls[0][1] == {'action': 'camera.late', 'secret': 'camera-test-secret', 'events': [
        {'email': 'alice@example.com', 'date': DAY.isoformat(), 'arrivedAt': '10:12', 'lateMinutes': 12}]}
    again = client.post('/settings/send-camera-api', follow_redirects=True)
    assert 'No unsent late arrivals' in again.get_data(as_text=True)
    assert len(calls) == 1
    assert any(action == 'send camera API' for _, _, action, _ in app.store.audit())


@pytest.mark.parametrize('state,expected', [
    ('unconfigured', 'Save the Camera API URL and key'),
    ('before_end', 'after scheduled send time 11:00'),
    ('no_late', 'No unsent late arrivals'),
    ('missing_base', 'Waiting for HR leave data'),
    ('offline', 'Delivery failed'),
    ('rejected', '0 processed, 1 rejected'),
])
def test_manual_camera_api_explains_why_nothing_was_sent(core, monkeypatch, state, expected):
    app, clock, _ = core
    calls = connect(app, monkeypatch, [OSError('offline') if state == 'offline' else result('rejected')])
    clock.t = at(11, 0)
    if state != 'no_late':
        app.service.on_identified('A', at(10, 12), .9, None)
    if state == 'unconfigured':
        app.cfg.camera_api.key = ''
    elif state == 'before_end':
        clock.t = at(10, 30)
    elif state == 'missing_base':
        app.cfg.base_api_key = 'base-test-key'
    response = create_app(app).test_client().post('/settings/send-camera-api', follow_redirects=True)
    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)
    if state not in ('offline', 'rejected'):
        assert not calls
    else:
        assert not app.store.notice_sent(DAY, 'camera_api:alice@example.com')


def test_camera_api_serializes_manual_and_automatic_delivery(core, monkeypatch):
    app, clock, _ = core
    from attendance.integrations import camera_api
    connect(app, monkeypatch, [])
    app.service.on_identified('A', at(10, 12), .9, None)
    clock.t = at(11, 0)
    nested = []

    def post(url, payload):
        nested.append(app.camera_sync.sync())
        return result()

    monkeypatch.setattr(camera_api, 'post', post)
    assert app.camera_sync.sync()['ok'] is True
    assert len(nested) == 1 and nested[0]['ok'] is False
    assert 'already running' in nested[0]['message']


def test_manual_camera_api_is_admin_only(core, monkeypatch):
    app, _, _ = core
    calls = connect(app, monkeypatch, [])
    app.cfg.web.password = 'admin-password'
    app.cfg.web.viewer_password = 'viewer-password'
    app.cfg.web.viewer_username = 'viewer'
    client = create_app(app).test_client()
    assert client.post('/settings/send-camera-api').status_code == 401
    response = client.post('/settings/send-camera-api', auth=('viewer', 'viewer-password'))
    assert response.status_code == 403
    assert not calls


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
