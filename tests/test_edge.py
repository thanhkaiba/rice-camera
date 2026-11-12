"""The Mac mirrors camera events to the server: nothing is lost or applied twice, and the site is closed by default."""
import base64
import datetime as dt
import gzip
import hashlib
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from attendance import server
from attendance.core.edge import EdgeClient
from attendance.store import Store
from attendance.web import edge_import
from conftest import DAY, at

TOKEN = "edge-token"
HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
PASSWORD = "a-long-admin-password"
AUTH = {"Authorization": "Bearer " + TOKEN}
IMAGE = f"crops/{DAY}/101500_A_abc.jpg"
JPEG = b"\xff\xd8jpeg"


def admin():
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}


@pytest.fixture
def site(tmp_path):
    seed = Store(tmp_path / "data" / "attendance.db")
    with seed._db:
        seed._db.executemany("INSERT INTO employees (id,email,name) VALUES (?,?,?)",
                             [("A", "alice@example.com", "Alice"), ("B", "bob@example.com", "Bob")])
    seed.close()
    application, core = server.build(tmp_path, PASSWORD, HASH, now=lambda: at(10, 30))
    return application, core, application.test_client()


def event(event_id="e1", **more):
    return {"id": event_id, "kind": "identified", "ts": at(10, 15).isoformat(), "employee_id": "A", "score": 0.8,
            "image": None, "vector": None, "sha256": None, **more}


def post(c, *events):
    return c.post("/api/edge/events", json={"events": list(events)}, headers=AUTH)


def test_server_refuses_a_weak_setup(tmp_path):
    with pytest.raises(RuntimeError):
        server.build(tmp_path, "short", HASH)
    with pytest.raises(RuntimeError):
        server.build(tmp_path, PASSWORD, "not-a-hash")


def test_only_the_mac_may_call_the_api(site):
    _, core, c = site
    assert post(c, event()).status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        assert c.post("/api/edge/events", json={"events": [event("e2")]}, headers=headers).status_code == 401
        assert c.post("/api/edge/heartbeat", json={}, headers=headers).status_code == 401
        assert c.put(f"/api/edge/photos/{IMAGE}", data=JPEG, headers=headers).status_code == 401
    assert not core.store.edge_applied("e2")


def test_pages_need_the_admin_login_and_health_does_not(site):
    _, _, c = site
    assert c.get("/healthz").get_data(as_text=True) == "ok\n"
    assert c.get("/").status_code == 401
    assert c.get("/", headers=admin()).status_code == 200


def test_shadow_mode_changes_nothing_and_posts_nothing(site):
    _, core, c = site
    assert c.post("/employees/A", data={"name": "X", "active": "on"}, headers=admin()).status_code == 403
    assert core.store.employee("A").name == "Alice"
    post(c, event())
    assert core.store.checkins(DAY)["A"].source == "camera"
    assert core.store.outbox() == []


def test_a_resent_event_is_applied_once(site):
    _, core, c = site
    assert post(c, event()).get_json() == {"accepted": ["e1"], "rejected": []}
    assert post(c, event()).get_json() == {"accepted": ["e1"], "rejected": []}
    assert len(core.store.sightings(DAY, "A")) == 1


def test_a_bad_event_does_not_block_the_good_ones(site):
    _, core, c = site
    answer = post(c, event("bad", ts="yesterday"), event("no-zone", ts="2026-10-14T10:15:00"), event("path", image="../../x.jpg"),
                  event("kind", kind="nope"), event("good")).get_json()
    assert answer["accepted"] == ["good"]
    assert {r["id"] for r in answer["rejected"]} == {"bad", "no-zone", "path", "kind"}
    assert len(core.store.sightings(DAY, "A")) == 1


def test_unknown_face_becomes_a_review_card_with_its_vector(site):
    _, core, c = site
    vector = base64.b64encode(np.ones(512, dtype=np.float32).tobytes()).decode()
    answer = post(c, {"id": "u1", "kind": "unknown", "ts": at(10, 16).isoformat(), "image": None, "vector": vector,
                      "guess_id": None, "guess_score": None, "sha256": None, "quality": 0.5,
                      "track": [0, 7], "follows": None, "frames": 5, "votes": 1}).get_json()
    assert answer["accepted"] == ["u1"]
    assert core.store.pending_group_count(DAY) == 1
    assert core.store.pending_groups()[0][0].vector.shape == (512,)


@pytest.mark.parametrize("rel", ["faces/2026-10-14/a.jpg", "crops/2026-10-14/a.png", "crops/2026-10-14/..%2F..%2Fa.jpg",
                                 "crops/x/a.jpg", "attendance.db"])
def test_photos_only_go_under_crops(site, rel):
    _, core, c = site
    assert c.put(f"/api/edge/photos/{rel}", data=JPEG, headers=AUTH).status_code in (400, 404)
    assert not list(core.data_dir.glob("**/a.*"))


def test_photo_is_saved_and_must_be_a_jpeg(site):
    _, core, c = site
    assert c.put(f"/api/edge/photos/{IMAGE}", data=b"not a jpeg", headers=AUTH).status_code == 415
    assert not (core.data_dir / IMAGE).exists()
    assert c.put(f"/api/edge/photos/{IMAGE}", data=JPEG, headers=AUTH).status_code == 200
    assert (core.data_dir / IMAGE).read_bytes() == JPEG


def test_heartbeat_tells_the_server_how_the_camera_is(site):
    _, core, c = site
    assert server.camera_status(core) == (False, "the office Mac has not connected yet")
    c.post("/api/edge/heartbeat", json={"camera_ok": True, "camera_state": "ok", "backlog": 2}, headers=AUTH)
    assert server.camera_status(core) == (True, "ok")
    beat = json.loads(core.store.get("edge_heartbeat"))
    beat["at"] = (at(10, 30) - dt.timedelta(minutes=10)).isoformat()
    core.store.put("edge_heartbeat", json.dumps(beat))
    ok, why = server.camera_status(core)
    assert not ok and "10 min" in why


def client_for(tmp_path, site, *, fail=None):
    _, _, c = site
    store = Store(tmp_path / "mac" / "attendance.db")
    data = tmp_path / "mac"

    def send(method, path, body, content_type):
        if fail and fail(path):
            raise ConnectionError("network down")
        answer = c.open(path, method=method, data=body, content_type=content_type, headers=AUTH)
        return answer.status_code, answer.get_data()

    return EdgeClient(SimpleNamespace(server_url="https://x", token=TOKEN), store, data, send=send), store, data


def test_mac_delivers_event_and_photos_then_forgets_them(tmp_path, site):
    _, core, _ = site
    edge, store, data = client_for(tmp_path, site)
    (data / IMAGE).parent.mkdir(parents=True)
    (data / IMAGE).write_bytes(JPEG)
    edge.identified("A", at(10, 15), 0.8, IMAGE, np.ones(512), "sha")
    assert edge.deliver_batch() == 1
    assert store.edge_backlog() == 0
    assert core.store.checkins(DAY)["A"].image == IMAGE
    assert (core.data_dir / IMAGE).read_bytes() == JPEG


def test_mac_keeps_events_while_the_server_is_down(tmp_path, site):
    _, core, _ = site
    down = {"yes": True}
    edge, store, _ = client_for(tmp_path, site, fail=lambda path: down["yes"])
    edge.identified("A", at(10, 15), 0.8, None, None, None)
    with pytest.raises(ConnectionError):
        edge.deliver_batch()
    assert store.edge_backlog() == 1 and "A" not in core.store.checkins(DAY)
    store.close()
    reopened = Store(tmp_path / "mac" / "attendance.db")  # the Mac restarted meanwhile
    assert reopened.edge_backlog() == 1
    edge.store = reopened
    down["yes"] = False
    assert edge.deliver_batch() == 1
    assert core.store.checkins(DAY)["A"].first_seen == at(10, 15)


def test_a_lost_answer_does_not_duplicate(tmp_path, site):
    _, core, c = site
    edge, store, _ = client_for(tmp_path, site)
    edge.identified("A", at(10, 15), 0.8, None, None, None)
    real = edge._send

    def answer_lost(method, path, body, content_type):
        real(method, path, body, content_type)
        raise ConnectionError("answer lost")

    edge._send = answer_lost
    with pytest.raises(ConnectionError):
        edge.deliver_batch()
    edge._send = real
    assert edge.deliver_batch() == 1
    assert len(core.store.sightings(DAY, "A")) == 1 and store.edge_backlog() == 0


def test_camera_loop_survives_a_broken_queue(tmp_path, site):
    edge, store, _ = client_for(tmp_path, site)
    store.close()
    edge.identified("A", at(10, 15), 0.8, None, None, None)  # must not raise


def test_no_server_configured_changes_nothing(core):
    app, _, _ = core
    assert app.service.edge is None and not app.edge.enabled


# The one-time copy from the Mac ------------------------------------------------------------------

def mac_database(tmp_path):
    folder = tmp_path / "mac"
    folder.mkdir(exist_ok=True)
    mac = Store(folder / "attendance.db")
    with mac._db:
        mac._db.execute("INSERT INTO employees (id,email,name) VALUES ('Z','z@example.com','Zed')")
    mac.put("settings", json.dumps({"camera": {"url": "rtsp://secret", "process_fps": 5}, "web": {"password": "old"},
                                    "backup": {"folder": "/Volumes/x", "time": "22:00"},
                                    "google_chat": {"webhook_url": "https://chat.example/hook"}}))
    mac.enqueue({"text": "still to post"}, at(10, 0))
    mac.close()
    return folder


def upload_database(c, folder, tmp_path):
    from attendance.migrate import snapshot

    copy = tmp_path / "snapshot.db"
    copy.unlink(missing_ok=True)
    counts = snapshot(folder / "attendance.db", copy)
    return counts, c.put("/api/edge/import/database", data=gzip.compress(copy.read_bytes()), headers=admin())


@pytest.fixture
def no_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(edge_import, "restart_worker", lambda: calls.append(1))
    return calls


def test_the_copy_needs_the_admin_login_not_the_mac_token(site):
    _, _, c = site
    for headers in ({}, AUTH):
        assert c.get("/api/edge/import/manifest", headers=headers).status_code == 401
        assert c.put("/api/edge/import/database", data=b"x", headers=headers).status_code == 401
        assert c.put("/api/edge/import/photo/faces/A/a.jpg", data=JPEG, headers=headers).status_code == 401


def test_the_copy_is_closed_once_the_site_is_live(tmp_path):
    application, _ = server.build(tmp_path, PASSWORD, HASH, shadow=False, now=lambda: at(10, 30))
    c = application.test_client()
    assert c.get("/api/edge/import/manifest", headers=admin()).status_code == 404
    assert c.put("/api/edge/import/database", data=b"x", headers=admin()).status_code == 404


@pytest.mark.parametrize("rel", ["faces/../../etc/a.jpg", "other/a.jpg", "faces/A/a.png", "faces/A/.hidden.jpg"])
def test_a_photo_can_only_land_in_the_photo_folders(site, rel):
    _, _, c = site
    assert c.put("/api/edge/import/photo/" + rel, data=JPEG, headers=admin()).status_code in (400, 404)


def test_photos_arrive_and_are_listed(site):
    _, core, c = site
    assert c.put("/api/edge/import/photo/faces/A/one.jpg", data=JPEG, headers=admin()).status_code == 200
    assert c.put("/api/edge/import/photo/faces/A/two.jpg", data=b"not a jpeg", headers=admin()).status_code == 415
    assert (core.data_dir / "faces/A/one.jpg").read_bytes() == JPEG
    assert c.get("/api/edge/import/manifest", headers=admin()).get_json() == {"faces/A/one.jpg": len(JPEG)}


def test_the_database_replaces_the_old_one_at_the_next_start_without_mac_secrets(tmp_path, site, no_restart):
    _, core, c = site
    counts, answer = upload_database(c, mac_database(tmp_path), tmp_path)
    assert answer.status_code == 200 and answer.get_json() == counts and no_restart == [1]
    assert core.store.employee_count() == 2  # the running worker is not disturbed
    core.store.close()

    application, again = server.build(tmp_path, PASSWORD, HASH, now=lambda: at(10, 30))
    assert list(again.store.employees()) == ["Z"]
    saved = json.loads(again.store.get("settings"))
    assert "url" not in saved["camera"] and saved["camera"]["process_fps"] == 5
    assert "password" not in saved["web"] and "folder" not in saved["backup"]
    assert saved["google_chat"]["webhook_url"]  # the server posts to Chat after the cutover
    assert again.store.outbox() == []
    assert application.test_client().get("/api/edge/import/status", headers=admin()).get_json() == counts
    assert not (tmp_path / "data" / edge_import.PENDING).exists()


@pytest.mark.parametrize("body", [b"junk", gzip.compress(b"junk"), gzip.compress(b"SQLite format 3\x00" + b"x" * 200)])
def test_a_bad_database_is_refused_and_nothing_changes(tmp_path, site, no_restart, body):
    _, core, c = site
    assert c.put("/api/edge/import/database", data=body, headers=admin()).status_code == 400
    assert no_restart == [] and not (core.data_dir / edge_import.PENDING).exists()
    assert [p.name for p in core.data_dir.iterdir() if p.name.endswith(".import")] == []


def test_shadow_never_posts_even_what_is_already_queued(tmp_path, monkeypatch):
    seed = Store(tmp_path / "data" / "attendance.db")
    seed.put("settings", json.dumps({"google_chat": {"webhook_url": "https://chat.example/hook"}}))
    seed.enqueue({"text": "queued"}, at(10, 0))
    seed.close()
    _, core = server.build(tmp_path, PASSWORD, HASH, now=lambda: at(10, 30))
    posted = []
    monkeypatch.setattr("attendance.integrations.notifier.GoogleChat._post", lambda self, *a: posted.append(a), raising=False)
    core.service.alert("anything")
    core.chat.kick(at(10, 30))
    assert posted == [] and len(core.store.outbox()) == 1


def test_shadow_never_reports_late_arrivals_to_the_camera_api(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr("attendance.integrations.camera_api.post", lambda *a: posted.append(a))
    _, core = server.build(tmp_path, PASSWORD, HASH, now=lambda: at(18, 0))
    core.cfg.camera_api.url, core.cfg.camera_api.key = "https://api.example/x", "key"
    core.camera_sync.sync()
    assert posted == []


def test_config_file_keeps_only_what_the_server_can_use(site, tmp_path):
    _, core, c = site
    text = "timezone: Asia/Ho_Chi_Minh\ndata_dir: /Mac/data\nweb:\n  port: 8000\nrecognition:\n  det_size: 640\ncamera:\n  url: rtsp://x\n"
    assert c.put("/api/edge/import/config", data=text, headers=admin()).status_code == 200
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert saved == {"timezone": "Asia/Ho_Chi_Minh", "recognition": {"det_size": 640}}
    assert c.put("/api/edge/import/config", data="nonsense_key: 1\n", headers=admin()).status_code == 400
    assert c.put("/api/edge/import/config", data=text, headers=AUTH).status_code == 401


def test_the_server_points_to_the_office_for_anything_about_the_camera(site):
    _, _, c = site
    home = c.get("/report", headers=admin()).get_data(as_text=True)
    assert 'href="/live"' not in home
    page = c.get("/live", headers=admin())
    assert page.status_code == 200 and "http://192.168.10.243:8000/live" in page.get_data(as_text=True)
    settings_page = c.get("/settings", headers=admin()).get_data(as_text=True)
    assert "192.168.10.x" in settings_page and "http://192.168.10.243:8000/live" in settings_page
    assert 'data-camera-check="auto"' not in settings_page


def test_the_office_mac_keeps_its_camera_page(tmp_path):
    from attendance.core.config import Config
    from attendance.core.pipeline import App
    from attendance.web import create_app

    cfg = Config()
    cfg.base_dir = tmp_path
    c = create_app(App(cfg, now=lambda: at(10, 30))).test_client()
    assert 'href="/live"' in c.get("/report").get_data(as_text=True)
    assert "only available on the office computer" not in c.get("/live").get_data(as_text=True)
    settings_page = c.get("/settings").get_data(as_text=True)
    assert 'data-camera-check="auto"' in settings_page and "can only be checked from the office" not in settings_page


# What admins do on the Mac reaches the server -------------------------------------------------------

def mirror_to(site, tmp_path):
    application, core, c = site
    mac = Store(tmp_path / "mac2.db")
    with mac._db:
        mac._db.execute("INSERT INTO employees (id,email,name) VALUES ('A','alice@example.com','Alice'),('C','c@example.com','Cara')")
    def send(method, path, body, content_type):
        answer = c.open(path, method=method, data=body, content_type=content_type, headers=AUTH)
        return answer.status_code, answer.data
    (tmp_path / "macdata").mkdir(exist_ok=True)
    return core, mac, EdgeClient(SimpleNamespace(server_url="x", token="t"), mac, tmp_path / "macdata", send=send, sleep=lambda s: None)


def sighting(mac, sid, employee, ts, source="camera", image=None):
    with mac._db:
        mac._db.execute("INSERT INTO sightings (id,day,employee_id,ts,source,image) VALUES (?,?,?,?,?,?)",
                        (sid, str(DAY), employee, ts.isoformat(), source, image))


def rows(core, sql):
    return [tuple(r) for r in core.store._all(sql)]


def test_admin_work_on_the_mac_reaches_the_server_and_only_once_the_queue_is_empty(site, tmp_path):
    core, mac, edge = mirror_to(site, tmp_path)
    edge.identified("A", at(9, 0), 0.8, None, None, None)
    sighting(mac, 7, "C", at(9, 5), "admin")
    edge.sync_state()
    assert rows(core, "SELECT id FROM sightings WHERE id = 7") == []  # the camera event goes first
    assert edge.deliver_batch() == 1
    edge.sync_state()
    assert rows(core, "SELECT id, employee_id, source FROM sightings WHERE id = 7") == [(7, "C", "admin")]
    assert sorted(core.store.employees()) == ["A", "C"]


def test_a_correction_replaces_the_day_and_nothing_is_sent_when_nothing_changed(site, tmp_path):
    core, mac, edge = mirror_to(site, tmp_path)
    sighting(mac, 1, "A", at(9, 0), "admin")
    edge.sync_state()
    with mac._db:
        mac._db.execute("UPDATE sightings SET employee_id = 'C' WHERE id = 1")
    edge.sync_state()
    assert rows(core, "SELECT employee_id FROM sightings") == [("C",)]
    calls = []
    original = edge._send
    edge._send = lambda *a: (calls.append(a), original(*a))[1]
    edge.sync_state()
    assert calls == []


def test_learned_faces_follow_and_deleted_ones_go(site, tmp_path):
    core, mac, edge = mirror_to(site, tmp_path)
    vector = np.ones(4, dtype=np.float32).tobytes()
    with mac._db:
        mac._db.execute("INSERT INTO embeddings (id,model,employee_id,vector,source,created_at) VALUES (1,'m','A',?,'x','t'),(2,'m','A',?,'x','t')", (vector, vector))
    edge.sync_state()
    assert rows(core, "SELECT id, vector FROM embeddings") == [(1, vector), (2, vector)]
    with mac._db:
        mac._db.execute("DELETE FROM embeddings WHERE id = 1")
    edge.sync_state()
    assert rows(core, "SELECT id FROM embeddings") == [(2,)]


def test_the_server_asks_for_photos_it_does_not_have(site, tmp_path):
    core, mac, edge = mirror_to(site, tmp_path)
    (tmp_path / "macdata" / "crops" / str(DAY)).mkdir(parents=True)
    (tmp_path / "macdata" / IMAGE).write_bytes(JPEG)
    sighting(mac, 1, "A", at(9, 0), "admin", IMAGE)
    edge.sync_state()
    assert (core.data_dir / IMAGE).read_bytes() == JPEG


def test_shadow_notes_camera_arrivals_only_one_side_saw(site, tmp_path):
    application, core, c = site
    _, mac, edge = mirror_to(site, tmp_path)
    assert edge.identified("A", at(9, 0), 0.8, None, None, None) is None
    edge.deliver_batch()
    sighting(mac, 50, "A", at(9, 30))
    edge.sync_state()
    report = c.get("/api/edge/import/shadow", headers=admin()).get_json()[str(DAY)]
    assert len(report["only_server"]) == 1 and len(report["only_mac"]) == 1 and report["mac_camera"] == 1


def test_a_bad_state_update_changes_nothing(site):
    _, core, c = site
    good = c.post("/api/edge/state", json={"days": [str(DAY)], "tables": {"sightings": [
        {"day": "1999-01-01", "employee_id": "A", "ts": "x", "source": "camera"}]}}, headers=AUTH)
    assert good.status_code == 400 and rows(core, "SELECT * FROM sightings") == []
    assert c.post("/api/edge/state", json={"days": []}).status_code == 401


def test_a_live_server_takes_no_state_from_the_mac(tmp_path):
    application, _ = server.build(tmp_path, PASSWORD, HASH, shadow=False, now=lambda: at(10, 30))
    assert application.test_client().post("/api/edge/state", json={"days": []}, headers=AUTH).status_code == 404


def test_the_shadow_site_has_no_stream_settings_and_says_it_only_mirrors(site):
    _, _, c = site
    settings_page = c.get("/settings", headers=admin()).get_data(as_text=True)
    assert 'name="camera.url"' not in settings_page and 'name="camera.more"' not in settings_page
    assert "192.168.10.x" in settings_page
    assert "only mirrors the office computer" in c.get("/report", headers=admin()).get_data(as_text=True)


# The server in charge ---------------------------------------------------------------------------------

@pytest.fixture
def live(tmp_path):
    seed = Store(tmp_path / "data" / "attendance.db")
    with seed._db:
        seed._db.executemany("INSERT INTO employees (id,email,name) VALUES (?,?,?)", [("A", "a@example.com", "Alice")])
        seed._db.execute("INSERT INTO embeddings (id,model,employee_id,vector,source,created_at) VALUES (5,'m','A',?,'x','t')",
                         (np.ones(4, dtype=np.float32).tobytes(),))
    seed.close()
    application, core = server.build(tmp_path, PASSWORD, HASH, shadow=False, now=lambda: at(10, 30))
    return application, core, application.test_client()


def follower(live, tmp_path):
    _, _, c = live
    mac = Store(tmp_path / "mac3.db")
    def send(method, path, body, content_type):
        answer = c.open(path, method=method, data=body, content_type=content_type, headers=AUTH)
        return answer.status_code, answer.data
    (tmp_path / "macdata").mkdir(exist_ok=True)
    return mac, EdgeClient(SimpleNamespace(server_url="x", token="t"), mac, tmp_path / "macdata", send=send, sleep=lambda s: None)


def test_the_mac_learns_the_server_is_in_charge_and_remembers(live, tmp_path):
    mac, edge = follower(live, tmp_path)
    assert not edge.following
    edge.heartbeat()
    assert edge.following
    assert EdgeClient(SimpleNamespace(server_url="x", token="t"), mac, tmp_path / "macdata").following


def test_people_faces_and_settings_come_from_the_server(live, tmp_path):
    _, core, _ = live
    mac, edge = follower(live, tmp_path)
    configs, reloads = [], []
    edge.on_config, edge.on_gallery = configs.append, lambda: reloads.append(1)
    with mac._db:
        mac._db.execute("INSERT INTO embeddings (id,model,employee_id,vector,source,created_at) VALUES (9,'m','Z',x'00','x','t')")
    edge.follow()
    assert [e for e in mac.employees()] == ["A"]
    assert [tuple(r) for r in mac._all("SELECT id, employee_id FROM embeddings")] == [(5, "A")]
    assert "area" not in configs[0]["camera"] and "url" in configs[0]["camera"] and reloads == [1]
    edge.follow()  # nothing new: nothing is applied again
    assert len(configs) == 1 and reloads == [1]
    core.cfg.recognition.accept_score = 0.61
    edge.follow()
    assert configs[1]["recognition"]["accept_score"] == 0.61


def test_a_photo_uploaded_on_the_server_is_read_by_the_mac(live, tmp_path):
    _, core, c = live
    mac, edge = follower(live, tmp_path)
    edge.heartbeat()
    jpeg = __import__("cv2").imencode(".jpg", np.zeros((20, 20, 3), dtype=np.uint8))[1].tobytes()
    answer = c.post("/employees/A/photos", data={"photos": (io.BytesIO(jpeg), "a.jpg")}, headers=admin(), content_type="multipart/form-data")
    assert answer.status_code == 302
    calls = []
    def add_photo(employee_id, data, source):
        calls.append((employee_id, data))
        return SimpleNamespace(ok=False, message="No face found", warnings=[])
    edge.add_photo = add_photo
    edge.follow()
    assert calls == [("A", jpeg)] and core.store.edge_jobs() == []
    assert not list((core.data_dir / "uploads").iterdir())
    assert "No face found" in c.get("/audit", headers=admin()).get_data(as_text=True)
    edge.follow()
    assert len(calls) == 1


def test_references_and_alerts_made_on_the_mac_reach_the_server(live, tmp_path):
    _, core, c = live
    mac, edge = follower(live, tmp_path)
    edge.reference("A", np.ones(4, dtype=np.float32), "enrol", at(9, 0), None, True, 0.5, "m")
    edge.alert("power is off")
    assert edge.deliver_batch() == 2
    assert [r["source"] for r in core.store._all("SELECT source FROM embeddings ORDER BY id")] == ["x", "enrol"]


def test_the_live_server_can_edit_the_stream_and_the_area_stays_at_the_office(live):
    _, _, c = live
    page = c.get("/settings", headers=admin()).get_data(as_text=True)
    assert 'name="camera.url"' in page and 'name="camera.area"' not in page and "only mirrors" not in page


def test_the_shadow_server_receives_the_address_it_will_hand_back(site, tmp_path):
    core, mac, edge = mirror_to(site, tmp_path)
    edge.camera_address = lambda: {"url": "rtsp://cam/1", "more": ""}
    edge.sync_state()
    assert core.cfg.camera.url == "rtsp://cam/1"


def test_a_mac_following_the_server_only_shows_the_camera(tmp_path):
    from attendance.core.config import Config
    from attendance.core.pipeline import App
    from attendance.web import create_app

    cfg = Config()
    cfg.base_dir = tmp_path
    cfg.edge.server_url, cfg.edge.token = "https://server.example", "t"
    core = App(cfg, now=lambda: at(10, 30))
    core.edge.following = True
    c = create_app(core).test_client()
    assert c.get("/").headers["Location"].endswith("/live")
    assert c.get("/report?day=1").headers["Location"] == "https://server.example/report?day=1"
    assert c.post("/settings", data={}).status_code == 403
    assert c.get("/live").status_code == 200
    page = c.get("/live").get_data(as_text=True)
    assert 'href="/report"' not in page and 'href="/live"' in page
    core.service.chat.send("hello", None, at(10, 30))
    assert core.store.outbox() == []
    core.service.alert("power")
    assert json.loads(core.store.edge_pending(5)[0]["payload"])["kind"] == "alert"


def test_a_mac_never_loses_its_camera_to_an_empty_server_address(tmp_path):
    from attendance.core.config import Config
    from attendance.core.pipeline import App

    cfg = Config()
    cfg.base_dir = tmp_path
    cfg.camera.url = "rtsp://cam/1"
    core = App(cfg, now=lambda: at(10, 30))
    core._config_from_server({"camera": {"url": "", "more": "", "process_fps": 7.0}})
    assert cfg.camera.url == "rtsp://cam/1" and cfg.camera.process_fps == 7.0


def test_a_second_job_is_done_even_when_the_server_gives_the_same_id(live, tmp_path):
    _, core, c = live
    mac, edge = follower(live, tmp_path)
    said = []
    edge.on_say = lambda: said.append(1) or {"ok": True, "message": f"test {len(said)}"}
    for _ in range(2):
        assert c.post("/settings/greeting/test", headers=admin()).status_code == 302
        edge.follow()
    assert said == [1, 1] and core.store.edge_jobs() == []
    page = c.get("/audit", headers=admin()).get_data(as_text=True)
    assert "said: test 1" in page and "said: test 2" in page
    core.store.edge_job_add("say", {"kind": "say"}, core.service.now())
    assert core.store.edge_jobs()[0]["id"] == 3  # never 1 again, though the table was empty
    mac.put("edge_jobs_unsent", json.dumps({"3": {"ok": True, "message": "sent before"}}))
    edge.follow()  # a result the server did not get is sent again, not done twice
    assert said == [1, 1] and "said: sent before" in c.get("/audit", headers=admin()).get_data(as_text=True)
    assert json.loads(mac.get("edge_jobs_unsent")) == {}


def test_the_mac_hears_who_has_an_arrival_on_the_server(live, tmp_path):
    _, core, _ = live
    mac, edge = follower(live, tmp_path)
    heard = []
    edge.on_arrived = heard.append
    edge.follow()
    assert heard[-1] == {"day": at(10, 30).date().isoformat(), "people": []}
    core.service.set_arrival(at(10, 30).date(), "A", at(9, 0).time(), "admin")
    edge.follow()
    assert heard[-1]["people"] == ["A"]


def test_evidence_label_is_written_without_accents():
    from attendance.core import pipeline

    seen = {}
    import cv2
    real = cv2.putText
    try:
        cv2.putText = lambda img, text, *a, **k: seen.setdefault("text", text) and img
        pipeline.render_scene(np.zeros((90, 160, 3), dtype=np.uint8), (10, 10, 40, 40), "Hồ Thị Hằng Đỗ  match 0.54")
    finally:
        cv2.putText = real
    assert seen["text"] == "Ho Thi Hang Do  match 0.54"
