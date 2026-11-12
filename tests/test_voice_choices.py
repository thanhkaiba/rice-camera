import datetime as dt

import pytest

from attendance.core import greeting, settings
from attendance.core.voice import check_voice_name, voice_names
from attendance.web import create_app
from attendance.web.edge_api import _greeting_status
from conftest import at


def add_pack(cfg, folder, name):
    path = cfg.path(folder) / name / 'voice.npz'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_picker_discovers_custom_folder_deduplicates_and_keeps_missing_selection(core):
    app, _, _ = core
    app.cfg.greeting.zerotts_voices_dir = 'my-voices'
    app.cfg.greeting.zerotts_voice = 'unavailable'
    add_pack(app.cfg, 'models/ZeroTTS/voices', 'maichi')
    add_pack(app.cfg, 'my-voices', 'maichi')
    add_pack(app.cfg, 'my-voices', 'adam')
    add_pack(app.cfg, 'other-folder', 'not-listed')
    app.cfg.path('my-voices/empty').mkdir()
    assert voice_names(app.cfg) == ['adam', 'maichi']
    page = create_app(app).test_client().get('/settings').text
    assert '<select name="greeting.zerotts_voice">' in page
    assert page.count('value="maichi"') == 1
    assert 'value="adam"' in page and 'value="baotrang"' in page
    assert 'value="auto"' in page and 'value="unavailable" selected' in page
    assert 'value="not-listed"' not in page and 'value="empty"' not in page


def test_auto_keeps_one_voice_per_local_day_across_restart_and_rotates(core):
    app, clock, _ = core
    settings.save(app.cfg, app.store, {'greeting': {'engine': 'zerotts', 'zerotts_voice': 'auto',
                                                  'zerotts_voices_dir': 'my-voices'}})
    for name in ['adam', 'lam', 'mark']:
        add_pack(app.cfg, 'my-voices', name)
    g = app.greeter
    clock.t = at(23, 59)
    first = g.voice_for('vi')
    assert first == 'adam' and g.voice_for('vi') == first
    restarted = greeting.Greeter(app.cfg, app.store, clock, g.late_from)
    assert restarted.voice_for('vi') == first
    # Discovering another pack mid-day must not change today's voice.
    add_pack(app.cfg, 'my-voices', 'a-new-voice')
    assert restarted.voice_for('vi') == first
    clock.t += dt.timedelta(minutes=2)
    assert restarted.voice_for('vi') == 'lam'
    assert restarted.voice_for('en') == app.cfg.greeting.voice_en
    for expected in ['mark', 'a-new-voice', 'adam']:
        clock.t += dt.timedelta(days=1)
        assert restarted.voice_for('vi') == expected


def test_auto_handles_no_packs_one_pack_and_removed_pack(core):
    app, clock, _ = core
    app.cfg.greeting.engine = 'zerotts'
    app.cfg.greeting.zerotts_voice = 'auto'
    g = app.greeter
    assert g.voice_for('vi') == '' and not g.voice_ready()
    pack = add_pack(app.cfg, 'models/ZeroTTS/voices', 'maichi')
    assert g.voice_for('vi') == 'maichi'
    clock.t += dt.timedelta(days=1)
    assert g.voice_for('vi') == 'maichi'
    add_pack(app.cfg, 'models/ZeroTTS/voices', 'lam')
    pack.unlink()
    assert g.voice_for('vi') == 'lam'


def test_auto_is_reserved_for_rotation():
    with pytest.raises(ValueError, match='reserved'):
        check_voice_name('auto')


def test_remote_camera_voice_names_are_sanitized_and_shown_in_picker(core):
    app, _, _ = core
    app.photos_read_elsewhere = True
    state = _greeting_status({'voices': ['camera-only', '../unsafe', None, 'camera-only', 'auto']})
    assert state['voices'] == ['camera-only']
    import json
    from attendance.web.edge_api import HEARTBEAT_KEY
    app.store.put(HEARTBEAT_KEY, json.dumps({'greeting': state}))
    page = create_app(app).test_client().get('/settings').text
    assert 'value="camera-only"' in page
    assert app._edge_status()['greeting']['voices'] == []


def test_remote_unconfirmed_voice_is_disabled_and_cannot_be_saved(core):
    import json
    from attendance.web.edge_api import HEARTBEAT_KEY
    app, _, _ = core
    app.photos_read_elsewhere = True
    app.cfg.greeting.zerotts_voices_dir = 'data/voices'
    add_pack(app.cfg, 'data/voices', 'server-only')
    app.store.put(HEARTBEAT_KEY, json.dumps({'greeting': _greeting_status({'voices': ['camera-only']})}))
    client = create_app(app).test_client()
    import re
    page = client.get('/settings').text
    option = re.search(r'<option[^>]*value="server-only"[^>]*>', page).group()
    assert 'disabled' in option
    assert 'disabled' not in re.search(r'<option[^>]*value="camera-only"[^>]*>', page).group()
    assert 'disabled' not in re.search(r'<option[^>]*value="maichi"[^>]*>', page).group()
    before = app.cfg.greeting.zerotts_voice
    response = client.post('/settings', data={'greeting.zerotts_voice': 'server-only'}, follow_redirects=True)
    assert app.cfg.greeting.zerotts_voice == before
    assert 'has not been confirmed' in response.text
    # A later heartbeat confirms installation and unlocks selection.
    app.store.put(HEARTBEAT_KEY, json.dumps({'greeting': _greeting_status({'voices': ['server-only']})}))
    response = client.post('/settings', data={'greeting.zerotts_voice': 'server-only'})
    assert response.status_code == 302 and app.cfg.greeting.zerotts_voice == 'server-only'


def test_camera_reports_daily_auto_voice_and_heartbeat_keeps_it(core):
    app, _, _ = core
    app.cfg.greeting.engine = 'zerotts'
    add_pack(app.cfg, 'models/ZeroTTS/voices', 'adam')
    state = app._edge_status()['greeting']
    assert state['daily_voice']['name'] == 'adam'
    assert _greeting_status(state)['daily_voice'] == state['daily_voice']
    for bad in [{'name': '../bad', 'at': at(8, 0).isoformat()},
                {'name': 'adam', 'at': 'bad'}, {'name': 'adam', 'at': '2026-10-14T08:00:00'}]:
        assert 'daily_voice' not in _greeting_status({'voices': ['adam'], 'daily_voice': bad})
