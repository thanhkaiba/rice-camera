import numpy as np
import pytest

from conftest import DAY, at, person_vector
from test_web import client


def add_face(app, vector, *, group=None, model=None, minute=10):
    return app.store.add_unknown(DAY, at(9, minute), None, model or app.service.model_id,
                                 vector, "B", .99, group_id=group)


def test_review_updates_suggestions_after_reference_photos_change(core):
    app, _, _ = core
    add_face(app, person_vector(20))
    c = client(app)
    assert 'data-guess=""' in c.get('/unknowns').get_data(as_text=True)
    app.store.add_embedding(app.service.model_id, 'A', person_vector(20), 'upload', at(8, 0))
    # Two references ensure the new appearance is represented by the gallery's top-two score.
    app.store.add_embedding(app.service.model_id, 'A', person_vector(20), 'upload', at(8, 1))
    app.library.reload()
    page = c.get('/unknowns').get_data(as_text=True)
    assert 'data-guess="A"' in page
    assert 'data-guess-name="Alice (alice@example.com)"' in page


def test_review_uses_group_agreement_instead_of_the_highest_cached_guess(core):
    app, _, _ = core
    first = add_face(app, person_vector(1))
    group = app.store.unknown(first).group_id
    add_face(app, person_vector(1), group=group, minute=11)
    add_face(app, person_vector(2), group=group, minute=12)
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'data-guess="A"' in page
    assert 'data-guess="B"' not in page


def test_review_does_not_suggest_between_two_nearly_equal_employees(core):
    app, _, _ = core
    a = np.zeros(512, dtype=np.float32)
    a[0] = 1
    b = a.copy()
    b[1] = .1
    app.gallery.replace([('A', a), ('B', b)])
    add_face(app, a)
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'data-guess=""' in page
    assert 'No clear suggestion' in page


def test_review_does_not_suggest_for_a_split_group(core):
    app, _, _ = core
    first = add_face(app, person_vector(1))
    add_face(app, person_vector(2), group=app.store.unknown(first).group_id, minute=11)
    assert 'data-guess=""' in client(app).get('/unknowns').get_data(as_text=True)


@pytest.mark.parametrize('vector,model', [
    (None, None), (person_vector(1), 'other-model'),
    (np.zeros(512, dtype=np.float32), None), (np.full(512, np.nan, dtype=np.float32), None),
])
def test_review_does_not_reuse_a_cached_name_without_a_usable_embedding(core, vector, model):
    app, _, _ = core
    add_face(app, vector, model=model)
    assert 'data-guess=""' in client(app).get('/unknowns').get_data(as_text=True)


def test_review_get_does_not_assign_learn_or_change_cached_camera_evidence(core):
    app, _, sent = core
    uid = add_face(app, person_vector(1))
    before = app.store.unknown(uid)
    refs = len(app.store.references(app.library.model_id))
    assert 'data-guess="A"' in client(app).get('/unknowns').get_data(as_text=True)
    after = app.store.unknown(uid)
    assert after.status == before.status == 'pending'
    assert after.guess_id == before.guess_id == 'B'
    assert after.guess_score == before.guess_score == .99
    assert len(app.store.references(app.library.model_id)) == refs
    assert not app.store.checkins(DAY) and not sent
