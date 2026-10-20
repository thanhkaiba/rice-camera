"""Assess reference usefulness using recognition, without self-matches or duplicate evidence."""
import numpy as np
import pytest

from attendance.vision.gallery import Gallery
from conftest import at
from test_web import client


@pytest.fixture
def library(core):
    app, _, _ = core
    app.store._exec("DELETE FROM embeddings")
    return app.library


def add(lib, employee, vector, clarity=0.8):
    lib.store.add_embedding(lib.model_id, employee, vector, "upload", at(8, 0), quality_score=clarity)
    return lib.store.references(lib.model_id, employee)[-1].id


def test_prepared_gallery_preserves_two_reference_matching_without_mutating_inputs():
    grouped = {"A": np.array([[1, 0, 0], [0.8, 0.6, 0]], dtype=np.float32),
               "B": np.array([[0, 0, 1]], dtype=np.float32)}
    gallery = Gallery.from_normalized(grouped)
    matches = gallery.match(np.array([1, 0, 0], dtype=np.float32))
    assert [person for person, _ in matches] == ["A", "B"]
    assert [score for _, score in matches] == pytest.approx([0.9, 0])
    gallery.add("A", np.array([0, 1, 0], dtype=np.float32))
    assert grouped["A"].shape == (2, 3)
    assert gallery.counts() == {"A": 3, "B": 1}


def test_clear_identity_match_is_useful_even_when_image_clarity_is_low(library):
    first = add(library, "A", [1, 0, 0], clarity=0.1)
    add(library, "A", [0.8, 0.6, 0])
    add(library, "B", [0, 0, 1])
    check = library.recognition_quality("A")[first]
    assert check.status == "useful"
    assert check.own == pytest.approx(0.8)
    assert check.other == pytest.approx(0)
    assert check.margin == pytest.approx(0.8)
    assert check.duplicate_of is None


def test_misplaced_reference_is_flagged_with_the_confusable_employee(library):
    add(library, "A", [1, 0, 0])
    misplaced = add(library, "A", [0, 0, 1])
    add(library, "B", [0, 0.1, 1])
    add(library, "B", [0.1, 0, 1])
    check = library.recognition_quality("A")[misplaced]
    assert check.status == "review"
    assert check.other_id == "B"
    assert check.own == pytest.approx(0)
    assert check.margin < 0
    assert "another employee" in check.reason


def test_ambiguous_and_weak_references_have_distinct_reasons(library):
    first = add(library, "A", [1, 0, 0])
    add(library, "A", [0.8, 0.6, 0])
    add(library, "B", [0.8, 0, 0.6])
    add(library, "B", [0.8, 0, -0.6])
    check = library.recognition_quality("A")[first]
    assert check.status == "review" and check.margin == pytest.approx(0)
    assert "separation" in check.reason

    library.store._exec("DELETE FROM embeddings WHERE employee_id = 'B'")
    library.cfg.recognition.accept_score = 0.9
    check = library.recognition_quality("A")[first]
    assert check.status == "review" and "weak" in check.reason.lower()


def test_single_reference_is_unverified_instead_of_matching_itself(library):
    first = add(library, "A", [1, 0, 0])
    check = library.recognition_quality("A")[first]
    assert check.status == "unverified"
    assert check.own is None and check.margin is None


@pytest.mark.parametrize("copies", [1, 2])
def test_strong_other_identity_needs_review_even_without_independent_own_evidence(library, copies):
    first = add(library, "A", [1, 0, 0])
    if copies == 2:
        add(library, "A", [1, 0.01, 0])
    add(library, "B", [1, 0.1, 0])
    add(library, "B", [1, 0, 0.1])
    check = library.recognition_quality("A")[first]
    assert check.status == "review" and "another employee" in check.reason
    assert check.own is None and check.other_id == "B"
    assert check.other == pytest.approx(0.995, abs=0.001)


def test_other_employee_score_uses_two_best_references_like_live_recognition(library):
    first = add(library, "A", [1, 0, 0])
    add(library, "A", [0.8, 0.6, 0])
    add(library, "B", [1, 0, 0])
    add(library, "B", [0, 0, 1])
    check = library.recognition_quality("A")[first]
    assert check.status == "useful"
    assert check.other == pytest.approx(0.5)
    assert check.margin == pytest.approx(0.3)


def test_centering_is_recomputed_without_the_held_out_photo(library):
    first = add(library, "A", [1, 0, 0, 0, 0, 0, 0, 0, 0, 1])
    add(library, "A", [0.8, 0.6, 0, 0, 0, 0, 0, 0, 0, 1])
    others = ["B", "C"] + [library.store.add_employee(f"{name}@example.com", name) for name in "DEFGH"]
    for index, person in enumerate(others, 2):
        vector = [0] * 10
        vector[index] = vector[-1] = 1
        add(library, person, vector)
    library.cfg.recognition.center_embeddings = True
    check = library.recognition_quality("A")[first]
    # Analytic cosine after projecting off the mean of the eight held-in identities.
    assert check.own == pytest.approx(0.7783, abs=0.001)
    assert check.other == pytest.approx(-0.1112, abs=0.001)
    assert check.status == "useful"


def test_near_duplicates_are_not_independent_evidence_and_prefer_clearer_copy(library):
    first = add(library, "A", [1, 0, 0], clarity=0.2)
    clearer = add(library, "A", [1, 0.01, 0], clarity=0.9)
    checks = library.recognition_quality("A")
    assert all(check.status == "unverified" and check.own is None for check in checks.values())
    assert checks[first].duplicate_of == clearer
    assert checks[clearer].duplicate_of is None


def test_inactive_employees_do_not_create_conflicts(library):
    first = add(library, "A", [1, 0, 0])
    add(library, "A", [0.8, 0.6, 0])
    add(library, "B", [1, 0, 0])
    library.store.update_employee("B", "Bob", False)
    assert library.recognition_quality("A")[first].status == "useful"
    # Inactive people's own profiles can still be assessed against active competitors.
    assert library.recognition_quality("B")


def test_people_and_profile_show_recognition_findings_without_loading_models(core, library):
    app, _, _ = core
    add(library, "A", [1, 0, 0])
    add(library, "A", [0, 0, 1])
    add(library, "B", [0, 0.1, 1])
    add(library, "B", [0.1, 0, 1])
    app._recognizer = None
    library.reload()
    c = client(app)
    assert c.post("/employees/quality").status_code == 302
    page = c.get("/employees/A").get_data(as_text=True)
    assert "Recognition check" in page and "another employee" in page
    assert "Bob" in page and "Same person" in page and "Gap" in page
    assert "need review" in c.get("/employees").get_data(as_text=True)
    assert app._recognizer is None


def test_opening_people_or_profile_never_runs_quality_checks(core, monkeypatch):
    app, _, _ = core

    def forbidden(*args, **kwargs):
        raise AssertionError("Opening a page must only read saved quality results")

    monkeypatch.setattr(app.library, "recognition_quality", forbidden)
    monkeypatch.setattr(app.library, "look_alikes", forbidden)
    c = client(app)
    for path in ("/employees", "/employees/A"):
        page = c.get(path)
        assert page.status_code == 200
        assert "Re-evaluate quality" in page.get_data(as_text=True)
        assert "Not evaluated" in page.get_data(as_text=True)


def test_manual_quality_results_survive_restart_and_gets_only_read_them(core, library, monkeypatch):
    from attendance.core.pipeline import App
    from attendance.vision.faces import FaceLibrary
    from test_review_controls import ReviewControls

    app, clock, _ = core
    first = add(library, "A", [1, 0, 0])
    add(library, "A", [0.8, 0.6, 0])
    c = client(app)
    controls = ReviewControls(c.get('/employees').get_data(as_text=True))
    form = next(f for f in controls.forms if f.get('action') == '/employees/quality')
    response = c.post(form['action'], data=form['fields'], follow_redirects=True)
    assert response.status_code == 200 and 'Last evaluated' in response.get_data(as_text=True)
    assert library.saved_quality()['checks'][first].own == pytest.approx(0.8)
    assert any(action == 'evaluate photo quality' for _, _, action, _ in app.store.audit())

    def forbidden(*args, **kwargs):
        raise AssertionError('Saved results must survive restart without recomputing')

    monkeypatch.setattr(FaceLibrary, 'recognition_quality', forbidden)
    monkeypatch.setattr(FaceLibrary, 'look_alikes', forbidden)
    restarted = App(app.cfg, now=clock)
    try:
        report = restarted.library.saved_quality()
        assert not report['stale'] and report['checked_at']
        assert report['checks'][first].own == pytest.approx(0.8)
        assert 'consistent' in client(restarted).get('/employees').get_data(as_text=True)
    finally:
        restarted.store.close()


def test_profile_quality_button_returns_to_the_profile_and_rejects_unknown_people(core, monkeypatch):
    from test_review_controls import ReviewControls

    app, _, _ = core
    c = client(app)
    controls = ReviewControls(c.get('/employees/A').get_data(as_text=True))
    form = next(f for f in controls.forms if f.get('action') == '/employees/quality')
    assert c.post(form['action'], data=form['fields']).location == '/employees/A'

    def forbidden():
        raise AssertionError('Unknown employee must be rejected before evaluating')

    monkeypatch.setattr(app.library, 'reevaluate_quality', forbidden)
    assert c.post('/employees/quality', data={'employee_id': 'missing'}).status_code == 404


@pytest.mark.parametrize('change', ['add', 'delete', 'score', 'active', 'threshold', 'center', 'model'])
def test_changed_photos_or_recognition_settings_mark_saved_results_stale(library, change):
    first = add(library, 'A', [1, 0, 0])
    add(library, 'A', [0.8, 0.6, 0])
    library.reload()
    library.reevaluate_quality()
    assert library.saved_quality()['checks']
    if change == 'add':
        add(library, 'A', [0, 0, 1])
    elif change == 'delete':
        library.store.delete_reference(first)
    elif change == 'score':
        library.store._exec('UPDATE embeddings SET quality_score = 0.3 WHERE id = ?', (first,))
    elif change == 'active':
        library.store.update_employee('B', 'Bob', False)
    elif change == 'threshold':
        library.cfg.recognition.min_margin = 0.2
    elif change == 'center':
        library.cfg.recognition.center_embeddings = True
    else:
        library.cfg.recognition.recognizer_model = 'models/other.onnx'
    report = library.saved_quality()
    assert report['stale'] and report['checked_at']
    assert not report['checks'] and not report['look_alikes']


def test_failed_manual_evaluation_preserves_the_previous_report(core, library, monkeypatch):
    app, _, _ = core
    add(library, 'A', [1, 0, 0])
    library.reload()
    library.reevaluate_quality()
    previous = library.saved_quality()

    def fail():
        raise ValueError('Could not assess these photos')

    monkeypatch.setattr(library, 'recognition_quality', fail)
    response = client(app).post('/employees/quality', follow_redirects=True)
    assert response.status_code == 200
    assert 'Quality evaluation failed' in response.get_data(as_text=True)
    assert library.saved_quality() == previous


def test_photos_changed_during_evaluation_do_not_publish_a_mixed_report(library, monkeypatch):
    add(library, 'A', [1, 0, 0])
    library.reload()
    library.reevaluate_quality()
    evaluate = library.recognition_quality

    def concurrent_upload():
        checks = evaluate()
        add(library, 'A', [0, 0, 1])
        return checks

    monkeypatch.setattr(library, 'recognition_quality', concurrent_upload)
    with pytest.raises(ValueError, match='changed'):
        library.reevaluate_quality()
    assert library.saved_quality()['stale']
