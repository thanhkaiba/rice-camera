import datetime as dt

import numpy as np
import pytest

from attendance.gallery import Gallery
from attendance.service import AttendanceService
from conftest import DAY, at, person_vector


def run_until(service, clock, end, step=dt.timedelta(seconds=30), camera_ok=True):
    while clock.t < end:
        clock.t += step
        service.tick(camera_ok)


def test_on_time_seen_again_after_deadline_is_not_late(env):
    service, _, clock, sent = env
    run_until(service, clock, at(9, 20))
    service.on_identified("A", at(9, 15), 0.7, None)
    run_until(service, clock, at(10, 30))
    service.on_identified("A", at(10, 25), 0.7, None)  # A walks past again

    rep = service.report(DAY)
    assert [a.employee_id for a in rep.on_time] == ["A"]
    assert rep.on_time[0].time == "09:15"
    assert not any("Alice" in m and "late" in m for m in sent)


def test_not_seen_by_deadline_is_listed_then_late_arrival_announced(env):
    service, _, clock, sent = env
    run_until(service, clock, at(9, 30))
    service.on_identified("A", at(9, 25), 0.7, None)
    run_until(service, clock, at(10, 1))

    deadline = [m for m in sent if m.startswith("⏰")]
    assert len(deadline) == 1
    assert "2/3 people not seen yet" in deadline[0]
    assert "• @Bob" in deadline[0] and "• @Carol" in deadline[0] and "Alice" not in deadline[0]

    clock.t = at(10, 23)
    service.on_identified("B", at(10, 23), 0.7, None)
    assert sent[-1] == "🟠 @Bob arrived at 10:23, 23 min late."

    run_until(service, clock, at(11, 1))
    summary = sent[-1]
    assert summary.startswith("📋")
    assert "On time: 1 · Late: 1 · Not seen: 1" in summary
    assert "• @Bob: 10:23 (23 min late)" in summary
    assert "• @Carol" in summary


def test_notices_are_sent_once_even_after_restart(env, tmp_path):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 5))
    restarted = AttendanceService(service.cfg, store, service.chat, Gallery(), now=clock)
    run_until(restarted, clock, at(10, 10))
    assert len([m for m in sent if m.startswith("⏰")]) == 1


def test_unknown_faces_wait_on_the_review_page_and_admin_can_resolve(env):
    service, store, clock, sent = env
    run_until(service, clock, at(9, 10))
    carol_turned = person_vector(3) + 0.5 * person_vector(40)  # Carol, but not like her reference photo
    uid = service.on_unknown(at(9, 12), "x.jpg", carol_turned / np.linalg.norm(carol_turned), "C", 0.38)
    service.on_unknown(at(9, 13), "y.jpg", person_vector(20), None, None)
    run_until(service, clock, at(9, 20))
    assert not [m for m in sent if m.startswith("❓")]  # Review is on the page; Chat is not told
    assert store.pending_group_count(DAY) == 2

    run_until(service, clock, at(10, 5))
    deadline = [m for m in sent if m.startswith("⏰")][0]
    assert "Carol" in deadline and "unidentified" not in deadline and "/unknowns" not in deadline

    # Admin confirms the 9:12 face was C: on time, quietly.
    posted = len(sent)
    service.resolve_unknown(uid, "C")
    assert len(sent) == posted
    rep = service.report(DAY)
    assert "C" in [a.employee_id for a in rep.on_time]
    assert service.gallery.counts()["C"] == 2  # the confirmed face was learned
    assert [r.source for r in store.references(service.model_id, "C")] == ["upload", "admin"]
    assert store.embeddings("some-other-model") == []
    with pytest.raises(ValueError):
        service.resolve_unknown(uid, "C")


def test_moving_a_sighting_to_the_right_person_learns_their_face(env):
    service, store, clock, sent = env
    run_until(service, clock, at(9, 30))
    carol_as_bob = person_vector(3) + 0.5 * person_vector(40)  # Carol's face, which the camera took for Bob
    service.on_identified("B", at(9, 12), 0.5, "crops/x.jpg", carol_as_bob / np.linalg.norm(carol_as_bob))
    s = store.sightings(DAY)[0]
    assert s.model == service.model_id and s.vector is not None
    assert service.move_sighting(s.id, "C") is True
    assert set(store.checkins(DAY)) == {"C"}
    assert service.gallery.counts()["C"] == 2 and service.gallery.counts()["B"] == 1
    assert [r.source for r in store.references(service.model_id, "C")] == ["upload", "admin"]
    assert "face learned" in store.audit()[0][3]

    # The same face moved back learns nothing more for Bob when the setting is off; a sighting
    # without an embedding (an arrival typed in) never learns.
    service.cfg.recognition.learn_corrections = False
    assert service.move_sighting(s.id, "B") is False
    assert service.gallery.counts()["B"] == 1
    service.cfg.recognition.learn_corrections = True
    service.set_arrival(DAY, "A", dt.time(9, 0))
    manual = next(x for x in store.sightings(DAY) if x.employee_id == "A")
    assert service.move_sighting(manual.id, "C") is False


def test_unclear_face_like_someone_checked_in_is_reviewed_unless_setting_is_on(env):
    service, store, clock, sent = env
    service.on_identified("B", at(9, 0), 0.7, None)
    like_bob = person_vector(2) + 0.6 * person_vector(20)  # Bob, unclear
    like_bob /= np.linalg.norm(like_bob)
    assert service.on_unknown(at(9, 5), "a.jpg", like_bob, "B", 0.35) is None  # taken to be Bob again
    service.cfg.recognition.seen_again_score = 0
    assert service.on_unknown(at(9, 6), "b.jpg", like_bob, "B", 0.35) is not None


def test_no_late_time_message_when_everyone_is_in(env):
    service, store, clock, sent = env
    store.update_employee("A", "Alice", True, exempt=True)  # the boss: not expected by the late time
    run_until(service, clock, at(9, 30))
    service.on_identified("B", at(9, 10), 0.7, None)
    service.on_identified("C", at(9, 12), 0.7, None)
    run_until(service, clock, at(10, 5))
    rep = service.report(DAY)
    assert rep.all_in and rep.last_arrival.time == "09:12"
    assert not [m for m in sent if m.startswith("⏰")]  # everyone expected was in: no 10:00 list
    assert store.notice_sent(DAY, "deadline_skipped") and not store.notice_sent(DAY, "deadline")

    service.on_identified("A", at(10, 20), 0.7, None)  # the boss comes in later: recorded, not announced
    assert not [m for m in sent if "Alice" in m]
    run_until(service, clock, at(11, 5))
    summary = sent[-1]
    assert summary.startswith("📋") and "On time: 2 · Late: 0 · Not seen: 0" in summary
    assert "✅ Everyone was in by 09:12" in summary


def test_late_time_message_still_sent_when_someone_is_missing_or_faces_wait(env):
    service, store, clock, sent = env
    run_until(service, clock, at(9, 30))
    for emp in "ABC":
        service.on_identified(emp, at(9, 10), 0.7, None)
    service.on_unknown(at(9, 20), "x.jpg", person_vector(20), None, None)  # a visitor, or someone mis-enrolled
    run_until(service, clock, at(10, 5))
    deadline = [m for m in sent if m.startswith("⏰")]
    assert len(deadline) == 1 and "all 3 people arrived on time" in deadline[0] and "unidentified" not in deadline[0]


def test_admin_correction_moves_late_arrival_earlier(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 30))
    service.on_identified("B", at(10, 20), 0.7, None)
    uid = service.on_unknown(at(9, 40), "b.jpg", None, "B", 0.2)  # too unlike B to be suppressed
    service.resolve_unknown(uid, "B")
    assert store.checkins(DAY)["B"].first_seen == at(9, 40)
    assert "B" in [a.employee_id for a in service.report(DAY).on_time]


def test_no_notices_on_weekend(env):
    service, _, clock, sent = env
    saturday = dt.date(2026, 10, 17)
    clock.t = at(8, 0, day=saturday)
    run_until(service, clock, at(12, 0, day=saturday), step=dt.timedelta(minutes=5))
    assert sent == []


def test_camera_outage_is_reported(env):
    service, _, clock, sent = env
    run_until(service, clock, at(9, 30))
    run_until(service, clock, at(9, 50), camera_ok=False)
    run_until(service, clock, at(10, 1))
    deadline = [m for m in sent if m.startswith("⏰")][0]
    assert "was down: 09:30–09:50" in deadline


def test_system_started_late_reports_the_gap(env):
    service, _, clock, sent = env
    clock.t = at(9, 40)  # PC switched on at 9:40
    run_until(service, clock, at(10, 1))
    assert "was down: 08:00–09:40" in [m for m in sent if m.startswith("⏰")][0]


def test_pc_off_all_morning_sends_only_summary_with_warning(env):
    service, _, clock, sent = env
    clock.t = at(14, 0)
    service.tick()
    assert len(sent) == 1 and sent[0].startswith("📋")
    assert "was down: 08:00–11:00" in sent[0]


def test_messages_use_one_thread_per_day(env):
    service, store, clock, _ = env
    payloads = []
    service.chat._post = lambda url, payload: payloads.append((url, payload))
    run_until(service, clock, at(10, 1))
    url, payload = payloads[0]
    assert payload["thread"] == {"threadKey": "attendance-2026-10-14"}
    assert url.endswith("&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")


def test_camera_down_in_camera_hours_is_reported_once_and_recovery_too(env):
    service, store, clock, sent = env
    run_until(service, clock, at(8, 30))
    clock.t, step = at(8, 30), dt.timedelta(seconds=10)
    for _ in range(6):  # one minute down: not reported yet
        clock.t += step
        service.tick(False, "no picture (reconnecting)")
    assert not [m for m in sent if "camera is not working" in m]
    for _ in range(30):
        clock.t += step
        service.tick(False, "no picture (reconnecting)")
    down = [m for m in sent if "camera is not working" in m]
    assert len(down) == 1 and "since 08:30" in down[0] and "no picture (reconnecting)" in down[0]
    run_until(service, clock, at(9, 0))
    assert len([m for m in sent if "working again" in m]) == 1
    run_until(service, clock, at(9, 30))
    assert len([m for m in sent if "camera is not working" in m]) == 1


def test_camera_alert_waits_after_a_restart(env):
    service, store, clock, sent = env
    run_until(service, clock, at(8, 30))
    clock.t = at(9, 0)  # the program was restarted: half an hour without news, camera still connecting
    service._bad_since = None
    service.tick(False, "connecting")
    assert not [m for m in sent if "camera is not working" in m]


def test_exempt_people_are_recorded_but_never_late_or_missing(env):
    service, store, clock, sent = env
    store.update_employee("C", "Carol", True, exempt=True)
    run_until(service, clock, at(10, 30))
    service.on_identified("C", at(10, 20), 0.8, None)  # the boss comes in at 10:20
    rep = service.report(DAY)
    assert [(i, a.time) for i, _, a in rep.exempt] == [("C", "10:20")]
    assert "C" not in [a.employee_id for a in rep.late] and "C" not in [i for i, _ in rep.not_seen]
    assert rep.total == 2
    assert not any("Carol" in m for m in sent)
    assert "Carol" not in (service.late_text(DAY) or "")
    assert "C" not in [p.employee_id for p in service.period_report(DAY, DAY).people]


def test_confirming_faces_of_a_late_person_posts_nothing(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 40))
    first = service.on_unknown(at(10, 30), "b1.jpg", person_vector(21), None, None)  # unlike each other:
    second = service.on_unknown(at(10, 29), "b2.jpg", person_vector(22), None, None)  # two separate groups
    service.resolve_unknown(first, "B")
    service.resolve_unknown(second, "B")  # an earlier visit, still late
    assert store.checkins(DAY)["B"].first_seen == at(10, 29)
    early = service.on_unknown(at(9, 50), "b3.jpg", person_vector(23), None, None)
    service.resolve_unknown(early, "B")  # turns out B was on time after all
    assert store.checkins(DAY)["B"].first_seen == at(9, 50)
    assert not [m for m in sent if "Bob" in m and not m.startswith("⏰")]  # only the late-time list named him


def test_unclear_face_also_like_someone_not_in_yet_is_reviewed(env):
    """A mask or a side view makes a face only somewhat like several people: once one of them is in,
    it must not be dropped as "them again" while it may be the arrival of someone not in yet."""
    service, store, clock, sent = env
    service.on_identified("B", at(9, 0), 0.7, None)
    both = person_vector(2) + person_vector(3)  # as like Bob (in) as Carol (not in yet)
    both /= np.linalg.norm(both)
    (bob, score), = [m for m in service.gallery.match(both, k=3) if m[0] == "B"]
    assert service.on_unknown(at(9, 5), "m.jpg", both, "B", score) is not None
    mostly_bob = person_vector(2) + 0.2 * person_vector(3)
    mostly_bob /= np.linalg.norm(mostly_bob)
    (_, score), = [m for m in service.gallery.match(mostly_bob, k=3) if m[0] == "B"]
    assert service.on_unknown(at(9, 6), "b.jpg", mostly_bob, "B", score) is None  # clearly Bob again


def test_seen_again_margin_uses_scores_from_the_same_picture(env):
    service, store, clock, sent = env
    service.on_identified("B", at(9, 0), 0.7, None)
    both = person_vector(2) + person_vector(3)  # this picture is as like Carol (not in) as Bob
    both /= np.linalg.norm(both)
    # Another frame of the track matched Bob at 0.95: still not "Bob again", as this picture shows.
    assert service.on_unknown(at(9, 5), "m.jpg", both, "B", 0.95) is not None
