import csv
import hashlib
import json
import sqlite3

import pytest
import numpy as np

from attendance.store import Store
from conftest import DAY, at, face_image, person_vector
from test_web import client


def sample(app, vector=None):
    return app.store.add_unknown(DAY, at(9, 0), None, app.service.model_id, vector,
                                 None, None, source="seed")


def test_seed_review_confirmation_learns_without_attendance_or_chat(core):
    app, clock, sent = core
    clock.t = at(9, 10)
    uid = sample(app, person_vector(20))
    before = len(app.store.references(app.library.model_id))
    response = client(app).post(f'/unknowns/{uid}/assign', data={'employee_id': 'A'}, follow_redirects=True)
    assert response.status_code == 200
    assert app.store.unknown(uid).employee_id == 'A'
    assert len(app.store.references(app.library.model_id)) == before + 1
    assert not app.store.checkins(DAY) and not app.store.sightings(DAY) and not sent


def test_seed_samples_are_visible_but_excluded_from_live_counts_and_notifications(core):
    app, _, _ = core
    uid = sample(app)
    assert app.store.pending_group_count() == 1
    assert app.store.pending_group_count(DAY) == 0
    assert app.store.unknown_counts(DAY, DAY) == {}
    assert app.store.unnotified_unknowns(DAY) == []
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert f'data-id="{uid}"' in page and 'Seed sample' in page
    assert 'No attendance' in page


def test_new_employee_from_undetectable_seed_does_not_claim_a_face_was_learned(core):
    app, _, sent = core
    uid = sample(app)
    page = client(app).post(f'/unknowns/{uid}/new-employee',
                           data={'email':'new@example.com','name':'New'}, follow_redirects=True).get_data(as_text=True)
    assert 'sample labelled' in page and 'No attendance' in page
    assert 'their face learned' not in page
    person = app.store.employee_by_email('new@example.com')
    assert not app.store.references(app.library.model_id,person.id)
    assert not app.store.checkins(DAY) and not sent


def test_live_camera_never_joins_a_seed_sample_group(core):
    app, _, _ = core
    uid = sample(app, person_vector(20))
    live = app.service.on_unknown(at(9, 1), None, person_vector(20), None, None)
    assert app.store.unknown(live).group_id != app.store.unknown(uid).group_id
    assert app.store.unknown(live).source == 'camera'


def test_camera_notification_link_opens_camera_face_instead_of_newer_seed_sample(core):
    app, _, _ = core
    sample(app)
    live = app.service.on_unknown(at(8, 55), None, None, None, None)
    c = client(app)
    page = c.get('/unknowns?review=first').get_data(as_text=True)
    assert f'document.querySelector(\'.face-card[data-id="{live}"]\')' in page
    app.store.resolve_unknown(live, 'assigned', 'A')
    assert 'openReview(first)' not in c.get('/unknowns?review=first').get_data(as_text=True)


def test_review_seed_samples_survive_snapshot_retention_and_do_not_enter_camera_evaluation(core):
    app, _, _ = core
    uid = sample(app)
    app.store._exec('UPDATE unknowns SET image=? WHERE id=?', ('review-seed/sample.jpg', uid))
    app.service.ignore_unknown(uid)
    assert app.store.labelled_faces(DAY, DAY) == []
    assert app.store.expire_snapshots(DAY.replace(day=DAY.day + 1)) == []
    assert app.store.unknown(uid) is not None


def test_old_unknown_records_default_to_camera(tmp_path):
    import sqlite3
    path = tmp_path/'old.db'
    store = Store(path)
    store.add_unknown(DAY, at(9, 0), None, None, None, None, None)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE unknowns DROP COLUMN source')
    reopened = Store(path)
    assert reopened.unknowns()[0].source == 'camera'


def make_seed(tmp_path, image_bytes):
    seed = tmp_path/'seed';(seed/'review').mkdir(parents=True)
    (seed/'review/sample.jpg').write_bytes(image_bytes)
    row = {'file':'review/sample.jpg','captured_at':at(9, 0).isoformat(),
           'sha256':hashlib.sha256(image_bytes).hexdigest()}
    with (seed/'review.csv').open('w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    manifest={name:hashlib.sha256((seed/name).read_bytes()).hexdigest()
              for name in ('review.csv','review/sample.jpg')}
    (seed/'manifest.json').write_text(json.dumps(manifest))
    return seed


def seed_metadata(seed, **metadata):
    path = seed/'review.csv'
    with path.open(newline='') as file:
        rows = list(csv.DictReader(file))
    rows[0].update(metadata)
    with path.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest = json.loads((seed/'manifest.json').read_text())
    manifest['review.csv'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (seed/'manifest.json').write_text(json.dumps(manifest))


def test_seed_auto_resolution_uses_local_email_id_without_learning_or_attendance(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, sent = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model=app.service.model_id)
    before = len(app.store.references(app.service.model_id))
    report = import_review_samples(app, seed)
    u = app.store.unknowns(status=None)[0]
    assert report['resolved'] == 1 and u.status == 'assigned' and u.employee_id == 'A'
    assert len(app.store.references(app.service.model_id)) == before
    assert not app.store.checkins(DAY) and not app.store.sightings(DAY) and not sent
    assert import_review_samples(app, seed)['resolved'] == 0
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'Auto-labelled seed samples (1)' in page and 'alice@example.com' in page


@pytest.mark.parametrize('score,runner', [(.59, .0), (.70, .59)])
def test_seed_auto_resolution_rejects_weak_scores_and_small_margins(core, tmp_path, score, runner):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model=app.service.model_id)
    q = person_vector(1)
    other = person_vector(50)
    other -= np.dot(q, other)*q
    other /= np.linalg.norm(other)
    app.gallery.replace([('A', score*q + np.sqrt(1-score**2)*other),
                         ('B', runner*q - np.sqrt(1-runner**2)*other)])
    report = import_review_samples(app, seed)
    assert report['resolved'] == 0 and report['auto_pending'] == 1
    assert app.store.unknowns()[0].employee_id is None


@pytest.mark.parametrize('case', ['model', 'inactive', 'stricter_score', 'different_person', 'no_face'])
def test_seed_auto_resolution_rechecks_model_employee_and_actual_face(core, tmp_path, case):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(0 if case == 'no_face' else 1)))
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model='another-model' if case == 'model' else app.service.model_id)
    if case == 'inactive':
        app.store.update_employee('A', 'Alice', False)
    if case == 'stricter_score':
        app.cfg.recognition.accept_score = 1.01
    if case == 'different_person':
        app.gallery.replace([('B', person_vector(1)), ('A', person_vector(2))])
    report = import_review_samples(app, seed)
    assert report['resolved'] == 0 and report['auto_pending'] == 1
    assert app.store.unknowns()[0].employee_id is None


def test_seed_rejects_conflicting_auto_email_before_writing_anything(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='bob@example.com',
                  auto_resolve_model=app.service.model_id)
    with pytest.raises(ValueError, match='auto.*email'):
        import_review_samples(app, seed)
    assert app.store.unknowns(status=None) == []


@pytest.mark.parametrize('handled', ['assigned', 'ignored', 'camera'])
def test_seed_auto_resolution_preserves_handled_labels_and_camera(core, tmp_path, handled):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    import_review_samples(app, seed)
    uid = app.store.unknowns()[0].id
    if handled == 'camera':
        app.store._exec("UPDATE unknowns SET source='camera' WHERE id=?", (uid,))
    else:
        app.store.resolve_unknown(uid, handled, 'B' if handled == 'assigned' else None)
    before = app.store.unknown(uid)
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model=app.service.model_id)
    assert import_review_samples(app, seed)['resolved'] == 0
    after = app.store.unknown(uid)
    assert after.status == before.status and after.employee_id == before.employee_id and after.source == before.source


def test_seed_source_hint_is_manual_and_independent_of_the_model_guess(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, sent = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    seed_metadata(seed, expected_email='bob@example.com')
    assert import_review_samples(app, seed)['resolved'] == 0
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'Source label:' in page and 'Bob (bob@example.com)' in page
    assert 'Model and source disagree' in page and 'data-guess="A"' in page
    assert app.store.unknowns()[0].employee_id is None and not sent


def test_seed_auto_label_can_be_reopened_without_reimport_overriding_the_user(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, sent = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model=app.service.model_id)
    import_review_samples(app, seed)
    uid = app.store.unknowns(status=None)[0].id
    before = len(app.store.references(app.service.model_id))
    response = client(app).post(f'/unknowns/{uid}/reopen-seed', follow_redirects=True)
    assert response.status_code == 200
    assert app.store.unknown(uid).status == 'pending' and app.store.unknown(uid).employee_id is None
    assert import_review_samples(app, seed)['resolved'] == 0
    assert app.store.unknown(uid).status == 'pending'
    assert len(app.store.references(app.service.model_id)) == before and not sent


def test_seed_auto_resolution_keeps_a_label_changed_while_the_model_runs(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    import_review_samples(app, seed)
    uid = app.store.unknowns()[0].id
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model=app.service.model_id)
    analyze = app._recognizer.analyze_photo
    def changed(image):
        app.store.resolve_unknown(uid, 'assigned', 'B')
        return analyze(image)
    app._recognizer.analyze_photo = changed
    assert import_review_samples(app, seed)['resolved'] == 0
    assert app.store.unknown(uid).employee_id == 'B'
    assert app.store.get(f'seed_review_auto:{uid}') is None


@pytest.mark.parametrize('operation', ['assign', 'reopen'])
def test_seed_auto_label_and_reopen_roll_back_with_their_audit(core, tmp_path, operation):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    import_review_samples(app, seed)
    uid = app.store.unknowns()[0].id
    seed_metadata(seed, expected_email='alice@example.com', auto_resolve_email='alice@example.com',
                  auto_resolve_model=app.service.model_id)
    if operation == 'reopen':
        import_review_samples(app, seed)
    app.store._exec("CREATE TEMP TRIGGER fail_seed_audit BEFORE INSERT ON audit "
                    "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
        if operation == 'assign':
            import_review_samples(app, seed)
        else:
            client(app).post(f'/unknowns/{uid}/reopen-seed')
    assert app.store.unknown(uid).status == ('pending' if operation == 'assign' else 'assigned')
    assert bool(app.store.get(f'seed_review_auto:{uid}')) == (operation == 'reopen')
    assert app.store.get(f'seed_review_manual:{uid}') is None


@pytest.mark.parametrize('source', ['camera', 'seed'])
def test_reopen_seed_rejects_samples_that_were_not_auto_labelled(core, source):
    app, _, _ = core
    uid = app.store.add_unknown(DAY, at(9, 0), None, app.service.model_id,
                                person_vector(1), None, None, source=source)
    app.store.resolve_unknown(uid, 'assigned', 'A')
    assert client(app).post(f'/unknowns/{uid}/reopen-seed').status_code == 404
    assert app.store.unknown(uid).status == 'assigned'


def test_review_seed_import_is_repeatable_without_learning_or_recording_arrivals(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, sent = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    before = len(app.store.references(app.library.model_id))
    first = import_review_samples(app, seed)
    assert first['imported'] == 1
    uid = app.store.unknowns()[0].id
    assert app.store.unknown(uid).source == 'seed'
    assert app.store.unknown(uid).vector is not None
    assert (app.data_dir/app.store.unknown(uid).image).is_file()
    assert client(app).get('/media/'+app.store.unknown(uid).image).status_code == 200
    assert import_review_samples(app, seed)['skipped'] == 1
    assert len(app.store.unknowns()) == 1
    app.service.resolve_unknown(uid, 'A')
    assert import_review_samples(app, seed)['skipped'] == 1
    assert app.store.unknowns() == []
    assert len(app.store.references(app.library.model_id)) == before
    assert not app.store.checkins(DAY) and not sent


def test_undetectable_review_sample_can_still_be_labelled(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed=make_seed(tmp_path,encode_jpeg(face_image(0)))
    assert import_review_samples(app,seed)['imported']==1
    uid=app.store.unknowns()[0].id
    assert app.store.unknown(uid).vector is None
    app.service.resolve_unknown(uid,'B')
    assert app.store.unknown(uid).employee_id=='B'
    assert not app.store.checkins(DAY)


def test_reimport_refreshes_missing_seed_vector_without_resetting_labels_or_camera(core, tmp_path):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, sent = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    import_review_samples(app, seed)
    uid = app.store.unknowns()[0].id
    app.store._exec('UPDATE unknowns SET vector=NULL WHERE id=?', (uid,))
    before = len(app.store.references(app.library.model_id))
    report = import_review_samples(app, seed)
    assert app.store.unknown(uid).vector is not None
    assert report['refreshed'] == 1 and report['skipped'] == 1 and report['imported'] == 0
    assert app.store.unknown(uid).status == 'pending'
    assert len(app.store.unknowns(status=None)) == 1
    assert len(app.store.references(app.library.model_id)) == before
    assert not app.store.checkins(DAY) and not sent

    app.store.resolve_unknown(uid, 'assigned', 'A')
    app.store._exec('UPDATE unknowns SET vector=NULL WHERE id=?', (uid,))
    assert import_review_samples(app, seed)['refreshed'] == 0
    assert app.store.unknown(uid).employee_id == 'A' and app.store.unknown(uid).vector is None
    app.store.resolve_unknown(uid, 'ignored', None)
    assert import_review_samples(app, seed)['refreshed'] == 0
    assert app.store.unknown(uid).status == 'ignored' and app.store.unknown(uid).vector is None
    app.store._exec("UPDATE unknowns SET status='pending', employee_id=NULL, source='camera' WHERE id=?", (uid,))
    assert import_review_samples(app, seed)['refreshed'] == 0
    assert app.store.unknown(uid).source == 'camera' and app.store.unknown(uid).vector is None


def test_seed_vector_refresh_checks_current_state_and_never_overwrites_a_vector(core):
    app, _, _ = core
    uid = sample(app)
    app.store.resolve_unknown(uid, 'assigned', 'A')
    assert not app.store.refresh_seed_vector(uid, app.service.model_id, person_vector(1))
    assert app.store.unknown(uid).vector is None
    app.store.resolve_unknown(uid, 'pending', None)
    assert app.store.refresh_seed_vector(uid, app.service.model_id, person_vector(1))
    assert not app.store.refresh_seed_vector(uid, 'another-model', person_vector(2))
    import numpy as np
    np.testing.assert_array_equal(app.store.unknown(uid).vector, person_vector(1))
    assert app.store.unknown(uid).model == app.service.model_id


@pytest.mark.parametrize('second_size', [100, 30])
def test_review_sample_with_two_faces_does_not_learn_an_arbitrary_person(core, tmp_path, second_size):
    from attendance.cli.seed import import_review_samples
    from attendance.vision.faces import encode_jpeg
    app, _, _ = core
    seed = make_seed(tmp_path, encode_jpeg(face_image(1)))
    first = app._recognizer.largest_face(face_image(1))
    second = app._recognizer.largest_face(face_image(2))
    second.size = second_size
    app._recognizer.analyze = lambda image: [first, second]
    assert import_review_samples(app, seed)['imported'] == 1
    uid = app.store.unknowns()[0].id
    assert app.store.unknown(uid).vector is None
    before = len(app.store.references(app.library.model_id))
    app.service.resolve_unknown(uid, 'A')
    assert len(app.store.references(app.library.model_id)) == before


def test_review_seed_photos_are_backed_up_and_restored_without_camera_snapshots(core, tmp_path):
    from attendance.backup import restore
    from attendance.config import Config
    from test_backup import prepare
    app, _, _ = core
    destination = prepare(app, tmp_path)
    app.cfg.backup.include_photos = False
    folder = app.data_dir/'review-seed';folder.mkdir()
    (folder/'sample.jpg').write_bytes(b'sample')
    assert app.backup.run_now().ok
    assert (destination/'data/review-seed/sample.jpg').read_bytes() == b'sample'
    restored = tmp_path/'restored';restored.mkdir()
    cfg = Config(base_dir=restored)
    restore(cfg, destination)
    assert (restored/'data/review-seed/sample.jpg').read_bytes() == b'sample'
