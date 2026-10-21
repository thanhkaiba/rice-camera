import datetime as dt
import json
from html.parser import HTMLParser

import pytest

from conftest import DAY, at, person_vector
from test_web import client


class ReviewControls(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.in_dialog = False
        self.forms = []
        self.inputs = []
        self.cards = []
        self.form = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "face-card" in attrs.get("class", "").split() and "data-id" in attrs:
            self.cards.append(attrs)
        if tag == "input":
            self.inputs.append(attrs)
        if tag == "dialog":
            self.in_dialog = True
        if tag == "form":
            self.form = {**attrs, "in_dialog": self.in_dialog, "fields": {}}
            self.forms.append(self.form)
        if tag == "input" and self.form and "name" in attrs:
            self.form["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "dialog":
            self.in_dialog = False
        if tag == "form":
            self.form = None


@pytest.mark.parametrize("source", ["camera", "seed"])
def test_card_can_confirm_current_suggestion_without_opening_dialog(core, source):
    app, _, _ = core
    uid = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id,
                                person_vector(1), "B", .99, source=source)
    c = client(app)
    controls = ReviewControls(c.get("/unknowns").get_data(as_text=True))
    quick = [f for f in controls.forms if not f["in_dialog"] and f.get("action") == f"/unknowns/{uid}/assign"]
    assert len(quick) == 1
    assert quick[0]["fields"] == {"employee_id": "A"}
    assert quick[0]["method"] == "post"
    assert app.store.unknown(uid).status == "pending"

    response = c.post(quick[0]["action"], data=quick[0]["fields"])
    assert response.status_code == 302
    assert app.store.unknown(uid).employee_id == "A"
    assert ("A" in app.store.checkins(DAY)) == (source == "camera")


def test_card_without_suggestion_has_no_quick_confirm(core):
    app, _, _ = core
    app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, None, "B", .99)
    controls = ReviewControls(client(app).get("/unknowns").get_data(as_text=True))
    assert not [f for f in controls.forms if not f["in_dialog"] and f.get("action", "").endswith("/assign")]


@pytest.mark.parametrize("source", ["camera", "seed"])
def test_delete_removes_only_the_selected_review_group_without_learning_or_attendance(core, source):
    app, clock, sent = core
    store = app.store
    image = app.data_dir / "crops" / "blurry.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"original image")
    first = store.add_unknown(DAY, at(9, 10), "crops/blurry.jpg", app.service.model_id,
                              person_vector(20), None, None, source=source)
    latest = store.add_unknown(DAY, at(9, 11), None, app.service.model_id,
                               person_vector(20), None, None, group_id=first, source=source)
    other = store.add_unknown(DAY, at(9, 12), None, app.service.model_id, None, None, None, source=source)
    refs = len(store.references(app.service.model_id))
    c = client(app)
    controls = ReviewControls(c.get("/unknowns").get_data(as_text=True))
    delete = [f for f in controls.forms if not f["in_dialog"] and f.get("action") == f"/unknowns/{latest}/delete"]
    assert len(delete) == 1 and delete[0].get("data-confirm")
    assert delete[0]["method"] == "post"

    response = c.post(delete[0]["action"], data=delete[0]["fields"], follow_redirects=True)
    assert response.status_code == 200
    assert store.unknown(first).status == store.unknown(latest).status == "discarded"
    assert store.unknown(other).status == "pending"
    assert store.pending_group_count() == 1
    assert f'data-id="{latest}"' not in response.get_data(as_text=True)
    assert image.read_bytes() == b"original image"
    assert not store.ignored_groups(DAY) and not store.labelled_faces(DAY, DAY)
    assert not store.checkins(DAY) and not store.sightings(DAY) and not sent
    assert len(store.references(app.service.model_id)) == refs
    assert any(action == "delete review" and user == "admin" for _, user, action, _ in store.audit())
    clock.t = at(9, 0, day=DAY + dt.timedelta(days=1))
    assert [g[-1].id for g in store.pending_groups()] == [other]
    again = c.post(delete[0]["action"], follow_redirects=True)
    assert "already been handled" in again.get_data(as_text=True)


@pytest.mark.parametrize("status", ["assigned", "ignored", "discarded", "missing"])
def test_delete_review_rejects_missing_or_handled_faces(core, status):
    app, _, _ = core
    uid = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, None, None, None)
    before = "pending" if status == "missing" else status
    app.store.resolve_unknown(uid, before, "A" if status == "assigned" else None)
    target = uid + 100 if status == "missing" else uid
    response = client(app).post(f"/unknowns/{target}/delete", follow_redirects=True)
    assert "already been handled" in response.get_data(as_text=True)
    assert app.store.unknown(uid).status == before


def test_bulk_delete_only_removes_selected_groups(core):
    app, _, sent = core
    store = app.store
    first = store.add_unknown(DAY, at(9, 10), None, app.service.model_id, None, None, None)
    latest = store.add_unknown(DAY, at(9, 11), None, app.service.model_id, None, None, None, group_id=first)
    seed = store.add_unknown(DAY, at(9, 12), None, app.service.model_id, None, None, None, source="seed")
    other = store.add_unknown(DAY, at(9, 13), None, app.service.model_id, None, None, None)
    c = client(app)
    controls = ReviewControls(c.get("/unknowns").get_data(as_text=True))
    bulk = next(f for f in controls.forms if f.get("id") == "bulk-delete")
    assert bulk["method"] == "post" and bulk.get("data-confirm")
    checkboxes = [i for i in controls.inputs if i.get("form") == "bulk-delete"]
    assert {i["value"] for i in checkboxes} == {str(latest), str(seed), str(other)}
    assert all(i["type"] == "checkbox" and i["name"] == "unknown_id" for i in checkboxes)
    refs = len(store.references(app.service.model_id))
    response = c.post(bulk["action"], data={"unknown_id": [str(latest), str(seed)]}, follow_redirects=True)
    assert response.status_code == 200
    assert all(store.unknown(uid).status == "discarded" for uid in [first, latest, seed])
    assert store.unknown(other).status == "pending" and store.pending_group_count() == 1
    assert not store.checkins(DAY) and not store.sightings(DAY) and not sent
    assert len(store.references(app.service.model_id)) == refs


@pytest.mark.parametrize("invalid", ["", "not-an-id", "999999", "assigned", "ignored", "discarded"])
def test_bulk_delete_invalid_selection_leaves_every_group_unchanged(core, invalid):
    app, _, _ = core
    store = app.store
    valid = store.add_unknown(DAY, at(9, 10), None, app.service.model_id, None, None, None)
    other = store.add_unknown(DAY, at(9, 11), None, app.service.model_id, None, None, None)
    if invalid in {"assigned", "ignored", "discarded"}:
        store.resolve_unknown(other, invalid, "A" if invalid == "assigned" else None)
        ids = [str(valid), str(other)]
    else:
        ids = [str(valid), invalid] if invalid else []
    before = [(u.id, u.status) for u in store.unknowns(status=None)]
    response = client(app).post("/unknowns/delete", data={"unknown_id": ids}, follow_redirects=True)
    assert response.status_code == 200
    assert 'data-flash="error"' in response.get_data(as_text=True)
    assert [(u.id, u.status) for u in store.unknowns(status=None)] == before
    assert not any(action == "delete review" for _, _, action, _ in store.audit())


def test_bulk_delete_deduplicates_repeated_ids_and_members_of_the_same_group(core):
    app, _, _ = core
    first = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, None, None, None)
    second = app.store.add_unknown(DAY, at(9, 11), None, app.service.model_id, None, None, None, group_id=first)
    response = client(app).post("/unknowns/delete", data={"unknown_id": [str(first), str(second), str(first)]},
                                follow_redirects=True)
    assert response.status_code == 200
    assert app.store.unknown(first).status == app.store.unknown(second).status == "discarded"
    assert len([a for a in app.store.audit() if a[2] == "delete review"]) == 1


@pytest.mark.parametrize("source", ["camera", "seed"])
def test_confirm_selected_photos_leaves_other_people_pending(core, source):
    app, _, sent = core
    store = app.store
    first = store.add_unknown(DAY, at(9, 5), None, app.service.model_id, person_vector(30), None, None, source=source)
    chosen = store.add_unknown(DAY, at(9, 10), None, app.service.model_id, person_vector(31), None, None,
                               group_id=first, source=source)
    latest = store.add_unknown(DAY, at(9, 15), None, app.service.model_id, person_vector(32), None, None,
                               group_id=first, source=source)
    refs = len(store.references(app.service.model_id, "A"))
    response = client(app).post(f"/unknowns/{latest}/assign",
                                data={"employee_id": "A", "photo_selection": "1", "photo_id": [str(chosen)]})
    assert response.status_code == 302
    assert store.unknown(chosen).employee_id == "A" and store.unknown(chosen).status == "assigned"
    assert store.unknown(first).status == store.unknown(latest).status == "pending"
    learned = store.references(app.service.model_id, "A")[refs:]
    assert len(learned) == 1
    assert (learned[0].vector == person_vector(31)).all()
    if source == "camera":
        assert store.checkins(DAY)["A"].first_seen == at(9, 10)
        assert len(store.sightings(DAY)) == 1
    else:
        assert not store.checkins(DAY) and not sent


def test_delete_selected_photo_keeps_the_rest_of_its_group(core):
    app, _, sent = core
    first = app.store.add_unknown(DAY, at(9, 5), None, app.service.model_id, person_vector(30), None, None)
    latest = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, person_vector(31), None, None, group_id=first)
    refs = len(app.store.references(app.service.model_id))
    response = client(app).post(f"/unknowns/{latest}/delete",
                                data={"photo_selection": "1", "photo_id": [str(first)]})
    assert response.status_code == 302
    assert app.store.unknown(first).status == "discarded"
    assert app.store.unknown(latest).status == "pending"
    assert len(app.store.references(app.service.model_id)) == refs
    assert not app.store.checkins(DAY) and not app.store.sightings(DAY) and not sent


@pytest.mark.parametrize("action", ["assign", "delete", "new-employee"])
@pytest.mark.parametrize("selection", ["empty", "foreign", "handled", "not-an-id"])
def test_invalid_photo_selection_changes_nothing(core, action, selection):
    app, _, sent = core
    first = app.store.add_unknown(DAY, at(9, 5), None, app.service.model_id, person_vector(30), None, None)
    latest = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, person_vector(31), None, None, group_id=first)
    foreign = app.store.add_unknown(DAY, at(9, 15), None, app.service.model_id, person_vector(32), None, None)
    if selection == "handled":
        app.store.resolve_unknown(first, "discarded", None)
    ids = {"empty": [], "foreign": [str(latest), str(foreign)], "handled": [str(latest), str(first)],
           "not-an-id": [str(latest), "bad"]}[selection]
    before = [(u.id, u.status) for u in app.store.unknowns(status=None)]
    refs = len(app.store.references(app.service.model_id))
    response = client(app).post(f"/unknowns/{latest}/{action}", data={"photo_selection": "1", "photo_id": ids,
                                "employee_id": "A", "email": "new@example.com", "name": "New"}, follow_redirects=True)
    assert 'data-flash="error"' in response.get_data(as_text=True)
    assert [(u.id, u.status) for u in app.store.unknowns(status=None)] == before
    assert app.store.employee_count() == 3 and len(app.store.references(app.service.model_id)) == refs
    assert not app.store.checkins(DAY) and not sent


def test_multi_photo_confirm_requires_selection_and_shows_every_photo(core):
    app, _, _ = core
    first = None
    for minute in range(8):
        uid = app.store.add_unknown(DAY, at(9, minute), f"crops/{minute}.jpg", app.service.model_id,
                                    person_vector(1), None, None, group_id=first)
        first = first or uid
    c = client(app)
    controls = ReviewControls(c.get("/unknowns").get_data(as_text=True))
    assert len(json.loads(controls.cards[0]["data-photos"])) == 8
    assert not [f for f in controls.forms if not f["in_dialog"] and f.get("action", "").endswith("/assign")]
    response = c.post(f"/unknowns/{uid}/assign", data={"employee_id": "A"}, follow_redirects=True)
    assert 'data-flash="error"' in response.get_data(as_text=True)
    assert len(app.store.unknowns()) == 8 and not app.store.checkins(DAY)


def test_new_employee_is_confirmed_only_for_selected_photo(core):
    app, _, _ = core
    first = app.store.add_unknown(DAY, at(9, 5), None, app.service.model_id, person_vector(30), None, None)
    latest = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, person_vector(31), None, None, group_id=first)
    response = client(app).post(f"/unknowns/{latest}/new-employee", data={"email": "new@example.com", "name": "New",
                                "photo_selection": "1", "photo_id": [str(latest)]})
    assert response.status_code == 302
    person = app.store.employee_by_email("new@example.com")
    assert app.store.unknown(latest).employee_id == person.id
    assert app.store.unknown(first).status == "pending"
    assert app.store.checkins(DAY)[person.id].first_seen == at(9, 10)


def test_overlapping_confirmations_do_not_learn_or_record_the_losing_selection(core, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    app, _, _ = core
    first = app.store.add_unknown(DAY, at(9, 5), None, app.service.model_id, person_vector(30), None, None)
    latest = app.store.add_unknown(DAY, at(9, 10), None, app.service.model_id, person_vector(31), None, None, group_id=first)
    ready = Barrier(2)
    pending = app.service._pending_group

    def validate_together(*args, **kwargs):
        members = pending(*args, **kwargs)
        ready.wait(timeout=5)
        return members

    monkeypatch.setattr(app.service, "_pending_group", validate_together)
    clients = [client(app), client(app)]

    def confirm(index):
        return clients[index].post(f"/unknowns/{latest}/assign", data={"employee_id": "AB"[index],
                                    "photo_selection": "1", "photo_id": [str(first), str(latest)] if index else [str(first)]},
                                    follow_redirects=True).get_data(as_text=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(confirm, range(2)))
    assert sum('data-flash="error"' in page for page in pages) == 1
    winner = app.store.unknown(first).employee_id
    assert set(app.store.checkins(DAY)) == {winner}
    assert all(r.employee_id == winner for r in app.store.references(app.service.model_id) if r.source == "admin")
    assert app.store.unknown(latest).status == ("assigned" if winner == "B" else "pending")
