import io
import base64
import zipfile

import numpy as np
import pytest

from attendance.web import create_app
from test_edge import admin, follower, live  # noqa: F401


def pack(**changes):
    arrays = {"voice_emb": np.ones((1, 10, 768), dtype=np.float32), "n_voice_queries": np.array(10)}
    arrays.update(changes)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


@pytest.fixture
def upload(core):
    app, _, _ = core
    return app, create_app(app).test_client()


def send(client, data=None, name="office/voice.npz", **kwargs):
    return client.post('/settings/greeting/voice', data={
        'voice_files': [(io.BytesIO(pack() if data is None else data), name),
                        (io.BytesIO(b'ignored preview'), 'office/preview.wav')]},
        headers={"X-Requested-With": "fetch"}, **kwargs)


def test_uploading_a_folder_installs_and_selects_its_voice(upload):
    app, client = upload
    response = send(client)
    assert response.status_code == 200
    target = app.cfg.path('data/voices/office/voice.npz')
    with np.load(target, allow_pickle=False) as voice:
        np.testing.assert_array_equal(voice['voice_emb'], np.ones((1, 10, 768), dtype=np.float32))
    assert list(target.parent.iterdir()) == [target]
    assert app.cfg.greeting.engine == 'zerotts'
    assert app.cfg.greeting.zerotts_voice == 'office'
    assert app.cfg.greeting.zerotts_voices_dir == 'data/voices'
    page = client.get('/settings').get_data(as_text=True)
    assert 'webkitdirectory' in page and 'name="voice_files"' in page


@pytest.mark.parametrize('data,name', [
    (b'not a voice', 'office/voice.npz'),
    (pack(voice_emb=np.zeros((1, 9, 768), dtype=np.float32)), 'office/voice.npz'),
    (pack(voice_emb=np.full((1, 10, 768), np.nan, dtype=np.float32)), 'office/voice.npz'),
    (pack(voice_emb=np.ones((1, 10, 768), dtype=object)), 'office/voice.npz'),
    (pack(), '../outside/voice.npz'),
    (pack(), '/outside/voice.npz'),
    (pack(), 'voice.npz'),
    (pack(extra=np.zeros(1024 * 1024, dtype=np.uint8)), 'office/voice.npz'),
], ids=['invalid', 'shape', 'nan', 'pickle', 'traversal', 'absolute', 'no-folder', 'expanded-size'])
def test_invalid_uploads_do_not_change_files_or_settings(upload, data, name):
    app, client = upload
    before = app.cfg.greeting.zerotts_voice
    response = send(client, data, name)
    assert response.status_code == 400
    assert app.cfg.greeting.zerotts_voice == before
    assert not list(app.cfg.path('data/voices').glob('*/voice.npz'))


def test_existing_voice_is_not_overwritten(upload):
    app, client = upload
    assert send(client).status_code == 200
    target = app.cfg.path('data/voices/office/voice.npz')
    before = target.read_bytes()
    assert send(client, pack(voice_emb=np.zeros((1, 10, 768), dtype=np.float32))).status_code == 400
    assert target.read_bytes() == before


def test_requires_one_voice_file_and_limits_upload_size(upload):
    _, client = upload
    assert client.post('/settings/greeting/voice', headers={"X-Requested-With": "fetch"}).status_code == 400
    response = client.post('/settings/greeting/voice', data={'voice_files': [
        (io.BytesIO(pack()), 'a/voice.npz'), (io.BytesIO(pack()), 'b/voice.npz')]},
        headers={"X-Requested-With": "fetch"})
    assert response.status_code == 400
    assert send(client, b'x' * (10 * 1024 * 1024)).status_code == 413


def test_server_upload_queues_a_transfer_to_the_camera(upload):
    app, client = upload
    app.photos_read_elsewhere = True
    assert send(client).status_code == 200
    jobs = app.store.edge_jobs()
    assert len(jobs) == 1 and jobs[0]['kind'] == 'add_voice'
    assert jobs[0]['payload']['voice'] == 'office'
    assert (app.data_dir / jobs[0]['payload']['image']).is_file()
    assert app.cfg.greeting.zerotts_voice == 'maichi'


def test_upload_requires_admin(upload):
    app, client = upload
    app.cfg.web.password = 'admin-password'
    assert send(client).status_code == 401
    app.cfg.web.viewer_password = 'viewer-password'
    credentials = base64.b64encode(b'viewer:viewer-password').decode()
    response = client.post('/settings/greeting/voice', headers={'Authorization': 'Basic ' + credentials})
    assert response.status_code == 403


def test_array_headers_cannot_request_unbounded_memory(upload):
    _, client = upload
    header, archive = io.BytesIO(), io.BytesIO()
    np.lib.format.write_array_header_1_0(header, {'descr': '<f4', 'fortran_order': False,
                                                'shape': (1, 10**12, 768)})
    with zipfile.ZipFile(io.BytesIO(pack())) as valid:
        queries = valid.read('n_voice_queries.npy')
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('voice_emb.npy', header.getvalue())
        output.writestr('n_voice_queries.npy', queries)
    assert send(client, archive.getvalue()).status_code == 400


def test_uploaded_voice_reaches_the_camera_and_closes_its_job(live, tmp_path):
    from attendance.core.voice import install_voice

    _, app, client = live
    response = client.post('/settings/greeting/voice', data={
        'voice_files': [(io.BytesIO(pack()), 'office/voice.npz')]}, headers=admin())
    assert response.status_code == 302
    job = app.store.edge_jobs()[0]
    _, edge = follower(live, tmp_path)
    configs = []
    edge.on_config = configs.append
    root = tmp_path / 'camera-voices'
    edge.add_voice = lambda name, data: install_voice(root, name, data)
    edge.follow()
    assert (root / 'office/voice.npz').is_file()
    assert configs[0]['greeting']['zerotts_voice'] == 'maichi'
    edge.follow()
    assert configs[-1]['greeting']['zerotts_voice'] == 'office'
    assert app.store.edge_jobs() == []
    assert not (app.data_dir / job['payload']['image']).exists()
    assert any(action == 'installed voice' and 'office: installed' in detail
               for _, _, action, detail in app.store.audit())


def test_failed_camera_installation_keeps_the_previous_voice(live, tmp_path):
    from attendance.core.voice import install_voice

    _, app, client = live
    root = tmp_path / 'camera-voices'
    previous = install_voice(root, 'office', pack(voice_emb=np.zeros((1, 10, 768), dtype=np.float32)))
    original = previous.read_bytes()
    response = client.post('/settings/greeting/voice', data={
        'voice_files': [(io.BytesIO(pack()), 'office/voice.npz')]}, headers=admin())
    assert response.status_code == 302
    _, edge = follower(live, tmp_path)
    edge.add_voice = lambda name, data: install_voice(root, name, data)
    edge.follow()
    assert app.cfg.greeting.zerotts_voice == 'maichi'
    assert previous.read_bytes() == original
    assert any(action == 'installed voice' and 'office: failed:' in detail
               for _, _, action, detail in app.store.audit())
