"""The 'Photos to review' page: every flagged reference photo in one place, to delete or keep."""
from conftest import at
from test_web import client


def setup(app):
    """A has two consistent photos and one that is really B's; B has two photos."""
    store, model = app.store, app.library.model_id
    store._exec("DELETE FROM embeddings")
    for emp, vector in (("A", [1, 0, 0]), ("A", [0.95, 0.31, 0]), ("A", [0, 0.05, 1]),
                        ("B", [0, 0.1, 1]), ("B", [0.1, 0, 1])):
        store.add_embedding(model, emp, vector, "upload", at(8, 0), quality_score=0.8)
    app.library.reload()
    app.library.reevaluate_quality()
    misplaced = store.references(model, "A")[-1]
    assert app.library.saved_quality()["checks"][misplaced.id].status == "review"
    return misplaced


def ids(app, emp):
    return [r.id for r in app.store.references(app.library.model_id, emp)]


def test_flagged_photo_is_listed_with_the_look_alike(core):
    app, _, _ = core
    misplaced = setup(app)
    page = client(app).get("/photos/review").get_data(as_text=True)
    assert f'value="{misplaced.id}"' in page
    assert "Looks like" in page and "Bob" in page
    assert page.count('name="ref_id"') == len([c for c in app.library.saved_quality()["checks"].values()
                                                if c.status == "review"])


def test_people_page_links_to_the_review(core):
    app, _, _ = core
    assert "/photos/review" in client(app).get("/employees").get_data(as_text=True)


def test_delete_selected_removes_only_those(core):
    app, _, _ = core
    misplaced = setup(app)
    before_b = ids(app, "B")
    response = client(app).post("/photos/review/delete", data={"ref_id": [str(misplaced.id)]}, follow_redirects=True)
    assert response.status_code == 200
    assert misplaced.id not in ids(app, "A") and len(ids(app, "A")) == 2
    assert ids(app, "B") == before_b


def test_after_a_delete_the_remaining_flags_still_show_until_reevaluated(core):
    app, _, _ = core
    misplaced = setup(app)
    flagged = [i for i, c in app.library.saved_quality()["checks"].items() if c.status == "review" and i != misplaced.id]
    client(app).post("/photos/review/delete", data={"ref_id": [str(misplaced.id)]})
    page = client(app).get("/photos/review").get_data(as_text=True)
    assert f'value="{misplaced.id}"' not in page
    for i in flagged:
        assert f'value="{i}"' in page
    if flagged:
        assert "re-evaluate quality to refresh" in page


def test_keep_hides_the_photo_for_good(core):
    app, _, _ = core
    misplaced = setup(app)
    client(app).post("/photos/review/keep", data={"ref_id": [str(misplaced.id)]})
    assert misplaced.id in ids(app, "A")
    assert f'value="{misplaced.id}"' not in client(app).get("/photos/review").get_data(as_text=True)
    app.library.reevaluate_quality()  # flagged again by a new check: still kept
    assert f'value="{misplaced.id}"' not in client(app).get("/photos/review").get_data(as_text=True)


def test_bad_selection_is_refused_without_deleting(core):
    app, _, _ = core
    setup(app)
    before = ids(app, "A") + ids(app, "B")
    for data in ({}, {"ref_id": ["x"]}, {"ref_id": ["999999"]}):
        assert client(app).post("/photos/review/delete", data=data).status_code == 400
        assert client(app).post("/photos/review/keep", data=data).status_code == 400
    assert ids(app, "A") + ids(app, "B") == before


def test_person_who_would_keep_too_few_photos_is_not_in_select_all_safe(core):
    app, _, _ = core
    setup(app)  # A has 3 photos, 1 flagged: 2 would be left, under the minimum of 3
    page = client(app).get("/photos/review").get_data(as_text=True)
    assert 'data-safe="0"' in page and "would keep only" in page


def test_not_evaluated_yet_says_so(core):
    app, _, _ = core
    app.store.put("reference_quality", "")
    page = client(app).get("/photos/review").get_data(as_text=True)
    assert "Not evaluated yet" in page
