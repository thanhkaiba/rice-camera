"""Other systems read a person's attendance with their own key; the description is public."""
import base64

import pytest

from attendance.web import create_app, public_api
from conftest import DAY, at


@pytest.fixture
def api(core):
    app, clock, _ = core
    app.cfg.web.password = "admin-password"
    client = create_app(app).test_client()
    return app, clock, client


def admin():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:admin-password").decode()}


def key_for(app, name="Payroll"):
    return public_api.create_key(app.store, name, at(9, 0))


def get(client, key, **params):
    return client.get("/api/v1/attendance", query_string=params, headers={"Authorization": f"Bearer {key}"} if key else {})


def test_a_key_is_needed_and_a_revoked_key_stops_working(api):
    app, clock, client = api
    assert get(client, None, email="alice@example.com", date=str(DAY)).status_code == 401
    assert get(client, "att_wrong", email="alice@example.com", date=str(DAY)).status_code == 401
    key = key_for(app)
    assert get(client, key, email="alice@example.com", date=str(DAY)).status_code == 200
    assert client.get("/api/v1/attendance", query_string={"email": "alice@example.com", "date": str(DAY)},
                      headers={"X-API-Key": key}).status_code == 200
    public_api.revoke_key(app.store, public_api.keys(app.store)[0]["id"])
    assert get(client, key, email="alice@example.com", date=str(DAY)).status_code == 401
    assert key not in app.store.get(public_api.KEYS)  # only its hash is kept


def test_the_day_says_who_was_late_by_how_much(api):
    app, clock, client = api
    clock.t = at(18, 0)
    app.service.on_identified("A", at(9, 0), 0.8, None)
    app.service.on_identified("B", at(10, 15), 0.8, None)
    key = key_for(app)
    bob = get(client, key, email="bob@example.com", date=str(DAY)).get_json()
    assert bob["name"] == "Bob" and bob["timezone"]
    day = bob["days"][0]
    assert day["status"] == "late" and day["late_minutes"] == 15 and day["arrived_at"].startswith(f"{DAY}T10:15")
    assert day["final"] is True and day["workday"] is True
    alice = get(client, key, email="ALICE@example.com", date=str(DAY)).get_json()["days"][0]
    assert alice["status"] == "on_time" and alice["late_minutes"] == 0
    assert get(client, key, email="carol@example.com", date=str(DAY)).get_json()["days"][0]["status"] == "not_seen"


def test_a_range_of_days_and_wrong_requests(api):
    app, clock, client = api
    key = key_for(app)
    import datetime as dt
    week = get(client, key, email="alice@example.com", **{"from": str(DAY - dt.timedelta(days=6)), "to": str(DAY)}).get_json()
    assert len(week["days"]) == 7 and {d["status"] for d in week["days"] if not d["workday"]} <= {"day_off"}
    assert get(client, key, email="nobody@example.com", date=str(DAY)).status_code == 404
    assert get(client, key, email="alice@example.com", date="yesterday").status_code == 400
    too_long = get(client, key, email="alice@example.com", **{"from": "2026-01-01", "to": "2026-06-01"})
    assert too_long.status_code == 400 and "62" in too_long.get_json()["error"]


def test_the_description_is_public_and_admins_make_keys_in_settings(api):
    app, clock, client = api
    spec = client.get("/api/openapi.json").get_json()
    assert spec["openapi"].startswith("3.") and "/api/v1/attendance" in spec["paths"]
    assert "swagger-ui-bundle.js" in client.get("/api/docs").get_data(as_text=True)
    assert client.post("/settings/api-keys", data={"name": "HR"}).status_code == 401
    page = client.post("/settings/api-keys", data={"name": "HR"}, headers=admin()).get_data(as_text=True)
    key = page.split("user-select:all; word-break:break-all\">")[1].split("<")[0]
    assert key.startswith("att_") and get(client, key, email="alice@example.com", date=str(DAY)).status_code == 200
    settings_page = client.get("/settings", headers=admin()).get_data(as_text=True)
    assert "HR" in settings_page and key not in settings_page and "/api/docs" in settings_page
    key_id = public_api.keys(app.store)[0]["id"]
    assert client.post(f"/settings/api-keys/{key_id}/revoke", headers=admin()).status_code == 302
    assert public_api.keys(app.store) == []
