import numpy as np
import pytest

from attendance.config import RecognitionConfig
from attendance.recognizer import FaceRecognizer


class CropBackend:
    def __init__(self, found_at=160, count=1):
        self.found_at = found_at
        self.count = count
        self.calls = []

    def detect(self, frame, threshold=0.5, det_size=None):
        self.calls.append(det_size)
        if det_size != self.found_at:
            return []
        return [(np.array([20, 20, 140, 160], dtype=np.float32), 0.8,
                 np.array([[50, 60], [100, 60], [75, 85], [55, 110], [95, 110]], dtype=np.float32))] * self.count

    def embed(self, frame, landmarks):
        return np.array([3, 4], dtype=np.float32)


@pytest.mark.parametrize('det_size', [640, 320])
def test_small_reference_photo_recovers_a_face_missed_when_upscaled(det_size):
    rec = object.__new__(FaceRecognizer)
    rec.cfg = RecognitionConfig(det_size=det_size)
    rec.backend = CropBackend()
    image = np.full((200, 160, 3), 128, dtype=np.uint8)
    face = rec.largest_face(image, mirror=True)
    assert face is not None
    assert face.size == 120
    np.testing.assert_allclose(face.embedding, [0.6, 0.8])
    # The camera path keeps its usual detection cost and result.
    rec.backend.calls.clear()
    assert rec.analyze(image) == []
    assert rec.backend.calls == [None]


@pytest.mark.parametrize('shape', [(640, 480), (480, 640), (1200, 1600)])
def test_large_photo_without_faces_does_not_retry_at_crop_scales(shape):
    rec = object.__new__(FaceRecognizer)
    rec.cfg = RecognitionConfig()
    rec.backend = CropBackend()
    assert rec.analyze_photo(np.zeros((*shape, 3), dtype=np.uint8)) == []
    assert rec.backend.calls == [None]


@pytest.mark.parametrize('found_at,calls', [(None, [None]), (320, [None, 320]),
                                          (160, [None, 320, 160])])
def test_photo_preserves_multiple_faces_and_stops_at_first_success(found_at, calls):
    rec = object.__new__(FaceRecognizer)
    rec.cfg = RecognitionConfig()
    rec.backend = CropBackend(found_at=found_at, count=2)
    assert len(rec.analyze_photo(np.full((200, 160, 3), 128, dtype=np.uint8))) == 2
    assert rec.backend.calls == calls


def test_photo_without_faces_exhausts_smaller_scales_once():
    rec = object.__new__(FaceRecognizer)
    rec.cfg = RecognitionConfig()
    rec.backend = CropBackend(found_at=80)
    assert rec.analyze_photo(np.zeros((200, 160, 3), dtype=np.uint8)) == []
    assert rec.backend.calls == [None, 320, 160]
