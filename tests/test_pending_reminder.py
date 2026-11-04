import datetime as dt

from attendance.integrations import base_timeoff
from conftest import DAY, at
from test_base_timeoff import connect, item


def row(username, group='1977', value='9:00-09:30', owners=('hanadang',)):
    return {**item(username, group, value), 'owners': [{'username': o, 'approved': 0} for o in owners]}


def sync(app, monkeypatch, rows, approved=False):
    connect(app, monkeypatch, rows, approved=approved)
    assert app.leave_sync.sync()['ok']


def test_parse_names_the_owners_that_have_not_approved():
    pending = {**row('bob'), 'owners': [{'username': 'Rose', 'approved': 1}, {'username': '@HanaDang', 'approved': 0}]}
    entry = base_timeoff.parse([pending], DAY, dt.time(10), approved=False)['bob'][0]
    assert entry['approvers'] == ['hanadang']
    done = base_timeoff.parse([pending], DAY, dt.time(10), approved=True)['bob'][0]
    assert 'approvers' not in done


def test_text_groups_requests_by_approver_and_filters_kinds(core, monkeypatch):
    app, _, _ = core
    sync(app, monkeypatch, [row('bob'), row('alice', value='9:00-13:00', owners=('rose',)), row('carol', group='1978', value='Full day')])
    text = app.service.pending_reminder_text(DAY)
    assert '@hanadang: 1 to review' in text and '• Bob: Coming late 9:00-09:30' in text
    assert '@rose: 1 to review' in text and '• Alice: Coming late 9:00-13:00' in text
    assert 'Carol' not in text  # WFH is not among the chosen types
    app.cfg.pending_reminder.kinds = '1977,1978'
    assert 'Carol' in app.service.pending_reminder_text(DAY)


def test_approved_requests_are_not_reminded(core, monkeypatch):
    app, _, _ = core
    sync(app, monkeypatch, [row('bob')], approved=True)
    assert app.service.pending_reminder_text(DAY) is None


def test_approver_who_is_a_known_person_is_named_by_name(core, monkeypatch):
    app, _, _ = core
    sync(app, monkeypatch, [row('bob', owners=('alice',))])
    assert '@Alice: 1 to review' in app.service.pending_reminder_text(DAY, preview=True)


def test_sent_once_a_day_at_the_chosen_time_and_by_hand(core, monkeypatch):
    app, clock, sent = core
    sync(app, monkeypatch, [row('bob')])
    clock.t = at(9, 20)
    app.service.tick()
    assert not [m for m in sent if m.startswith('⏳')]
    clock.t = at(9, 31)
    app.leave_sync.sync()
    app.service.tick()
    app.service.tick()
    assert len([m for m in sent if m.startswith('⏳')]) == 1
    assert app.service.send_pending_reminder(DAY)  # by hand: again
    assert len([m for m in sent if m.startswith('⏳')]) == 2


def test_nothing_pending_sends_nothing_and_off_switch(core, monkeypatch):
    app, clock, sent = core
    sync(app, monkeypatch, [row('bob')], approved=True)
    clock.t = at(9, 31)
    app.leave_sync.sync()
    app.service.tick()
    assert not [m for m in sent if m.startswith('⏳')]
    assert not app.service.send_pending_reminder(DAY)
    assert app.store.notice_sent(DAY, 'pending_reminder_skipped')


def test_button_and_settings_form(core, monkeypatch):
    from attendance.web import create_app
    app, clock, sent = core
    sync(app, monkeypatch, [row('bob')])
    c = create_app(app).test_client()
    page = c.get('/').get_data(as_text=True)
    assert 'Remind approvers' in page
    c.post(f'/day/{DAY}/remind-approvers')
    assert len([m for m in sent if m.startswith('⏳')]) == 1
    form = {'pending_reminder.kinds_shown': '1', 'pending_reminder.kinds': ['1977', '1978'],
            'pending_reminder.time': '10:15', 'pending_reminder.enabled': 'on'}
    c.post('/settings', data=form)
    assert app.cfg.pending_reminder.kinds == '1977,1978' and app.cfg.pending_reminder.time == '10:15'


def test_auto_send_off_leaves_only_the_button(core, monkeypatch):
    app, clock, sent = core
    sync(app, monkeypatch, [row('bob')])
    app.cfg.pending_reminder.auto_send = False
    clock.t = at(9, 31)
    app.leave_sync.sync()
    app.service.tick()
    assert not [m for m in sent if m.startswith('⏳')]
    assert app.service.send_pending_reminder(DAY)


def test_approver_is_mentioned_by_chat_id_with_plain_fallbacks(core, monkeypatch):
    app, _, _ = core
    sync(app, monkeypatch, [row('bob', owners=('alice',)), row('carol', owners=('alice',)), row('x', owners=('stranger',))])
    # Known person, no Chat ID yet: plain "@Name" (as the late message does).
    assert '@Alice: 2 to review' in app.service.pending_reminder_text(DAY)
    # With the Chat ID: a real mention.
    app.store.update_employee('A', 'Alice', True, chat_user='123')
    text = app.service.pending_reminder_text(DAY)
    assert '<users/123>: 2 to review' in text
    # Not in People at all: the Base username as plain text.
    assert '@stranger: 1 to review' in text
