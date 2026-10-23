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
    assert 'data-guess-name="Alice (alice)"' in page


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
    assert 'data-guess=""' in page and 'Confirm:' not in page
    # Neither is offered as the answer, but both as choices to pick from.
    assert "Maybe <strong>Alice (alice)</strong>" in page and "It's Alice (alice)" in page and "It's Bob (bob)" in page
    app.store._db.execute("UPDATE employees SET name = 'Lương Thế Hùng', email = 'enzo@example.com' WHERE id = 'B'")
    app.store._db.commit()
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert "It's Lương Thế Hùng (enzo)" in page and "It's Enzo" not in page


@pytest.mark.parametrize("chat_names", ["email", "name"])
def test_review_uses_employee_name_and_email_prefix_everywhere(core, chat_names):
    app, _, _ = core
    app.cfg.google_chat.names = chat_names
    app.store.update_employee("A", "Lương Thế Hùng", True, email="enzo.nguyen@example.com")
    add_face(app, person_vector(1))
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'data-guess-name="Lương Thế Hùng (enzo.nguyen)"' in page
    assert 'Looks like <strong>Lương Thế Hùng (enzo.nguyen)</strong>' in page
    assert 'Confirm: Lương Thế Hùng (enzo.nguyen)' in page
    assert '<option value="Lương Thế Hùng (enzo.nguyen)" data-id="A">' in page
    assert 'enzo.nguyen@example.com' not in page


def test_review_shows_legacy_employee_without_empty_email_parentheses(core):
    app, _, _ = core
    app.store._exec("UPDATE employees SET email = NULL WHERE id = 'A'")
    add_face(app, person_vector(1))
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'data-guess-name="Alice"' in page
    assert 'Confirm: Alice' in page and 'Alice ()' not in page


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


def test_review_shows_maybe_choices_from_the_suggest_score_and_sorts_ready_cards_first(core):
    app, _, _ = core
    a, other = person_vector(1), person_vector(30)
    r = app.cfg.recognition
    for w in np.arange(0.5, 4, 0.05):  # mix in a stranger until the face is only somewhat like Alice
        weak = (a + w * other) / np.linalg.norm(a + w * other)
        score = app.gallery.match(weak)[0][1]
        if r.suggest_score + 0.01 <= score < r.accept_score - 0.02:
            break
    unlike = add_face(app, person_vector(31), minute=12)  # like nobody: last
    maybe = add_face(app, weak, minute=11)
    ready = add_face(app, a, minute=10)  # clearly Alice, though seen earliest: first
    score = app.service.review_candidates([app.store.unknown(maybe)])[0][1]
    assert r.suggest_score <= score < r.accept_score
    page = client(app).get('/unknowns').get_data(as_text=True)
    order = [page.index(f'data-id="{i}"') for i in (ready, maybe, unlike)]
    assert order == sorted(order)
    assert "1 ready to confirm" in page and "Confirm: Alice" in page and "It's Alice" in page
    app.cfg.recognition.suggest_score = round(score + 0.01, 2)  # stricter: no longer offered
    assert app.service.review_candidates([app.store.unknown(maybe)]) == []


def test_review_picker_labels_stay_unique_when_email_names_repeat():
    from types import SimpleNamespace as P

    from attendance.web.attendance import _review_labels

    labels = _review_labels([P(id="1", name="An Nguyen", email="an@company.com"),
                             P(id="2", name="An Nguyen", email="an@partner.com"),
                             P(id="3", name="Ruby Doan", email="ruby@company.com")])
    assert labels == {"1": "An Nguyen (an@company.com)", "2": "An Nguyen (an@partner.com)", "3": "Ruby Doan (ruby)"}


def test_a_clear_suggestion_also_offers_the_next_most_similar_people(core):
    app, _, _ = core
    a, b = person_vector(1), person_vector(2)
    face = (a + 0.75 * b) / np.linalg.norm(a + 0.75 * b)  # Alice 0.8, Bob 0.6
    add_face(app, face)
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert 'data-guess="A"' in page and "Confirm: Alice (alice)" in page
    assert "It's Bob (bob)" in page and "It's Carol" not in page  # Carol is like nobody here


def test_suggestions_say_who_is_already_in_today(core):
    app, clock, _ = core
    app.service.on_identified("A", at(8, 52), 0.8, None)
    clock.t = at(9, 30)
    add_face(app, person_vector(1), minute=20)
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert "Looks like <strong>Alice (alice)</strong>" in page and ">in 08:52</span>" in page


def test_blurry_camera_faces_are_listed_last_apart_and_never_dropped(core):
    app, _, _ = core
    blurry = app.service.on_unknown(at(9, 5), None, person_vector(1), "A", 0.5, quality=0.04)
    clear = app.service.on_unknown(at(9, 6), None, person_vector(31), None, None, quality=0.4)
    old = app.service.on_unknown(at(9, 7), None, person_vector(32), None, None)  # stored before the score was kept
    assert app.store.unknown(blurry).quality == 0.04 and app.store.unknown(old).quality is None
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert "Blurry or too small (1)" in page and 'id="select-blurry"' in page
    # The clear ones first, even the one with no suggestion, then the section, then the blurry one.
    order = [page.index(f'data-id="{clear}"'), page.index(f'data-id="{old}"'), page.index('id="blurry"'),
             page.index(f'data-id="{blurry}"')]
    assert sorted(order[:2]) + order[2:] == sorted(order)
    assert 'class="face-card blurry"' in page and "Confirm: Alice" in page  # still one click to confirm
    app.cfg.recognition.blurry_score = 0
    page = client(app).get('/unknowns').get_data(as_text=True)
    assert "Blurry or too small" not in page and "face-card blurry" not in page


def test_the_tracker_keeps_the_quality_of_the_face_it_reports():
    import datetime as dt

    from attendance.config import RecognitionConfig
    from attendance.tracker import FaceTracker, Observation

    tracker = FaceTracker(RecognitionConfig(unknown_min_frames=2))
    t0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.timezone.utc)
    face = person_vector(40)
    for i, q in enumerate((0.03, 0.07, 0.05)):
        tracker.update([Observation((100, 100, 160, 160), face, [("A", 0.2)], False, q)], t0 + dt.timedelta(seconds=i / 5))
    out = tracker.update([], t0 + dt.timedelta(seconds=30))
    assert [(d.kind, d.quality) for d in out] == [("unknown", 0.07)]
