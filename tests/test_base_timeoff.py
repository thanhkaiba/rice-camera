import datetime as dt
import json

import pytest

from attendance.integrations import base_timeoff
from attendance.store import Store
from conftest import DAY, at


def item(username='bob', group='1977', value='9:00-09:30', day=DAY):
    return {'username': username, 'group_id': group, 'approvals': [],
            'shifts': [{'datetime': int(at(0, 0, day=day).timestamp()), 'shifts': [{'value': value}]}]}


def test_csv_mapping_matches_email_without_changing_people(core):
    app, _, _ = core
    result = app.store.import_base_usernames('Email,Username,Họ và tên\nBOB@example.com,@Bob,Other\nx@example.com,x,X\n')
    assert result == (1, ['x@example.com'])
    assert app.store.employee('B').base_username == 'bob'
    assert app.store.employee('B').name == 'Bob'
    assert app.store.employee_count() == 3
    app.store.update_employee('B', 'Bob', True)
    assert app.store.employee('B').base_username == 'bob'
    with pytest.raises(ValueError):
        app.store.import_base_usernames('Email,Username\nalice@example.com,bob\n')
    assert app.store.employee('A').base_username == ''


def test_csv_duplicate_fails_atomically(core):
    app, _, _ = core
    with pytest.raises(ValueError):
        app.store.import_base_usernames('Email,Username\nalice@example.com,alice\nbob@example.com,alice\n')
    assert app.store.employee('A').base_username == ''


def test_parser_exact_day_timezone_pending_and_morning():
    rows = [item(), item('alice', '1967', '9:00-13:00'), item('carol', '1978', '14:00-18:00'),
            item('other', day=DAY + dt.timedelta(days=1)), item('early', '1975')]
    parsed = base_timeoff.parse(rows, DAY, dt.time(10, 0), approved=False)
    assert set(parsed) == {'bob', 'alice'}
    assert not parsed['bob'][0]['absence']
    assert parsed['alice'][0]['absence']
    assert not parsed['bob'][0]['approved']


def test_api_pagination_filters_and_rejection():
    calls = []
    def post(form):
        calls.append(form)
        page = int(form['page'])
        return {'code': 1, 'page': page, 'total_items': '2', 'items_per_page': 1,
                'timeoffs': [item('bob' if page == 0 else 'alice')]}
    result = base_timeoff.fetch(DAY, dt.time(10), 'secret', post=post)
    assert set(result) == {'bob', 'alice'}
    assert [c['page'] for c in calls] == [0, 1, 0, 1]
    assert {c['s'] for c in calls} == {'approved', 'pending'}
    assert all(c['start_date_to'] == c['end_date_from'] == DAY.strftime('%d/%m/%Y') for c in calls)
    with pytest.raises(ValueError):
        base_timeoff.fetch(DAY, dt.time(10), 'secret', post=lambda f: {'code': 0, 'message': 'invalid'})


def connect(app, monkeypatch, rows):
    app.cfg.base_api_key = 'secret'
    app.store.import_base_usernames('Email,Username\nbob@example.com,bob\nalice@example.com,alice\ncarol@example.com,carol\n')
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: base_timeoff.parse(rows, DAY, dt.time(10), approved=False))


def test_sync_excuses_late_but_still_requires_checkin(core, monkeypatch):
    app, clock, sent = core
    connect(app, monkeypatch, [item(), item('alice', '1967', 'Full day')])
    assert app.leave_sync.sync()['ok']
    assert ('B', 'Bob') in app.service.report(DAY).not_seen
    clock.t = at(10, 20)
    app.service.on_identified('B', clock(), .8, None)
    rep = app.service.report(DAY)
    assert not rep.late
    assert any(e == 'B' and 'pending' in label.lower() for e, _, label in rep.on_leave)
    assert app.store.checkins(DAY)['B'].first_seen == clock()
    assert not app.service.late_people(DAY)
    assert not any('Bob' in m and 'min late' in m for m in sent)
    app.leave_sync.sync()


def test_failure_keeps_snapshot_and_defers_messages_then_recovers(core, monkeypatch):
    app, clock, sent = core
    connect(app, monkeypatch, [item()])
    assert app.leave_sync.sync()['ok']
    previous = app.store.get(f'base_timeoffs:{DAY}')
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: (_ for _ in ()).throw(ValueError('bad data')))
    assert not app.leave_sync.sync()['ok']
    assert app.store.get(f'base_timeoffs:{DAY}') == previous
    clock.t = at(11, 5)
    app.service.tick()
    assert not app.store.notice_sent(DAY, 'deadline')
    assert not app.store.notice_sent(DAY, 'summary')
    assert not app.service.send_late_message(DAY)
    assert not app.store.notice_sent(DAY, 'late_message_skipped')
    assert app.store.get('cleanup_day') == DAY.isoformat()
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: {})
    assert app.leave_sync.sync()['ok']
    app.service.tick()
    assert app.store.notice_sent(DAY, 'summary')
    assert app.store.get(f'base_timeoffs:{DAY}') != previous


def test_weekends_not_due_and_secret_not_in_diagnostics(core):
    from attendance.core.diagnostics import redact, redact_settings
    app, clock, _ = core
    app.cfg.base_api_key = 'secret'
    clock.t = at(8, 0, day=dt.date(2026, 10, 17))
    assert not app.leave_sync.due()
    assert 'secret' not in redact('base_api_key: secret\naccess_token_v2=secret')
    assert redact_settings({'base_api_key': 'secret'})['base_api_key'] != 'secret'


def test_all_shift_values_are_checked_and_unknown_group_warns(caplog):
    row = item(group='9999', value='Afternoon')
    row['shifts'][0]['shifts'].append({'value': '9:00-13:00'})
    result = base_timeoff.parse([row], DAY, dt.time(10), approved=True)
    assert result['bob'][0]['absence']
    assert '9999' in caplog.text
    with pytest.raises(ValueError):
        base_timeoff.parse([item(value='Unrecognized shift')], DAY, dt.time(10), approved=True)


def test_noncontinuous_dates_and_failed_second_page():
    row = item(day=DAY - dt.timedelta(days=1))
    row['shifts'] += item(day=DAY + dt.timedelta(days=1))['shifts']
    assert not base_timeoff.parse([row], DAY, dt.time(10), approved=True)
    def repeated(form):
        return {'code': 1, 'page': form['page'], 'total_items': '3', 'timeoffs': [item()]}
    with pytest.raises(ValueError, match='repeated'):
        base_timeoff.fetch(DAY, dt.time(10), 'secret', post=repeated)


def test_missing_mapping_blocks_final_messages(core, monkeypatch):
    from attendance.integrations.leave_api import base_ready
    app, clock, _ = core
    connect(app, monkeypatch, [])
    assert app.leave_sync.sync()['ok']
    assert base_ready(app.cfg, app.store, clock())
    app.store.update_employee('C', 'Carol', True, base_username='')
    assert not base_ready(app.cfg, app.store, clock())


def test_username_survives_legacy_csv_and_restart(core):
    app, _, _ = core
    app.store.import_base_usernames('Email,Username\nbob@example.com,bob\n')
    app.store.import_employees('email,name\nbob@example.com,Bob\n')
    with pytest.raises(ValueError, match='already exists'):
        app.store.update_employee('A', 'Alice', True, base_username='bob')
    reopened = Store(app.data_dir / 'attendance.db')
    try:
        assert reopened.employee('B').base_username == 'bob'
    finally:
        reopened.close()


def test_employee_csv_imports_base_username_and_rejects_conflicts_atomically(core):
    app, _, _ = core
    app.store.import_employees('email,name,base_username\nbob@example.com,Bob,@Bob\n')
    assert app.store.employee('B').base_username == 'bob'
    with pytest.raises(ValueError):
        app.store.import_employees('email,name,base_username\nalice@example.com,Changed,alice\ncarol@example.com,Carol,bob\n')
    assert app.store.employee('A').name == 'Alice'
    assert app.store.employee('A').base_username == ''
    assert app.store.employee('C').base_username == ''


def test_mapping_import_accepts_canonical_employee_csv(core):
    app, _, _ = core
    assert app.store.import_base_usernames('email,name,base_username\nbob@example.com,Bob,bob\n') == (1, [])
    assert app.store.employee('B').base_username == 'bob'


def test_settings_saves_base_token_keeps_blank_and_hides_secret(core):
    from attendance.core import settings
    from attendance.core.config import Config
    from test_web import client
    app, _, _ = core
    c = client(app)
    form = {'base_api_key': 'base-test-secret-123', 'schedule.work_days': ['0', '1', '2', '3', '4']}
    page = c.post('/settings', data=form, follow_redirects=True).get_data(as_text=True)
    assert app.cfg.base_api_key == 'base-test-secret-123'
    assert app.leave_sync.enabled
    assert 'base-test-secret-123' not in page
    assert 'name="base_api_key" type="password"' in page
    assert 'base-test-secret-123' not in str(app.store.audit())
    c.post('/settings', data={**form, 'base_api_key': ''})
    assert app.cfg.base_api_key == 'base-test-secret-123'
    restarted = Config()
    settings.load(restarted, app.store)
    assert restarted.base_api_key == 'base-test-secret-123'
    assert settings.overrides(app.cfg, app.store)['base_api_key'] == 'empty'


def test_settings_does_not_expose_base_token_from_config(core):
    from attendance.core import settings
    from test_web import client
    app, _, _ = core
    assert 'base_api_key' in settings.EDITABLE['']
    app.cfg.base_api_key = 'file-base-secret'
    page = client(app).get('/settings').get_data(as_text=True)
    assert 'file-base-secret' not in page
    assert 'saved · leave empty to keep' in page


def test_mapping_upload_and_permission_display(core, monkeypatch):
    import io
    from test_web import client
    app, clock, _ = core
    c = client(app)
    response = c.post('/employees/base-usernames', data={
        'file': (io.BytesIO(b'Email,Username\nbob@example.com,bob\n'), 'base.csv')})
    assert response.status_code == 302
    assert 'bob' in c.get('/employees/B').get_data(as_text=True)
    connect(app, monkeypatch, [item()])
    assert app.leave_sync.sync()['ok']
    clock.t = at(10, 20)
    app.service.on_identified('B', clock(), .8, None)
    page = c.get('/').get_data(as_text=True)
    assert 'pending approval' in page and '10:20' in page
    exported = c.get(f'/report/details.csv?start={DAY}&end={DAY}').get_data(as_text=True)
    assert 'Coming late' in exported and '10:20' in exported


def test_personal_page_also_honors_base_permission(core, monkeypatch):
    from attendance.web import create_app
    from attendance.web.personal import token
    app, clock, _ = core
    connect(app, monkeypatch, [item()])
    assert app.leave_sync.sync()['ok']
    clock.t = at(10, 20)
    app.service.on_identified('B', clock(), .8, None)
    web = create_app(app)
    sig = token(app.store, web.secret_key, 'B', create=True)
    page = web.test_client().get(f'/p/B/{sig}').get_data(as_text=True)
    assert 'Coming late' in page and 'pending approval' in page
    assert 'Late 20 min' not in page
