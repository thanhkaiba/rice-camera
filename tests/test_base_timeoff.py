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


def connect(app, monkeypatch, rows, *, approved=False):
    app.cfg.base_api_key = 'secret'
    app.store.import_base_usernames('Email,Username\nbob@example.com,bob\nalice@example.com,alice\ncarol@example.com,carol\n')
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: base_timeoff.parse(rows, DAY, dt.time(10), approved=approved))
    monkeypatch.setattr(base_timeoff, 'fetch_upcoming', lambda *a: {})


def test_sync_excuses_late_but_still_requires_checkin(core, monkeypatch):
    app, clock, sent = core
    connect(app, monkeypatch, [item(value='9:00-10:30'), item('alice', '1967', 'Full day')], approved=True)
    assert app.leave_sync.sync()['ok']
    assert ('B', 'Bob') in app.service.report(DAY).not_seen
    clock.t = at(10, 20)
    app.service.on_identified('B', clock(), .8, None)
    rep = app.service.report(DAY)
    assert not rep.late
    assert any(e == 'B' and 'Coming late' in label for e, _, label in rep.on_leave)
    assert app.store.checkins(DAY)['B'].first_seen == clock()
    assert not app.service.late_people(DAY)
    assert not any('Bob' in m and 'min late' in m for m in sent)
    app.leave_sync.sync()


def test_failure_keeps_snapshot_and_warns_without_blocking_messages_then_recovers(core, monkeypatch):
    app, clock, sent = core
    connect(app, monkeypatch, [item()])
    assert app.leave_sync.sync()['ok']
    previous = app.store.get(f'base_timeoffs:{DAY}')
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: (_ for _ in ()).throw(ValueError('bad data')))
    assert not app.leave_sync.sync()['ok']
    assert app.store.get(f'base_timeoffs:{DAY}') == previous
    clock.t = at(11, 5)
    app.service.tick()
    # The warning is posted once, and the other messages still go out.
    assert app.store.notice_sent(DAY, 'base_deferred')
    assert any('Base Timeoff is unavailable' in m for m in sent)
    assert app.store.notice_sent(DAY, 'deadline')
    app.service.send_late_message(DAY)
    assert (app.store.notice_sent(DAY, 'late_message')
            or app.store.notice_sent(DAY, 'late_message_skipped'))
    assert app.store.get('cleanup_day') == DAY.isoformat()
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: {})
    assert app.leave_sync.sync()['ok']
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
    page = c.get('/?tab=coming_late').get_data(as_text=True)
    assert 'pending' in page and '10:20' in page
    exported = c.get(f'/report/details.csv?start={DAY}&end={DAY}').get_data(as_text=True)
    assert 'Coming late' in exported and '10:20' in exported


@pytest.mark.parametrize('approved', [False, True])
def test_person_detail_shows_own_timeoff_requests(core, monkeypatch, approved):
    from test_web import client
    app, _, _ = core
    connect(app, monkeypatch, [item(), item(group='1978', value='9:00-18:00'),
                               item(group='1967', value='9:00-13:00'),
                               item('alice', value='9:00-11:45')], approved=approved)
    assert app.leave_sync.sync()['ok']
    page = client(app).get('/employees/B').get_data(as_text=True)
    assert 'Timeoff requests' in page
    assert 'Coming late' in page and 'WFH' in page and 'Holiday' in page
    assert '9:00-09:30' in page and '9:00-18:00' in page and '9:00-13:00' in page
    assert ('Approved' if approved else 'Pending') in page
    assert '11:45' not in page


def test_person_detail_distinguishes_missing_sync_from_no_requests(core, monkeypatch):
    from test_web import client
    app, _, _ = core
    connect(app, monkeypatch, [])
    c = client(app)
    assert 'No synced request data for today.' in c.get('/employees/B').get_data(as_text=True)
    assert app.leave_sync.sync()['ok']
    assert 'No requests for today.' in c.get('/employees/B').get_data(as_text=True)


def test_fetch_upcoming_includes_future_afternoon_leave_and_exact_dates():
    tomorrow = DAY + dt.timedelta(days=1)
    end = DAY + dt.timedelta(days=60)
    calls = []
    rows = [item(group='1967', value='Afternoon', day=tomorrow),
            item(group='1978', value='9:00-18:00', day=end),
            item(group='1967', day=DAY), item(group='1967', day=end + dt.timedelta(days=1)),
            item(group='1977', day=tomorrow)]
    def post(form):
        calls.append(form)
        return {'code': 1, 'page': form['page'], 'total_items': len(rows), 'timeoffs': rows}
    result = base_timeoff.fetch_upcoming(tomorrow, end, 'secret', post=post)
    assert [(r['day'], r['kind'], r['shift'], r['approved']) for r in result['bob']] == [
        (str(tomorrow), 'Holiday', 'Afternoon', True),
        (str(tomorrow), 'Holiday', 'Afternoon', False),
        (str(end), 'WFH', '9:00-18:00', True),
        (str(end), 'WFH', '9:00-18:00', False)]
    assert all(f['start_date_to'] == end.strftime('%d/%m/%Y') and
               f['end_date_from'] == tomorrow.strftime('%d/%m/%Y') for f in calls)


def test_people_upcoming_leave_uses_base_and_preserves_cache_on_failure(core, monkeypatch, caplog):
    from attendance.integrations.leave_api import base_ready
    from test_web import client
    app, clock, _ = core
    connect(app, monkeypatch, [])
    tomorrow = DAY + dt.timedelta(days=1)
    app.store.add_leave('B', tomorrow, tomorrow, 'sick', 'manual-only-note')
    future = {'bob': [{'day': str(tomorrow), 'kind': 'Holiday', 'shift': 'Afternoon', 'approved': False, 'absence': True}],
              'alice': [{'day': str(tomorrow), 'kind': 'WFH', 'shift': 'Full day', 'approved': True, 'absence': True}]}
    def fetch(start, end, token):
        assert (start, end, token) == (tomorrow, DAY + dt.timedelta(days=60), 'secret')
        return future
    monkeypatch.setattr(base_timeoff, 'fetch_upcoming', fetch)
    assert app.leave_sync.sync()['ok']
    page = client(app).get('/employees/B').get_data(as_text=True)
    assert 'Holiday' in page and 'Afternoon' in page and 'Pending' in page
    assert 'manual-only-note' not in page and 'WFH' not in page
    assert app.service.report(DAY).on_leave == []
    def fail(*args):
        raise ValueError('secret')
    monkeypatch.setattr(base_timeoff, 'fetch_upcoming', fail)
    assert app.leave_sync.sync()['ok'] and base_ready(app.cfg, app.store, clock())
    page = client(app).get('/employees/B').get_data(as_text=True)
    assert 'Holiday' in page and 'Upcoming leave could not be refreshed from Base.' in page
    assert 'secret' not in page and 'secret' not in caplog.text
    clock.t = at(8, 0, day=tomorrow)
    assert 'Holiday' not in client(app).get('/employees/B').get_data(as_text=True)
    monkeypatch.setattr(base_timeoff, 'fetch_upcoming', lambda *a: {})
    assert app.leave_sync.sync()['ok']
    assert 'Holiday' not in client(app).get('/employees/B').get_data(as_text=True)


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
    assert 'Coming late' in page and 'pending' in page
    assert 'Late 20 min' in page


@pytest.mark.parametrize('approved', [False, True])
def test_today_separates_wfh_and_coming_late_requests(core, monkeypatch, approved):
    import re
    from test_web import client
    app, clock, _ = core
    connect(app, monkeypatch, [item(), item('alice', '1978', '9:00-18:00'),
                               item('carol', '1967', '9:00-18:00')], approved=approved)
    assert app.leave_sync.sync()['ok']
    clock.t = at(10, 20)
    app.service.on_identified('B', clock(), .8, None)
    c = client(app)
    page = c.get(f'/?day={DAY}').get_data(as_text=True)
    assert 'data-emp="C"' in page
    assert 'data-emp="A"' in page
    assert 'data-emp="B"' in page  # 10:20 is later than the approved 09:30 deadline too.
    ids = re.findall(r'data-emp="([^"]+)"', page)
    everyone = re.search(r'data-filter="all"><div class="n">(\d+)</div>', page)
    assert everyone and int(everyone[1]) == len(ids) == len(set(ids)) == 3
    for tab, employee, other in [('wfh', 'A', 'B'), ('coming_late', 'B', 'A')]:
        tile = re.search(r'data-filter="' + tab + r'"><div class="n">(\d+)</div>', page)
        assert tile and tile[1] == '1'
        members = re.findall(r'<tr[^>]*data-emp="([^"]+)"[^>]*data-requests="([^"]*)"', page)
        assert [emp for emp, groups in members if tab in groups.split()] == [employee]
    assert '10:20' in page


@pytest.mark.parametrize('approved,hour,minute,second,is_late,minutes', [
    (True, 9, 30, 0, False, 0),
    (True, 9, 30, 59, False, 0),
    (True, 9, 31, 0, True, 1),
    (False, 9, 20, 0, True, 20),
])
def test_coming_late_requires_approval_and_respects_requested_end(
        core, monkeypatch, approved, hour, minute, second, is_late, minutes):
    import csv
    import io
    from attendance.core.service import minutes_late
    from attendance.core.evidence import build
    from attendance.web import create_app
    from attendance.web.personal import token
    app, clock, sent = core
    app.cfg.schedule.late_after = '09:00'
    connect(app, monkeypatch, [item()], approved=approved)
    clock.t = at(hour, minute, second)
    assert app.leave_sync.sync()['ok']
    app.service.on_identified('B', clock(), .8, None)
    rep = app.service.report(DAY)
    assert bool(rep.late) == is_late
    assert bool(app.service.late_people(DAY)) == is_late
    assert any('Bob' in m and 'min late' in m for m in sent) == is_late
    if is_late:
        assert minutes_late(rep.late[0], rep.window) == minutes
    else:
        assert any(e == 'B' for e, _, _ in rep.on_leave)
    web = create_app(app)
    c = web.test_client()
    page = c.get('/?tab=coming_late').get_data(as_text=True)
    assert ('pending' in page) == (not approved)
    assert (f'Late {minutes} min' in page) == is_late
    sig = token(app.store, web.secret_key, 'B', create=True)
    personal = c.get(f'/p/B/{sig}').get_data(as_text=True)
    assert (f'Late {minutes} min' in personal) == is_late
    exported = c.get(f'/report/details.csv?start={DAY}&end={DAY}').get_data(as_text=True)
    bob = next(row for row in csv.DictReader(io.StringIO(exported)) if row['email'] == 'bob@example.com')
    assert bob['status'] == ('late' if is_late else 'on_leave')
    if is_late:
        assert bob['minutes_late'] == str(minutes)
    assert ('pending' in str(bob)) == (not approved)
    assert ('pending' in build(app.service, DAY, 'B').status) == (not approved)


@pytest.mark.parametrize('group', ['1978', '1967'])
def test_pending_absence_does_not_excuse_attendance(core, monkeypatch, group):
    app, clock, _ = core
    connect(app, monkeypatch, [item(group=group, value='9:00-18:00')])
    assert app.leave_sync.sync()['ok']
    assert ('B', 'Bob') in app.service.report(DAY).not_seen
    clock.t = at(10, 20)
    app.service.on_identified('B', clock(), .8, None)
    assert [a.employee_id for a in app.service.report(DAY).late] == ['B']


@pytest.mark.parametrize('reverse', [False, True])
def test_multiple_requests_use_latest_approved_deadline(core, monkeypatch, reverse):
    from attendance.core.service import minutes_late
    app, clock, _ = core
    connect(app, monkeypatch, [])
    rows = [item(value='9:00-10:30'), item(value='9:00-09:30')]
    approved = base_timeoff.parse(rows[::-1] if reverse else rows, DAY, dt.time(10), approved=True)
    approved['bob'] += base_timeoff.parse([item(value='9:00-11:30')], DAY, dt.time(10), approved=False)['bob']
    monkeypatch.setattr(base_timeoff, 'fetch', lambda *a: approved)
    clock.t = at(10, 45)
    assert app.leave_sync.sync()['ok']
    app.service.on_identified('B', clock(), .8, None)
    rep = app.service.report(DAY)
    assert [a.employee_id for a in rep.late] == ['B']
    assert minutes_late(rep.late[0], rep.window) == 15


def test_late_after_a_coming_late_permission_shows_the_registered_interval(core, monkeypatch):
    """Bob may come at 09:30 (Base, approved) but comes at 10:15: the message and the picture
    say what he registered and when he arrived."""
    from test_evidence import seen

    from attendance.core import latephoto

    app, clock, _ = core
    connect(app, monkeypatch, [item(value='9:00-09:30')], approved=True)
    assert app.leave_sync.sync()['ok']
    clock.t = at(10, 40)
    seen(app, 'B', at(10, 15))
    seen(app, 'C', at(10, 32))  # no permission: the line stays as before
    rep = app.service.report(DAY)
    assert [(a.employee_id, a.registered) for a in rep.late] == [('B', '9:00-09:30'), ('C', '')]
    assert app.service.late_text(DAY, preview=True) == \
        'Morning, coming late today:\nLate arrivals (2):\n• @Bob (10:15, registered 9:00-09:30)\n• @Carol (10:32)\n\nNot seen (1):\n• @Alice'
    app.cfg.late_message.item = '{mention} {minutes} min{registered}'
    assert app.service.late_text(DAY, preview=True) == \
        'Morning, coming late today:\nLate arrivals (2):\n• @Bob 15 min, registered 9:00-09:30\n• @Carol 32 min\n\nNot seen (1):\n• @Alice'  # late time 10:00

    people = [latephoto.Person('Bob', '10:15', 45, None, '9:00-09:30'), latephoto.Person('Carol', '10:32', 32, None)]
    assert people[0].lines() == ['Registered 9:00-09:30', 'Arrived 10:15 (+45 min)']
    assert people[1].lines() == ['10:32  +32 min']
    assert latephoto.fingerprint(people, None) != latephoto.fingerprint(
        [latephoto.Person('Bob', '10:15', 45, None), people[1]], None)
    rel = app.service.late_photo(DAY)
    assert rel and (app.data_dir / rel).exists()


def test_late_with_a_pending_coming_late_request_says_pending(core, monkeypatch):
    """Bob asked on Base to come at 09:30 but nobody approved it yet; he comes at 10:15. The
    message and the picture say the request is pending; the minutes count from the usual
    late time, since nothing was granted."""
    from attendance.core import latephoto
    from attendance.integrations.base_timeoff import pending_arrivals

    app, clock, _ = core
    connect(app, monkeypatch, [item(value='9:00-09:30'), item('alice', '1967', 'Full day')], approved=False)
    assert app.leave_sync.sync()['ok']
    assert pending_arrivals(app.store, DAY) == {'B': '9:00-09:30'}  # Alice's absence request is not an arrival
    clock.t = at(10, 40)
    app.service.on_identified('B', at(10, 15), .8, None)
    rep = app.service.report(DAY)
    assert [(a.employee_id, a.registered, a.pending) for a in rep.late] == [('B', '9:00-09:30', True)]
    assert app.service.late_text(DAY, preview=True) == 'Morning, coming late today:\nLate arrivals (1):\n• @Bob (10:15, request pending 9:00-09:30)\n\nNot seen (2):\n• @Alice\n• @Carol'
    person = latephoto.Person('Bob', '10:15', 15, None, '9:00-09:30', pending=True)
    assert person.lines() == ['Request pending 9:00-09:30', 'Arrived 10:15 (+15 min)']
    assert latephoto.fingerprint([person], None) != latephoto.fingerprint(
        [latephoto.Person('Bob', '10:15', 15, None, '9:00-09:30')], None)
