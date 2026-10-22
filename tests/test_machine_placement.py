"""Decoder threads, the page address check, power/throttling alerts and the camera placement hints."""
import datetime as dt
from types import SimpleNamespace

from attendance.core import machine, placement
from attendance.core.config import Config
from attendance.core.machine import MachineWatch, Power
from attendance.core.placement import Placement
from attendance.vision import camera
from conftest import DAY

BATT_AC_5W = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=6160483)\t6%; AC attached; not charging present: true
"""
BATT_CHARGING = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=6160483)\t30%; charging; 1:09 remaining present: true
"""
BATT_BATTERY = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=6160483)\t15%; discharging; 0:40 remaining present: true
"""
THERM = """Note: No thermal warning level has been recorded
2026-10-19 17:37:33 +0700 CPU Power notify
\tCPU_Scheduler_Limit \t= 40
\tCPU_Available_CPUs \t= 12
\tCPU_Speed_Limit \t= 24
"""
ADAPTER = """ Wattage = 60W
 Current = 3000mA
 Voltage = 20000mV
 Adapter Name = 61W USB-C Power Adapter
"""


# --- decoder threads ------------------------------------------------------------------------

def test_capture_options_set_and_replace_threads():
    assert camera.capture_options("rtsp_transport;tcp", 2) == "rtsp_transport;tcp|threads;2"
    assert camera.capture_options("rtsp_transport;tcp|threads;8", 2) == "rtsp_transport;tcp|threads;2"
    assert camera.capture_options("rtsp_transport;tcp|threads;8", 0) == "rtsp_transport;tcp"
    assert camera.capture_options("", 3) == "threads;3"


def test_open_capture_sets_options_only_while_opening(monkeypatch):
    seen = []
    monkeypatch.setattr(camera.cv2, "VideoCapture", lambda *a: seen.append((a, camera.os.environ[
        "OPENCV_FFMPEG_CAPTURE_OPTIONS"])) or "cap")
    assert camera.open_capture("rtsp://x", 2) == "cap"
    assert seen[-1][1].endswith("threads;2")
    assert camera.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == camera._BASE_OPTIONS
    camera.open_capture(0, 2)  # a webcam index is opened as it is
    assert seen[-1][0] == (0,)


def test_decode_threads_setting_validated():
    cfg = Config()
    cfg.camera.decode_threads = 40
    try:
        cfg.validate()
    except ValueError as exc:
        assert "decode_threads" in str(exc)
    else:
        raise AssertionError("40 decoder threads accepted")


def test_received_fps():
    cam = camera.Camera("rtsp://x")
    now = camera.time.time()
    cam._arrivals.extend(now - 2 + i * 0.1 for i in range(21))  # 10 a second for the last 2 s
    assert abs(cam.received_fps() - 10) < 0.5


# --- page address -----------------------------------------------------------------------------

FOUND = {"ips": ["127.0.0.1", "192.168.10.243"], "lan": "192.168.10.243",
         "names": ["face-detection", "face-detection.local"], "local_name": "face-detection"}


def test_public_url_wrong_ip_suggests_current_and_local_name():
    cfg = Config()
    cfg.web.public_url = "http://192.168.10.61:8000"
    problem = machine.public_url_problem(cfg, FOUND)
    assert problem and problem["better"] == ["http://192.168.10.243:8000", "http://face-detection.local:8000"]
    assert "192.168.10.61" in problem["text"]


def test_public_url_fine_cases():
    cfg = Config()
    for url in ("http://192.168.10.243:8000", "http://face-detection.local:8000", "http://pc:8000", ""):
        cfg.web.public_url = url
        assert machine.public_url_problem(cfg, FOUND) is None, url


# --- power -----------------------------------------------------------------------------------

def test_pmset_parsers():
    assert machine.parse_batt(BATT_AC_5W) == {"on_ac": True, "battery": 6, "charging": False}
    assert machine.parse_batt(BATT_CHARGING) == {"on_ac": True, "battery": 30, "charging": True, "remaining": "1:09"}
    assert machine.parse_batt(BATT_BATTERY) == {"on_ac": False, "battery": 15, "charging": False, "remaining": "0:40"}
    assert machine.parse_therm(THERM) == 24
    assert machine.parse_therm("Note: No thermal warning level has been recorded") is None
    assert machine.parse_adapter(ADAPTER) == 60
    assert machine.parse_adapter("No adapter attached.") is None


def test_problems():
    weak = Power(on_ac=True, battery=60, charging=False, adapter_w=5, speed_limit=24)
    assert set(machine.problems(weak)) == {"weak_adapter", "throttled"}
    # On the charger but not charging, low: critical (and not also "low battery")
    assert set(machine.problems(Power(on_ac=True, battery=6, charging=False, adapter_w=5))) == {"not_charging"}
    assert set(machine.problems(Power(on_ac=True, battery=80, charging=False, adapter_w=96))) == set()  # held at 80%
    unplugged = machine.problems(Power(on_ac=False, battery=15, charging=False, remaining="0:40"))
    assert set(unplugged) == {"battery_power"} and "CRITICAL" in unplugged["battery_power"] and "0:40 left" in unplugged["battery_power"]
    assert machine.problems(Power(on_ac=True, battery=30, charging=True, adapter_w=60, speed_limit=100)) == {}


def watch(core, readings):
    app, clock, sent = core
    app.cfg.web.public_url = "http://pc:8000"
    w = MachineWatch(app.cfg, app.store, lambda text: sent.append(text), clock,
                     read=lambda: readings[0], found=lambda: FOUND)
    return w, sent


def test_alerts_once_then_recovery(core):
    readings = [Power(on_ac=True, battery=50, charging=True, adapter_w=5, speed_limit=100)]
    w, sent = watch(core, readings)
    w.check(t=1000)
    w.check(t=1060)
    assert sum("charger gives only 5 W" in s for s in sent) == 1
    readings[0] = Power(on_ac=True, battery=50, charging=True, adapter_w=96, speed_limit=100)
    w.check(t=1120)
    assert any("charger is strong enough again" in s for s in sent)
    n = len(sent)
    w.check(t=1180)
    assert len(sent) == n


def test_throttling_reported_after_five_minutes(core):
    readings = [Power(on_ac=True, battery=80, charging=True, adapter_w=96, speed_limit=24)]
    w, sent = watch(core, readings)
    w.check(t=0)
    w.check(t=200)
    assert not any("CPU speed" in s for s in sent)
    w.check(t=machine.THROTTLED_FOR_S + 1)
    assert sum("24% of its CPU speed" in s for s in sent) == 1


def test_wrong_page_address_posted_once_a_day(core):
    app, clock, sent = core
    w, sent = watch(core, [None])
    app.cfg.web.public_url = "http://192.168.10.61:8000"
    w.check(t=0)
    w.check(t=60)
    assert sum("not this computer's" in s for s in sent) == 1


def test_status_page_shows_power_and_placement(core):
    from attendance.web import create_app

    app, clock, sent = core
    app.machine.power = Power(on_ac=True, battery=6, charging=False, adapter_w=5, speed_limit=24, checked_at=1)
    app.placement.add_frame(DAY, [SimpleNamespace(size=40, sharpness=100, yaw=0, pitch=0)], app.cfg.recognition, 1)
    web = create_app(app)
    web.testing = True
    html = web.test_client().get("/status").get_data(as_text=True)
    assert "5 W" in html and "CPU at 24% speed" in html
    assert "Faces the camera saw" in html and "45 px" in html


# --- placement --------------------------------------------------------------------------------

def face(size=100, sharp=100, yaw=0, pitch=0):
    return SimpleNamespace(size=size, sharpness=sharp, yaw=yaw, pitch=pitch)


def test_classify_order():
    rc = Config().recognition
    assert placement.classify(face(size=30, yaw=80), rc) == "small"
    assert placement.classify(face(sharp=5, yaw=80), rc) == "blur"
    assert placement.classify(face(yaw=80), rc) == "pose"
    assert placement.classify(face(), rc) == "good"


def test_hints_rules():
    s = placement.empty()
    s["connected_s"] = 3600
    assert "saw no faces" in placement.hints(s, 60)[0]
    s["connected_s"] = 600
    assert placement.hints(s, 60) == []
    s.update(faces=20, small=15)
    s["sizes"][4] = 15
    s["sizes"][9] = 5
    assert "too small" in placement.hints(s, 60)[0] and "45 px" in placement.hints(s, 60)[0]
    s.update(small=2, pose=12)
    assert "from the side" in placement.hints(s, 60)[0]
    s.update(pose=2, good=16)
    assert placement.hints(s, 60) == []
    s.update(faces=5, small=5)  # too few to judge
    assert placement.hints(s, 60) == []


def test_placement_saved_and_reloaded(env):
    service, store, clock, sent = env
    rc = Config().recognition
    p = Placement(store)
    p.add_frame(DAY, [face(size=30), face(yaw=80)], rc, gap_s=1)
    p.add_frame(DAY, [], rc, gap_s=30)  # long gaps count 5 s at most
    p.save()
    again = Placement(store)
    day = again.day(DAY)
    assert (day["frames"], day["faces"], day["small"], day["pose"], day["connected_s"]) == (2, 2, 1, 1, 6.0)
    rows = again.recent(DAY + dt.timedelta(days=1), 7, 60)
    assert rows[0]["day"] == DAY and rows[0]["small"] == 0.5


def test_summary_gets_placement_hint(core):
    app, clock, sent = core
    rc = app.cfg.recognition
    for _ in range(400):  # 400 frames x 5 s: well over half an hour of picture, no faces
        app.placement.add_frame(DAY, [], rc, gap_s=5)
    text = app.service.summary_text(app.service.report(DAY))
    assert "saw no faces" in text


def test_lasting_problem_repeated_after_six_hours(core):
    readings = [Power(on_ac=True, battery=50, charging=True, adapter_w=30)]  # not critical: every six hours
    w, sent = watch(core, readings)
    w.check(t=0)
    w.check(t=3600)
    w.check(t=machine.ALERT_EVERY_S + 1)
    assert sum("charger gives only 30 W" in s for s in sent) == 2


def test_an_unplugged_charger_is_critical_every_time_and_repeated_until_fixed(core):
    plugged = Power(on_ac=True, battery=90, charging=True, adapter_w=96, speed_limit=100)
    unplugged = Power(on_ac=False, battery=60, charging=False, speed_limit=100)
    readings = [unplugged]
    w, sent = watch(core, readings)
    critical = lambda: [m for m in sent if "CRITICAL" in m]  # noqa: E731
    w.check(t=0)
    assert len(critical()) == 1  # at once
    w.check(t=machine.POWER_CHECK_S)
    assert len(critical()) == 1
    w.check(t=machine.CRITICAL_EVERY_S + 1)
    assert len(critical()) == 2  # again while it lasts
    readings[0] = plugged
    w.check(t=machine.CRITICAL_EVERY_S + 30)
    assert sent[-1] == "✅ The attendance computer is on mains power again."
    readings[0] = unplugged  # pulled out again an hour later: not silenced for hours
    w.check(t=machine.CRITICAL_EVERY_S + 3600)
    assert len(critical()) == 3


def test_plugged_in_but_not_charging_is_critical_after_a_few_minutes(core):
    readings = [Power(on_ac=True, battery=30, charging=False, adapter_w=96, speed_limit=100)]
    w, sent = watch(core, readings)
    w.check(t=0)
    w.check(t=100)
    assert not sent  # a charger just plugged in takes a moment
    w.check(t=machine.NOT_CHARGING_FOR_S + 1)
    assert len(sent) == 1 and "plugged in but not charging" in sent[0]
    readings[0] = Power(on_ac=True, battery=31, charging=True, adapter_w=96, speed_limit=100)
    w.check(t=machine.NOT_CHARGING_FOR_S + 30)
    assert sent[-1] == "✅ The attendance computer is charging again."
