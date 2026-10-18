import base64
import datetime as dt
import io

import numpy as np

from attendance.web import create_app
from conftest import DAY, at, person_vector


def client(core_app, password=""):
    core_app.cfg.web.password = password
    app = create_app(core_app)
    app.testing = True
    return app.test_client()


def test_pages_render_and_admin_can_assign_unknown(core):
    app, clock, sent = core
    service, store = app.service, app.store
    clock.t = at(10, 30)
    service.on_identified("A", at(9, 5), 0.7, "crops/a.jpg")
    uid = service.on_unknown(at(9, 20), "crops/u.jpg", person_vector(2), "B", 0.4)
    c = client(app)

    page = c.get("/").get_data(as_text=True)
    assert "Alice" in page and "09:05" in page and page.count('data-status="not_seen"') == 2

    page = c.get("/unknowns").get_data(as_text=True)
    assert "Looks like <strong>Bob (bob@example.com)</strong>" in page
    assert 'data-guess-name="Bob (bob@example.com)"' in page
    assert "openReview(first)" not in page  # nothing opens by itself
    page = c.get("/unknowns?review=first").get_data(as_text=True)  # the link posted in chat
    assert f'.face-card[data-id="{uid}"]' in page and "openReview(first)" in page
    assert "/media/crops/u.jpg" in page

    resp = c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"})
    assert resp.status_code == 302
    assert "B" in store.checkins(DAY)
    again = c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"}, follow_redirects=True)
    assert "already been handled" in again.get_data(as_text=True)


def test_password_protects_pages(core):
    app, _, _ = core
    c = client(app, password="s3cret")
    assert c.get("/").status_code == 401
    token = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200


def png(image) -> bytes:
    import cv2

    return cv2.imencode(".png", image)[1].tobytes()


def test_employee_management_and_photo_upload(core):
    from conftest import face_image

    app, _, sent = core
    c = client(app)
    resp = c.post("/employees", data={"email": "dan@example.com", "name": "Dan"}, follow_redirects=True)
    employee_id = app.store.employee_by_email("dan@example.com").id
    assert "Employee added" in resp.get_data(as_text=True)
    assert "No photos" in c.get("/employees").get_data(as_text=True)

    resp = c.post(f"/employees/{employee_id}/photos", data={"photos": [(io.BytesIO(png(face_image(4))), "d1.png"),
                                                          (io.BytesIO(png(face_image(0))), "empty.png"),
                                                          (io.BytesIO(b"not an image"), "x.txt")]},
                  content_type="multipart/form-data", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "1 photo(s) added" in page and "empty.png: No face found" in page and "x.txt: not a readable image" in page
    image = app.store.references(app.library.model_id, employee_id)[0].image
    assert c.get(f"/media/{image}").status_code == 200

    # A photo of Alice uploaded for Dan triggers the look-alike warning.
    resp = c.post(f"/employees/{employee_id}/photos", data={"photos": [(io.BytesIO(png(face_image(1))), "oops.png")]},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "looks like Alice" in resp.get_data(as_text=True)
    assert "have reference photos that look" in c.get("/employees").get_data(as_text=True)

    c.post(f"/employees/{employee_id}/photos/delete", data={"image": image})
    assert not (app.data_dir / image).exists()

    c.post(f"/employees/{employee_id}", data={"name": "Daniel"})  # unticked "active"
    assert app.store.employee(employee_id).name == "Daniel" and not app.store.employee(employee_id).active
    c.post(f"/employees/{employee_id}/delete", data={"confirm": "wrong"})
    assert app.store.employee(employee_id) is not None
    c.post(f"/employees/{employee_id}/delete", data={"confirm": "dan@example.com"})
    assert app.store.employee(employee_id) is None

    resp = c.post("/employees/import", data={"file": (io.BytesIO("email,name\neve@example.com,Eve\n".encode()), "e.csv")},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "Imported 1 employees" in resp.get_data(as_text=True)


def test_attendance_corrections_from_the_page(core):
    app, clock, _ = core
    c = client(app)
    clock.t = at(10, 30)
    app.service.on_identified("A", at(9, 5), 0.8, None)
    c.post(f"/day/{DAY}/A/reassign", data={"to": "B"})
    c.post(f"/day/{DAY}/C/set", data={"time": "09:20"})
    resp = c.post(f"/day/{DAY}/C/set", data={"time": "nine"}, follow_redirects=True)
    assert "HH:MM" in resp.get_data(as_text=True)
    assert set(app.store.checkins(DAY)) == {"B", "C"}
    c.post(f"/day/{DAY}/B/delete")
    assert set(app.store.checkins(DAY)) == {"C"}
    assert "reassign arrival" in c.get("/audit").get_data(as_text=True)


def test_settings_page_saves_and_keeps_password(core):
    app, _, _ = core
    c = client(app)
    assert "Working hours" in c.get("/settings").get_data(as_text=True)
    form = {"schedule.window_start": "08:30", "schedule.late_after": "09:30", "schedule.window_end": "11:00",
            "schedule.work_days": ["0", "1", "2", "3", "4", "5"], "camera.url": "rtsp://cam", "camera.process_fps": "4",
            "recognition.accept_score": "0.5", "recognition.min_margin": "0.08", "recognition.votes_required": "3",
            "recognition.unknown_min_frames": "3", "recognition.min_face_px": "60", "recognition.min_det_score": "0.65",
            "recognition.min_sharpness": "40", "recognition.max_yaw": "35", "recognition.max_pitch": "30",
            "google_chat.webhook_url": "https://hook", "google_chat.unknown_batch_minutes": "5",
            "leave_api.url": "https://hr/api", "leave_api.token": "secret", "leave_api.refresh_minutes": "10",
            "web.public_url": "http://pc:8000", "web.password": "", "retention_days": "30", "healthcheck_url": ""}
    resp = c.post("/settings", data=form, follow_redirects=True)
    assert "Settings saved" in resp.get_data(as_text=True)
    cfg = app.cfg
    assert (cfg.schedule.late_after, cfg.schedule.work_days, cfg.camera.url, cfg.retention_days) == \
        ("09:30", [0, 1, 2, 3, 4, 5], "rtsp://cam", 30)
    assert cfg.camera.enhance_low_light is False and cfg.web.password == ""  # unticked box; empty password kept
    assert (cfg.leave_api.url, cfg.leave_api.token) == ("https://hr/api", "secret")
    c.post("/settings", data={**form, "leave_api.token": ""})
    assert cfg.leave_api.token == "secret"  # an empty token field keeps the saved token

    resp = c.post("/settings", data={**form, "schedule.late_after": "07:00"}, follow_redirects=True)
    assert "Not saved" in resp.get_data(as_text=True) and cfg.schedule.late_after == "09:30"


def test_live_page_and_preview(core):
    app, _, _ = core
    c = client(app)
    assert "Live camera" in c.get("/live").get_data(as_text=True)
    assert c.get("/live.jpg").status_code == 404
    app.live.set_frame(b"\xff\xd8jpeg")
    assert c.get("/live.jpg").data == b"\xff\xd8jpeg"
    c.post("/live/preview", data={"action": "start"})
    status = c.get("/live.json").get_json()
    assert status["previewing"] is True and app.live.wants_frame()
    c.post("/live/preview", data={"action": "stop"})
    assert c.get("/live.json").get_json()["previewing"] is False


def test_media_and_cross_site_posts_are_restricted(core):
    app, _, _ = core
    c = client(app)
    assert c.get("/media/attendance.db").status_code == 404
    assert c.get("/media/crops/../attendance.db").status_code == 404
    assert c.post("/employees", data={"id": "X", "name": "X"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert app.store.employee("X") is None


def test_report_page_and_csv_exports(core):
    app, clock, _ = core
    clock.t = at(11, 30)
    app.store.mark_notice(DAY, "camera_ok", at(9, 0))
    app.service.on_identified("A", at(10, 20), 0.8, None)
    c = client(app)
    page = c.get(f"/report?start={DAY}&end={DAY}").get_data(as_text=True)
    assert "Late &amp; absence report" in page and "Alice" in page
    summary = c.get(f"/report/summary.csv?start={DAY}&end={DAY}")
    text = summary.get_data(as_text=True)
    assert text.startswith("﻿email,name,work_days") and "alice@example.com,Alice,1,0,1,20,0,0" in text
    details = c.get(f"/report/details.csv?start={DAY}&end={DAY}").get_data(as_text=True)
    assert f"{DAY},alice@example.com,Alice,late,10:20,20,Camera," in details and f"{DAY},bob@example.com,Bob,not_seen" in details
    assert c.get("/report?start=2026-10-14&end=2026-01-01").status_code == 400


def test_leave_refresh_button(core):
    app, clock, _ = core
    c = client(app)
    resp = c.post("/leave/sync", follow_redirects=True)
    assert "Set the HR system API address" in resp.get_data(as_text=True)
    app.cfg.leave_api.url = "https://hr/api"
    app.leave_sync._fetch = lambda url, token: {"leaves": [{"email": "alice@example.com", "start": str(DAY)}]}
    page = c.post("/leave/sync", data={"back": f"/?day={DAY}"}, follow_redirects=True).get_data(as_text=True)
    assert "Leave data updated" in page and page.count('data-status="on_leave"') == 1
    assert c.post("/leave/sync", data={"back": "//evil.example"}).headers["Location"] == "/"


def test_settings_test_message_uses_the_typed_webhook(core):
    app, _, sent = core
    urls = []
    app.chat._post = lambda url, payload: urls.append(url)
    c = client(app)
    resp = c.post("/settings/test-chat", data={"google_chat.webhook_url": "https://typed.example/hook"}, follow_redirects=True)
    assert "Test message sent" in resp.get_data(as_text=True) and urls == ["https://typed.example/hook"]


def test_pages_have_day_navigation_and_report_ranges(core):
    app, clock, _ = core
    c = client(app)
    page = c.get(f"/?day={DAY}").get_data(as_text=True)
    assert "/?day=2026-10-13" in page and "/?day=2026-10-15" in page
    report = c.get("/report").get_data(as_text=True)
    assert "This month" in report and "Last month" in report and "start=2026-09-01" in report


def test_single_sightings_can_be_fixed_from_the_page(core):
    app, clock, _ = core
    c = client(app)
    clock.t = at(10, 30)
    app.service.on_identified("A", at(9, 5), 0.6, None)
    app.service.on_identified("A", at(9, 50), 0.9, None)
    page = c.get(f"/?day={DAY}").get_data(as_text=True)
    assert "09:05:00" in page and "09:50:00" in page
    first, second = app.store.sightings(DAY, "A")
    c.post(f"/sighting/{first.id}/move", data={"to": "B"})
    assert app.store.checkins(DAY)["A"].first_seen == at(9, 50)
    assert app.store.checkins(DAY)["B"].first_seen == at(9, 5)
    c.post(f"/sighting/{second.id}/reject")
    assert "A" not in app.store.checkins(DAY)
    c.post(f"/sighting/{second.id}/restore")
    assert app.store.checkins(DAY)["A"].first_seen == at(9, 50)
    resp = c.post("/sighting/999/reject", follow_redirects=True)
    assert "no longer exists" in resp.get_data(as_text=True)
    assert "move sighting" in c.get("/audit").get_data(as_text=True)


def test_detection_area_from_the_live_page(core):
    app, _, _ = core
    c = client(app)
    assert "Detection area" in c.get("/live").get_data(as_text=True)
    c.post("/live/area", data={"area": "0.200,0.100,0.900,1.000"})
    assert app.cfg.camera.area == "0.2,0.1,0.9,1"
    resp = c.post("/live/area", data={"area": "0.5,0.5,0.52,0.52"}, follow_redirects=True)
    assert "too small" in resp.get_data(as_text=True) and app.cfg.camera.area == "0.2,0.1,0.9,1"
    c.post("/live/area", data={"area": ""})
    assert app.cfg.camera.area == ""
    assert "camera.area" in c.get("/audit").get_data(as_text=True)


def test_accuracy_page(core):
    app, clock, _ = core
    c = client(app)
    clock.t = at(10, 30)
    app.service.on_identified("A", at(9, 5), 0.6, None)
    app.service.move_sighting(app.store.sightings(DAY, "A")[0].id, "B")
    page = c.get(f"/accuracy?start={DAY}&end={DAY}").get_data(as_text=True)
    assert "Recognition accuracy" in page and "Mix-ups" in page and "100.0%" in page
    assert "Alice" in page and "Bob" in page


def test_first_run_goes_to_setup_and_steps_get_ticked(tmp_path):
    from attendance.config import Config
    from attendance.pipeline import App
    from attendance.setup import open_count, steps

    cfg = Config(data_dir="data", base_dir=tmp_path)
    cfg.validate()
    app = App(cfg)
    app.chat._post = lambda url, payload: None
    c = client(app)
    resp = c.get("/")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/setup")
    page = c.get("/setup?step=done").get_data(as_text=True)
    assert "5 steps left" in page and "Camera address" in page
    assert open_count(app) == 5

    c.post("/setup/save", data={"camera.url": "rtsp://admin:X@10.0.0.5:554/Streaming/Channels/101",
                                "back": "/setup#camera"})
    c.post("/setup/save", data={"web.public_url": "http://10.0.0.9:8000"})
    c.post("/setup/save", data={"google_chat.webhook_url": "https://chat.googleapis.com/v1/spaces/X/messages?key=k"})
    c.post("/setup/skip-employees")
    assert cfg.camera.url.endswith("/101") and cfg.web.public_url == "http://10.0.0.9:8000"
    assert {s.key for s in steps(app) if s.required and not s.done} == {"password"}
    assert c.get("/").status_code == 200  # a camera is set: no more redirect

    c.post("/setup/save", data={"web.password": "secret1"})
    assert cfg.web.password == "secret1"
    assert c.get("/setup").status_code == 401  # login now required
    auth = {"Authorization": "Basic " + __import__("base64").b64encode(b"admin:secret1").decode()}
    assert "All set" in c.get("/setup?step=done", headers=auth).get_data(as_text=True)


def test_setup_can_be_put_off(tmp_path):
    from attendance.config import Config
    from attendance.pipeline import App

    cfg = Config(data_dir="data", base_dir=tmp_path)
    cfg.validate()
    c = client(App(cfg))
    c.post("/setup/later")
    assert c.get("/").status_code == 200
    resp = c.post("/setup/save", data={"camera.url": ""}, follow_redirects=True)
    assert "Nothing to save" in resp.get_data(as_text=True)


def test_camera_angle_shown_and_relearned(core):
    app, _, _ = core
    c = client(app)
    for _ in range(40):
        app.pose.add(0.0, -20.0)
    assert "20° from below" in c.get("/settings").get_data(as_text=True)
    assert "20° from below" in c.get("/live.json").get_json()["pose"]
    c.post("/settings/pose-reset")
    assert app.pose.samples == 0
    assert "relearn camera angle" in c.get("/audit").get_data(as_text=True)


def test_boss_can_be_exempt_from_late(core):
    app, clock, _ = core
    c = client(app)
    clock.t = at(10, 30)
    c.post("/employees/C", data={"name": "Carol", "active": "on", "exempt": "on"})
    assert app.store.employee("C").exempt
    app.service.on_identified("C", at(10, 20), 0.8, None)
    page = c.get(f"/?day={DAY}").get_data(as_text=True)
    assert "Arrived · not counted" in page and "Late 20 min" not in page
    c.post("/employees/C", data={"name": "Carol", "active": "on"})
    assert not app.store.employee("C").exempt


def test_camera_address_is_checked(core):
    app, _, _ = core
    c = client(app)
    unreachable = c.post("/camera/check", data={"url": "rtsp://127.0.0.1:1/stream"}).get_json()
    assert unreachable["level"] == "warn" and "Cannot reach 127.0.0.1:1" in unreachable["message"]
    assert c.post("/camera/check", data={"url": "rtsp:///nohost"}).get_json()["level"] == "error"
    assert c.post("/camera/check", data={"url": ""}).get_json()["level"] == "error"


def test_onboarding_walks_through_camera_hours_and_chat(tmp_path):
    from attendance.config import Config
    from attendance.pipeline import App

    cfg = Config(data_dir="data", base_dir=tmp_path)
    cfg.validate()
    app = App(cfg)
    c = client(app)
    assert c.get("/").headers["Location"].endswith("/setup")
    assert "Connect the camera" in c.get("/setup").get_data(as_text=True)
    c.post("/setup/save", data={"camera.url": "rtsp://cam/101", "back": "/setup?step=hours"})
    assert "Working hours" in c.get("/setup").get_data(as_text=True)
    c.post("/setup/save", data={"schedule.window_start": "07:30", "schedule.late_after": "08:30",
                                "schedule.window_end": "10:00", "schedule.days_shown": "1",
                                "schedule.work_days": ["0", "1", "2", "3", "4", "5"]})
    assert (cfg.schedule.late_after, cfg.schedule.work_days) == ("08:30", [0, 1, 2, 3, 4, 5])
    assert "Webhook URL" in c.get("/setup").get_data(as_text=True)
    c.post("/setup/skip-chat")
    assert "Almost done" in c.get("/setup").get_data(as_text=True)
