"""Review in bulk: after a confirmation the other cards of that person are offered, several cards
can be named as one person at once, and the page's background requests get JSON answers."""
import datetime as dt

import numpy as np
import pytest

from conftest import DAY, at, person_vector
from test_web import client


def _unit(v):
    return v / np.linalg.norm(v)


def _four_cards(service):
    """Two cards of one unknown person (a clean and a noisy face), a card of someone else, and a
    card of the first person from another day."""
    same, noisy, other = person_vector(7), _unit(person_vector(7) + 0.3 * person_vector(8)), person_vector(9)
    a = service.on_unknown(at(8, 30), "a.jpg", same, None, None)
    b = service.on_unknown(at(8, 40), "b.jpg", noisy, None, None)
    c = service.on_unknown(at(8, 50), "c.jpg", other, None, None)
    d = service.on_unknown(at(8, 30, day=DAY + dt.timedelta(days=1)), "d.jpg", same, None, None)
    return a, b, c, d


def test_confirming_a_card_offers_the_other_cards_of_that_person_from_that_day(env):
    service, store, clock, sent = env
    a, b, c, d = _four_cards(service)
    confirmed = service.resolve_unknown(a, "B")
    assert [u.id for u in confirmed] == [a]
    found = service.similar_pending(confirmed)
    assert [g[-1].id for g, score in found] == [b]  # not the stranger, not the other day
    assert found[0][1] > 0.9
    for u in (b, c, d):
        assert store.unknown(u).status == "pending"  # offered, not confirmed
    service.cfg.recognition.same_face_score = 0  # off
    assert service.similar_pending(confirmed) == []


def test_several_cards_are_confirmed_as_one_person_at_once(env):
    service, store, clock, sent = env
    a, b, c, d = _four_cards(service)
    confirmed = service.resolve_unknowns([b, a], "B")
    assert {u.id for u in confirmed} == {a, b}
    assert store.unknown(a).employee_id == store.unknown(b).employee_id == "B"
    assert store.checkins(DAY)["B"].first_seen == at(8, 30)  # the earliest card
    assert store.unknown(c).status == "pending"
    with pytest.raises(ValueError):
        service.resolve_unknowns([], "B")
    with pytest.raises(ValueError):  # one card already handled: nothing is confirmed
        service.resolve_unknowns([c, a], "B")
    assert store.unknown(c).status == "pending"
    with pytest.raises(ValueError):
        service.resolve_unknowns([c], "nobody")


def test_review_actions_answer_json_in_the_background(core):
    app, clock, sent = core
    a, b, c, d = _four_cards(app.service)
    web = client(app)
    headers = {"X-Requested-With": "fetch"}
    page = web.get("/unknowns").get_data(as_text=True)
    assert 'id="name-selected"' in page and "/unknowns/assign-many" in page and 'id="related"' in page

    r = web.post(f"/unknowns/{a}/assign", data={"employee_id": "B"}, headers=headers)
    assert r.status_code == 200 and r.is_json
    data = r.get_json()
    assert data["ok"] and data["message"] == "Confirmed as Bob (bob); face learned."
    assert data["related"]["employee_id"] == "B" and data["related"]["label"] == "Bob (bob)"
    assert [card["id"] for card in data["related"]["cards"]] == [b]
    assert data["related"]["cards"][0]["count"] == 1

    r = web.post("/unknowns/assign-many", data={"employee_id": "B", "unknown_id": [b, c]}, headers=headers)
    data = r.get_json()
    assert r.status_code == 200 and data["ok"] and data["message"] == "2 cards confirmed as Bob (bob); faces learned."
    assert data["related"]["cards"] == []
    assert app.store.unknown(b).status == app.store.unknown(c).status == "assigned"

    r = web.post(f"/unknowns/{a}/assign", data={"employee_id": "B"}, headers=headers)
    assert r.status_code == 400 and not r.get_json()["ok"] and "already been handled" in r.get_json()["message"]
    r = web.post(f"/unknowns/{d}/ignore", headers=headers)
    assert r.status_code == 200 and r.get_json()["ok"] and "related" not in r.get_json()

    # A plain form post (no script) is still answered with a redirect back to Review.
    r = web.post("/unknowns/assign-many", data={"employee_id": "B"})
    assert r.status_code == 302


def test_review_page_draws_its_own_employee_suggestions(core):
    """The name boxes no longer rely on the browser's datalist picker, which phones hide or skip."""
    app, clock, sent = core
    _four_cards(app.service)
    html = client(app).get("/unknowns").data.decode()
    assert 'list="employee-options"' not in html
    assert 'id="pick-suggest"' in html and 'id="bulk-suggest"' in html
    assert 'id="employee-options"' in html  # still the list of people the script reads
