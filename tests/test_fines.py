"""The morning's fine notice: when it is sent, who is on the list, the appeal and review pages and
their hours, the day's tokens, the opening line (Claude's or built in) and the pictures."""
import datetime as dt
import json

import pytest

from attendance.core import fines, greeting
from attendance.core.greeting import Greeter
from attendance.web import create_app
from conftest import DAY, at
from test_edge import AUTH, site  # noqa: F401  (site is a fixture)
from test_evidence import seen
from test_service import run_until

APPROVERS = "https://chat.googleapis.com/v1/spaces/APPROVERS/messages?key=k&token=t"


@pytest.fixture
def office(core):
    """The office's hours: camera 08:00-10:00, late after 09:00, the notice at 09:50. Alice approves."""
    app, clock, _ = core
    s = app.cfg.schedule
    s.window_start, s.late_after, s.window_end = "08:00", "09:00", "10:00"
    fn = app.cfg.fine_notice
    fn.enabled, fn.approvers, fn.approver_webhook_url = True, "A", APPROVERS
    gc = app.cfg.google_chat  # as on the server: the space gets only this message
    gc.notify_late_arrivals = gc.late_time_list = gc.daily_summary = False
    app.cfg.validate()
    app.store.mark_notice(DAY, "camera_ok", at(8, 0))  # the camera worked this morning
    posted = []
    app.chat._post = lambda url, payload: posted.append((url, payload))
    return app, clock, posted


def group(posted):
    """What the notifications space got as a message of its own (not in the day's thread)."""
    return [p for url, p in posted if "APPROVERS" not in url and "thread" not in p]


def approvers_space(posted):
    return [p["text"] for url, p in posted if "APPROVERS" in url]


def path(app, page=fines.APPEAL, day=DAY):
    return fines.link(app.service, day, page).removeprefix("http://pc:8000")


# when the notice is sent ----------------------------------------------------------------------

def test_the_notice_is_sent_once_at_its_time_and_replaces_the_late_message(office):
    app, clock, posted = office
    seen(app, "A", at(8, 30))
    seen(app, "B", at(9, 20))  # late; Carol is not seen
    run_until(app.service, clock, at(9, 49, 30))
    assert not group(posted)  # before 09:50: nothing
    run_until(app.service, clock, at(9, 50, 30))
    assert len(group(posted)) == 1
    text = group(posted)[0]["text"]
    opening, listed, closing = text.split("\n\n")[0], text.split("\n\n")[1:-1], text.split("\n\n")[-1]
    assert opening in fines.FALLBACK_INTROS  # no line from Claude today
    assert listed == ["Late arrivals (1):\n• @Bob (09:20)", "Not seen (1):\n• @Carol"]
    assert closing.startswith("🙋 Ai thấy oan xin lên tiếng trước 10:10 · <http://pc:8000/fines/") and "review" not in closing
    run_until(app.service, clock, at(11, 0))
    assert len(group(posted)) == 1  # not again, and no late message when the camera stops
    assert app.store.notice_sent(DAY, fines.NOTICE) and not app.store.notice_sent(DAY, "late_message")
    assert any(action == "send fine notice" and details == "Bob, Carol" for _, _, action, details in app.store.audit())


def test_a_server_that_was_down_at_the_minute_still_sends_it_while_appeals_are_open(office):
    app, clock, posted = office
    seen(app, "B", at(9, 20))
    clock.t = at(10, 5)
    app.service.tick()
    assert len(group(posted)) == 1
    app.store._exec("DELETE FROM notices WHERE kind = ?", (fines.NOTICE,))
    clock.t = at(10, 11)  # the appeal page has closed: a notice now would link to nothing
    app.service.tick()
    assert len(group(posted)) == 1


def test_nobody_fined_means_no_notice(office):
    app, clock, posted = office
    for who in "ABC":
        seen(app, who, at(8, 40))
    run_until(app.service, clock, at(10, 30))
    assert not group(posted) and app.store.notice_sent(DAY, fines.SKIPPED)


def test_switched_off_the_late_message_is_sent_as_before(office):
    app, clock, posted = office
    app.cfg.fine_notice.enabled = False
    seen(app, "B", at(9, 20))
    run_until(app.service, clock, at(10, 30))
    assert [p["text"].split("\n")[0] for p in group(posted)] == ["Morning, coming late today:"]
    assert not app.store.notice_sent(DAY, fines.NOTICE)


def test_the_times_must_be_in_order(office):
    app, _, _ = office
    app.cfg.fine_notice.appeal_until = "09:55"  # before the list closes
    with pytest.raises(ValueError, match="in order"):
        app.cfg.validate()


# the picture ----------------------------------------------------------------------------------

def test_the_picture_shows_the_late_only(office, monkeypatch):
    from attendance.core import latephoto

    app, clock, posted = office
    seen(app, "B", at(9, 20))
    monkeypatch.setattr(app.service, "best_reference_photo", lambda emp: app.data_dir / "ref.jpg")  # Carol has one
    drawn = []
    real = latephoto.write
    monkeypatch.setattr(latephoto, "write", lambda data_dir, day, people, *a: (drawn.append(people), real(data_dir, day, people, *a))[1])
    clock.t = at(9, 50)
    app.service.tick()
    assert drawn and all([p.name for p in people] == ["Bob"] and not people[0].not_seen for people in drawn)
    assert "/late-photo/" in group(posted)[0]["text"]  # http address: the picture is linked


# the pages and their hours --------------------------------------------------------------------

@pytest.mark.parametrize("when,appeal,review", [
    (at(9, 49, 59), 404, 404),  # before the notice
    (at(9, 50), 200, 200),
    (at(10, 0, 30), 200, 200),
    (at(10, 10, 59), 200, 200),  # the appeal minute itself is still open
    (at(10, 11), 404, 200),
    (at(10, 14, 59), 404, 200),
    (at(10, 15), 404, 404),
])
def test_each_page_opens_only_during_its_hours_and_without_a_login(office, when, appeal, review):
    app, clock, _ = office
    app.cfg.web.password = "a-long-admin-password"
    seen(app, "B", at(9, 20))
    c = create_app(app).test_client()
    clock.t = when
    assert c.get(path(app)).status_code == appeal
    assert c.get(path(app, fines.REVIEW)).status_code == review
    assert c.post(path(app), data={"employee_id": "B", "message": "tắc đường"}).status_code == (302 if appeal == 200 else 404)
    assert c.post(path(app, fines.REVIEW), data={"accept": "B", "approver": "A"}).status_code == (302 if review == 200 else 404)
    assert c.get("/").status_code == 401  # the rest of the site still asks for the login


def test_a_link_is_for_one_day_and_one_page(office):
    app, clock, _ = office
    seen(app, "B", at(9, 20))
    c = create_app(app).test_client()
    clock.t = at(9, 55)
    today, yesterday = path(app), path(app, day=DAY - dt.timedelta(days=1))
    assert today != yesterday and len(today.rpartition("/")[2]) == 40
    assert c.get(today).status_code == 200 and c.get(yesterday).status_code == 404
    assert c.get("/fines/" + "0" * 40).status_code == 404
    # The appeal token does not open the review page, nor the other way round.
    assert c.get("/fines/review/" + today.rpartition("/")[2]).status_code == 404
    assert c.get("/fines/" + path(app, fines.REVIEW).rpartition("/")[2]).status_code == 404
    clock.t = at(9, 55, day=DAY + dt.timedelta(days=1))  # the next morning: yesterday's link is dead
    assert c.get(today).status_code == 404
    app.cfg.fine_notice.enabled = False
    assert c.get(path(app, day=DAY + dt.timedelta(days=1))).status_code == 404


def test_the_list_follows_the_camera_until_it_closes(office):
    app, clock, _ = office
    seen(app, "B", at(9, 20))
    clock.t = at(9, 50)
    assert [(f.name, f.kind, f.time, bool(f.photo)) for f in fines.fined(app.service, DAY)] == [
        ("Bob", "late", "09:20", True), ("Alice", "not_seen", "", False), ("Carol", "not_seen", "", False)]
    seen(app, "C", at(9, 55))  # seen before the list closes: late now, with the camera's photo
    clock.t = at(9, 56)
    assert [(f.name, f.kind, f.time, bool(f.photo)) for f in fines.fined(app.service, DAY)] == [
        ("Bob", "late", "09:20", True), ("Carol", "late", "09:55", True), ("Alice", "not_seen", "", False)]
    seen(app, "A", at(10, 0, 40))  # the 10:00 minute still counts
    assert [f.kind for f in fines.fined(app.service, DAY)] == ["late", "late", "late"]
    app.store.delete_checkin(DAY, "A")
    app.service.set_arrival(DAY, "A", dt.time(10, 5))  # after the list closed: stays as not seen
    clock.t = at(10, 6)
    assert [(f.name, f.kind, bool(f.photo)) for f in fines.fined(app.service, DAY)][-1] == ("Alice", "not_seen", False)


def test_the_public_page_never_shows_a_photo_of_someone_not_seen(office, monkeypatch):
    app, clock, _ = office
    seen(app, "B", at(9, 20))
    reference = app.data_dir / "faces" / "C" / "c.jpg"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"\xff\xd8reference")
    monkeypatch.setattr(app.service, "best_reference_photo", lambda emp: reference)
    c = create_app(app).test_client()
    clock.t = at(9, 55)
    page = c.get(path(app))
    html = page.get_data(as_text=True)
    assert page.headers["Cache-Control"] == "no-store" and "noindex" in page.headers["X-Robots-Tag"]
    assert html.count("<img") == 1 and f"{path(app)}/photo/B.jpg" in html
    assert "Carol" in html and "Alice" in html and "photo/C.jpg" not in html
    photo = c.get(f"{path(app)}/photo/B.jpg")
    assert photo.status_code == 200 and photo.data[:2] == b"\xff\xd8" and photo.data != reference.read_bytes()
    assert c.get(f"{path(app)}/photo/C.jpg").status_code == 404
    assert c.get(f"{path(app, fines.REVIEW)}/photo/C.jpg").status_code == 404
    clock.t = at(10, 11)
    assert c.get(f"{path(app)}/photo/B.jpg").status_code == 404  # the photo closes with the page


# appeals and decisions ------------------------------------------------------------------------

def test_an_appeal_is_recorded_and_the_approvers_get_their_link(office):
    app, clock, posted = office
    app.store.update_employee("A", "Alice", True, "999")
    seen(app, "B", at(9, 20))
    c = create_app(app).test_client()
    clock.t = at(9, 55)
    assert c.post(path(app), data={"employee_id": "B", "message": "  thang máy   hỏng "}).status_code == 302
    appeal = app.store.appeal(DAY, "B")
    assert (appeal["message"], appeal["kind"], appeal["status"]) == ("thang máy hỏng", "late", "pending")
    told = approvers_space(posted)
    assert len(told) == 1 and "*Bob* (đến lúc 09:20)" in told[0] and "“thang máy hỏng”" in told[0]
    assert f"Xử trước 10:14: <{fines.link(app.service, DAY, fines.REVIEW)}|" in told[0] and "<users/999>" in told[0]
    assert not group(posted)  # the review link never goes to the notifications space
    assert ("appeal page", "appeal", "Bob: thang máy hỏng") in [(u, a, d) for _, u, a, d in app.store.audit()]
    assert "đang kêu oan" in c.get(path(app)).get_data(as_text=True)

    for data in ({"employee_id": "A", "message": "x"},  # Alice... is not seen, so she may appeal too
                 {"employee_id": "Z", "message": "x"}, {"employee_id": "B", "message": " "},
                 {"employee_id": "B", "message": "x" * (fines.MAX_MESSAGE + 1)}):
        c.post(path(app), data=data)
    assert sorted(a["employee_id"] for a in app.store.appeals(DAY)) == ["A", "B"]
    assert app.store.appeal(DAY, "B")["message"] == "thang máy hỏng"


def test_someone_on_time_cannot_appeal_and_an_appeal_can_be_rewritten_until_decided(office):
    app, clock, posted = office
    seen(app, "A", at(8, 30))
    seen(app, "B", at(9, 20))
    clock.t = at(9, 55)
    assert fines.submit(app.service, DAY, "A", "tôi đúng giờ mà") == "Tên này không có trong danh sách hôm nay."
    assert fines.submit(app.service, DAY, "B", "lần một") is None
    assert fines.submit(app.service, DAY, "B", "lần hai") is None
    assert app.store.appeal(DAY, "B")["message"] == "lần hai" and "Lời kêu oan được sửa" in approvers_space(posted)[-1]
    assert fines.decide(app.service, DAY, "B", False, "A") is None
    assert "đã được xử" in fines.submit(app.service, DAY, "B", "lần ba")
    assert app.store.appeal(DAY, "B")["message"] == "lần hai"


def test_an_accepted_appeal_counts_as_on_time_and_every_decision_is_logged(office):
    app, clock, _ = office
    seen(app, "B", at(9, 20))
    c = create_app(app).test_client()
    clock.t = at(9, 55)
    fines.submit(app.service, DAY, "B", "tắc đường")
    fines.submit(app.service, DAY, "C", "đi gặp khách")
    review = path(app, fines.REVIEW)
    html = c.get(review).get_data(as_text=True)
    assert "tắc đường" in html and "đi gặp khách" in html and html.count("<img") == 1  # Bob's photo; none of Carol
    assert c.get(f"{review}/photo/B.jpg").status_code == 200

    c.post(review, data={"accept": "B", "approver": "B"})  # Bob is no approver
    c.post(review, data={"accept": "B"})
    assert app.store.appeal(DAY, "B")["status"] == "pending"
    clock.t = at(10, 12)
    c.post(review, data={"accept": "B", "approver": "A", "note:B": "ok lần này"})
    c.post(review, data={"reject": "C", "approver": "A"})
    assert [(a["employee_id"], a["status"], a["decided_by"], a["note"]) for a in app.store.appeals(DAY)] == [
        ("B", "accepted", "Alice", "ok lần này"), ("C", "rejected", "Alice", "")]
    rep = app.service.report(DAY)
    assert [(a.name, a.time, a.appealed) for a in rep.on_time] == [("Bob", "09:20", True)] and not rep.late
    assert ("C", "Carol") in rep.not_seen
    assert [f.name for f in fines.fined(app.service, DAY)] == ["Alice", "Carol"]
    assert c.get(f"{review}/photo/B.jpg").status_code == 200  # the approver still sees who they accepted
    log = [(u, a, d) for _, u, a, d in app.store.audit()]
    assert ("Alice", "appeal accepted", "Bob: tắc đường | ok lần này") in log
    assert ("Alice", "appeal rejected", "Carol: đi gặp khách") in log

    c.post(review, data={"accept": "C", "approver": "A"})  # changed their mind while the page is open
    rep = app.service.report(DAY)
    carol = next(a for a in rep.on_time if a.employee_id == "C")
    assert (carol.source, carol.time, carol.appealed) == ("appeal", "09:00", True) and ("C", "Carol") not in rep.not_seen
    from attendance.core.service import minutes_late
    assert all(minutes_late(a, rep.window) == 0 for a in rep.on_time)
    assert app.service.report(DAY, appeals=False).late[0].name == "Bob"


def test_fines_go_to_the_camera_api_only_after_the_review_closes(office, monkeypatch):
    from attendance.integrations import camera_api

    app, clock, _ = office
    app.cfg.camera_api.url, app.cfg.camera_api.key = "https://script.google.com/macros/s/test/exec", "camera-test-secret"
    app.cfg.camera_api.send_at, app.cfg.camera_api.days_back, app.cfg.camera_api.include_not_seen = "10:00", 0, False
    calls = []
    monkeypatch.setattr(camera_api, "post", lambda url, payload: calls.append(payload) or {
        "ok": True, "results": [{"email": e["email"], "date": e["date"], "status": "created"} for e in payload["events"]]})
    seen(app, "B", at(9, 20))
    seen(app, "C", at(9, 30))
    clock.t = at(9, 55)
    fines.submit(app.service, DAY, "B", "tắc đường")
    clock.t = at(10, 0, 10)
    app.camera_sync.sync(auto=True)  # its usual minute: put off until the review has closed
    assert not calls and not app.store.notice_sent(DAY, "camera_api_daily")
    assert "Waiting until appeals" in app.camera_sync.sync()["message"] and not calls  # "Send now" waits too
    fines.decide(app.service, DAY, "B", True, "A")
    clock.t = at(10, 15, 10)
    app.camera_sync.sync(auto=True)
    assert [e["email"] for call in calls for e in call["events"]] == ["carol@example.com"]  # Bob's fine was lifted


# the opening line -----------------------------------------------------------------------------

def test_the_opening_line_is_one_of_claudes_for_today_and_not_one_used_lately(office):
    app, _, _ = office
    fines.keep_intros(app.store, DAY, ["Câu một.", "Câu hai.", "Câu ba."])
    app.store.put(fines.USED_KEY, json.dumps([[(DAY - dt.timedelta(days=1)).isoformat(), "Câu một."],
                                               [(DAY - dt.timedelta(days=2)).isoformat(), "Câu ba."],
                                               [(DAY - dt.timedelta(days=30)).isoformat(), "Câu hai."]]))
    offered = []
    text, source = fines.intro(app.store, DAY, choose=lambda lines: offered.append(lines) or lines[0])
    assert (text, source, offered) == ("Câu hai.", "claude", [["Câu hai."]])
    assert fines.intro(app.store, DAY, choose=lambda lines: 1 / 0) == ("Câu hai.", "claude")  # settled for the day
    used = json.loads(app.store.get(fines.USED_KEY))
    assert [DAY.isoformat(), "Câu hai."] in used and len(used) == 3  # the month-old one is forgotten


def test_without_claude_a_built_in_line_is_used_and_the_notice_is_still_sent(office):
    app, clock, posted = office
    fines.keep_intros(app.store, DAY - dt.timedelta(days=1), ["Câu của hôm qua."])  # yesterday's lines are not today's
    assert fines.intro(app.store, DAY)[1] == "fallback" and fines.intro(app.store, DAY)[0] in fines.FALLBACK_INTROS
    seen(app, "B", at(9, 20))
    clock.t = at(9, 50)
    app.service.tick()
    first = fines.intro(app.store, DAY)[0]
    assert group(posted)[0]["text"].split("\n\n")[0] == first
    following = [fines.intro(app.store, DAY + dt.timedelta(days=n))[0] for n in range(1, len(fines.FALLBACK_INTROS))]
    assert len({first, *following}) == len(fines.FALLBACK_INTROS)  # no repeat within the week


def test_a_morning_without_the_camera_fines_nobody_for_not_being_seen(office):
    app, clock, posted = office
    app.store._exec("DELETE FROM notices WHERE kind = 'camera_ok'")
    clock.t = at(9, 50)
    assert app.service.report(DAY).no_camera_data and fines.fined(app.service, DAY) == []
    app.service.tick(False, "no picture")
    assert not group(posted) and app.store.notice_sent(DAY, fines.SKIPPED)


def test_the_notice_opens_with_claudes_line_when_the_mac_sent_some(office):
    app, clock, posted = office
    fines.keep_intros(app.store, DAY, ["Đầu tuần, hệ thống phạt nguội xin kính chào quý vị 🚦"])
    seen(app, "B", at(9, 20))
    clock.t = at(9, 50)
    app.service.tick()
    assert group(posted)[0]["text"].startswith("Đầu tuần, hệ thống phạt nguội xin kính chào quý vị 🚦\n\nLate arrivals (1):")


@pytest.mark.parametrize("line,ok", [
    ("Thông báo phạt nguội đầu tuần 🚨", True),
    ("Mừng ngày 20/11, camera vẫn làm việc.", True),
    ("Xem tại https://example.com nhé", False), ("Vào www.example.com", False),
    ("Hạn chót 10:10 nhé", False), ("Trước 10h mới tính", False), ("Trước 10 giờ nhé", False),
    ("Kính mời <users/123> lên phường", False), ("Gọi @Bob lên phường", False),
    ("Hai emoji 🚨🚓 là quá nhiều", False), ("", False), ("x" * 301, False), (None, False),
    ("dòng một\ndòng hai", True),
])
def test_only_a_plain_line_is_posted(line, ok):
    cleaned = fines.clean_intro(line)
    assert (cleaned is not None) is ok
    assert cleaned is None or "\n" not in cleaned
    assert all(fines.clean_intro(t) == t for t in fines.FALLBACK_INTROS)


def greeter(core, answer):
    app, clock, _ = core
    app.cfg.greeting.enabled, app.cfg.greeting.key = True, "k"
    asked = []

    def write(people, day):
        asked.append(len(people))
        return {**{p.id: {"on_time": ["Chào!"], "late": ["Đến rồi!"]} for p in people}, **answer}

    return app, clock, Greeter(app.cfg, app.store, clock, lambda d: app.service.window(d).late_from,
                               speak=lambda *a, **k: None, write=write, sleep=lambda s: None), asked


def test_claude_writes_the_lines_in_the_greetings_request_and_the_mac_reports_them(core):
    app, clock, g, asked = greeter(core, {greeting.FINE_INTROS: ["Câu A.", "Xem https://x.vn", "Câu B."]})
    clock.t = at(7, 0)
    assert g.write_if_due() and asked == [3]  # one request: the greetings and the lines together
    assert g.fine_intros(DAY) == ["Câu A.", "Câu B."]
    assert g.status()[greeting.FINE_INTROS] == {"day": DAY.isoformat(), "lines": ["Câu A.", "Câu B."]}
    assert "fine_intros" not in json.loads(app.store.get(greeting.LINES_KEY))["people"]  # no greeting for a person of that name
    assert not g.write_if_due() and asked == [3]  # nothing left to write: no request for the lines alone

    people = [p for p in app.store.all_employees() if p.active]
    system, user, _ = g.prompt(people, DAY + dt.timedelta(days=1), intros=True)
    assert "fine_intros" in system and 'recent_fine_intros:\n["Câu A.", "Câu B."]' in user
    system, user, _ = g.prompt(people, DAY + dt.timedelta(days=1))
    assert "fine_intros" not in system and "recent_fine_intros" not in user


def test_the_lines_reach_the_server_with_the_heartbeat(core):
    from attendance.web.edge_api import _greeting_status

    app, clock, g, _ = greeter(core, {greeting.FINE_INTROS: ["Câu A."]})
    clock.t = at(7, 0)
    g.write_if_due()
    beat = json.loads(json.dumps({"greeting": g.status()}))  # as it travels
    assert _greeting_status(beat["greeting"])["enabled"]  # the server still reads the rest of the status
    lines = beat["greeting"]["fine_intros"]
    fines.keep_intros(app.store, dt.date.fromisoformat(lines["day"]), lines["lines"])
    assert fines.intro(app.store, DAY) == ("Câu A.", "claude")


def test_settings_choose_the_approvers_and_the_times(office):
    app, clock, _ = office
    c = create_app(app).test_client()
    page = c.get("/settings").get_data(as_text=True)
    assert 'name="fine_notice.approvers" value="A" checked' in page and 'name="fine_notice.send_at" value="09:50"' in page
    from attendance.core import settings

    settings.save(app.cfg, app.store, {"fine_notice": {"approvers": "B, C,B,", "appeal_until": "10:12"}})
    assert app.cfg.fine_notice.approvers == "B,C" and fines.approvers(app.service) == {"B": "Bob", "C": "Carol"}
    assert fines.times(app.service, DAY).appeal_close == at(10, 13)
    with pytest.raises(ValueError):
        settings.save(app.cfg, app.store, {"fine_notice": {"review_until": "10:00"}})


def test_the_server_keeps_todays_lines_from_the_macs_heartbeat(site):
    _, core, c = site
    today = core.service.now().date()
    beat = {"camera_ok": True, "greeting": {"enabled": True, "fine_intros": {
        "day": today.isoformat(), "lines": ["Câu A.", "Xem https://x.vn", 7]}}}
    assert c.post("/api/edge/heartbeat", json=beat, headers=AUTH).status_code == 200
    assert json.loads(core.store.get(fines.INTROS_KEY)) == {"day": today.isoformat(), "lines": ["Câu A."]}
    beat["greeting"]["fine_intros"] = {"day": (today - dt.timedelta(days=1)).isoformat(), "lines": ["Câu cũ."]}
    c.post("/api/edge/heartbeat", json=beat, headers=AUTH)  # an old Mac clock or a late beat: not today's
    c.post("/api/edge/heartbeat", json={"camera_ok": True, "greeting": "x"}, headers=AUTH)  # an older Mac sends none
    assert json.loads(core.store.get(fines.INTROS_KEY))["lines"] == ["Câu A."]
