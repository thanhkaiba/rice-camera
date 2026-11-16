import numpy as np
from conftest import face_image, person_vector

from attendance.config import RecognitionConfig
from attendance.posecal import LIMIT, MIN_SAMPLES, PoseCalibration
from attendance.processing import FrameProcessor
from attendance.recognizer import DetectedFace, FaceRecognizer, head_pose
from attendance.store import Store


def landmarks(pitch: float = 0.0, yaw_shift: float = 0.0) -> np.ndarray:
    """Eyes 40 px apart at y=100, mouth at y=140, nose placed to give the wanted pose estimate."""
    nose_y = 100 + (0.55 + pitch / 150) * 40
    return np.array([[80, 100], [120, 100], [100 + yaw_shift, nose_y], [85, 140], [115, 140]], dtype=np.float32)


def test_head_pose_is_signed():
    yaw, pitch = head_pose(landmarks(pitch=40, yaw_shift=10))
    assert round(pitch) == 40 and yaw > 0
    yaw, pitch = head_pose(landmarks(pitch=-25, yaw_shift=-10))
    assert round(pitch) == -25 and yaw < 0


def test_calibration_learns_the_median_after_enough_faces(tmp_path):
    store = Store(tmp_path / "db")
    cal = PoseCalibration(store)
    for i in range(MIN_SAMPLES - 1):
        cal.add(5, -20 + (i % 3))
    assert not cal.active and cal.offset == (0.0, 0.0) and "learning" in cal.describe()
    cal.add(5, -19)
    assert cal.active and cal.offset == (5.0, -19.0)
    assert "from below" in cal.describe() and "to the right" in cal.describe()
    cal.save()
    again = PoseCalibration(store)  # survives a restart
    assert again.offset == (5.0, -19.0)
    again.reset()
    assert PoseCalibration(store).samples == 0


def test_calibration_is_bounded_and_can_be_turned_off():
    on = {"v": True}
    cal = PoseCalibration(enabled=lambda: on["v"])
    for _ in range(MIN_SAMPLES):
        cal.add(0, 80)
    assert cal.offset == (0.0, LIMIT)
    on["v"] = False
    assert cal.offset == (0.0, 0.0) and cal.describe() == "off"


class Backend:
    def __init__(self, kps):
        self.kps = kps

    def detect(self, frame, threshold=0.5, det_size=None):
        return [(np.array([60, 60, 160, 180], dtype=np.float32), 0.95, self.kps)]

    def embed(self, frame, kps):
        return person_vector(1)


def recognizer(kps) -> FaceRecognizer:
    r = object.__new__(FaceRecognizer)
    r.cfg = RecognitionConfig(min_sharpness=0)
    r.backend = Backend(kps)
    return r


def test_pose_check_is_relative_to_the_camera_angle():
    frame = np.random.default_rng(0).integers(0, 255, (240, 240, 3), dtype=np.uint8)
    r = recognizer(landmarks(pitch=-38))  # a desk camera looking up at people
    [face] = r.analyze(frame)
    assert not face.quality_ok and round(face.raw_pitch) == -38
    [face] = r.analyze(frame, pose_offset=(0.0, -35.0))
    assert face.quality_ok and round(face.pitch) == -3 and round(face.raw_pitch) == -38


class PoseRecognizer:
    def analyze(self, frame, regions=(), region_size=320, full_frame=True, pose_offset=(0.0, 0.0)):
        self.offset = pose_offset
        clear = person_vector(1)
        unclear = person_vector(1) + person_vector(2) * 1.2
        return [DetectedFace((10.0, 10.0, 110.0, 110.0), clear, 0.9, 100.0, 100.0, 0.0, -38.0, False, 0.5,
                             None, True, 2.0, -38.0),
                DetectedFace((200.0, 10.0, 300.0, 110.0), unclear / np.linalg.norm(unclear), 0.9, 100.0, 100.0,
                             0.0, -60.0, False, 0.5, None, True, 9.0, -60.0)]


def test_only_clear_matches_teach_the_camera_angle(core):
    app, clock, _ = core
    app._recognizer = PoseRecognizer()
    proc = FrameProcessor(app)
    proc.process(face_image(1), clock())
    assert app.pose.samples == 1 and app.pose.learned == (2.0, -38.0)
    assert app._recognizer.offset == (0.0, 0.0)  # not active yet
    for _ in range(MIN_SAMPLES):
        app.pose.add(2.0, -38.0)
    proc.process(face_image(1), clock())
    assert app._recognizer.offset == (2.0, -38.0)


def test_what_was_learned_from_another_camera_is_forgotten(tmp_path):
    from attendance.posecal import camera_id

    store = Store(tmp_path / "db")
    assert camera_id("rtsp://admin:secret@192.168.1.10:554/ch1/main") == "192.168.1.10:554/ch1/main"
    assert camera_id("rtsp://other:pw@192.168.1.10:554/ch1/main") == camera_id("rtsp://192.168.1.10:554/ch1/main")
    assert camera_id("0") == "0"

    cal = PoseCalibration(store)
    for _ in range(MIN_SAMPLES):
        cal.add(-20, 10)
    assert not cal.for_camera("rtsp://a:b@192.168.1.10/ch1/main")  # first time the camera is remembered: kept
    assert not cal.for_camera("rtsp://a:new-password@192.168.1.10/ch1/main") and cal.samples == MIN_SAMPLES
    assert cal.for_camera("rtsp://a:b@192.168.1.210/Streaming/Channels/101") and cal.samples == 0
    assert PoseCalibration(store).samples == 0  # and stays forgotten after a restart
    assert not cal.for_camera("rtsp://a:b@192.168.1.210/Streaming/Channels/101")
    assert not PoseCalibration().for_camera("rtsp://x/1")  # nothing stored: nothing to forget
