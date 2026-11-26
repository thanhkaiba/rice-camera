"""Faces from a .zip of photos go to Review (core/photobatch.py): uploaded on the server, read on the camera computer."""
import base64
import io
import json
import re
import zipfile
from types import SimpleNamespace

import numpy as np

from attendance.core import photobatch


def vec(n):
    v = np.zeros(512, np.float32)
    v[n] = 1
    return v


class FakeRecognizer:
    """Two faces in the first tile of every photo: person 0 and person 1 (person 1's face is blurred)."""

    def __init__(self):
        self.crops = 0

    def analyze(self, tile):
        if tile.shape[1] < 900:
            return []
        return [SimpleNamespace(bbox=(100, 100, 300, 300)), SimpleNamespace(bbox=(500, 100, 700, 300))]

    def analyze_photo(self, crop):
        self.crops += 1
        blurred = self.crops % 2 == 0
        return [SimpleNamespace(quality_ok=not blurred, det_score=0.9, size=150, landmarks=None,
                                embedding=vec(0) if not blurred else vec(1))]


def photos_zip(path, count):
    import cv2

    with zipfile.ZipFile(path, "w") as z:
        for n in range(count):
            ok, jpg = cv2.imencode(".jpg", np.full((800, 1000, 3), 120 + n, np.uint8))
            z.writestr(f"trip/DSC{n:04d}.JPG", jpg.tobytes())
        z.writestr("__MACOSX/trip/._DSC0000.JPG", b"not a photo")
        z.writestr("notes.txt", b"ignored")


def test_clear_faces_are_kept_at_most_three_alike_ones_of_a_person(tmp_path, monkeypatch):
    import attendance.vision.recognizer as recognizer

    monkeypatch.setattr(recognizer, "looks_covered", lambda crop, landmarks: False)
    photos_zip(tmp_path / "p.zip", 5)
    kept, counts = photobatch.extract(None, tmp_path / "p.zip", tmp_path / "out", "faces_x", recognizer=FakeRecognizer())
    assert counts["photos"] == 5 and counts["faces"] == 10 and counts["unclear"] == 5
    # the same face in every photo: the first kept, the rest too alike to add anything
    assert len(kept) == 1 and counts["repeat"] == 4 and counts["people"] == 1
    assert (tmp_path / "out" / kept[0]["file"]).is_file() and (tmp_path / "out" / kept[0]["scene"]).is_file()
    assert kept[0]["photo"] == "DSC0000.JPG"


def test_upload_makes_a_job_and_the_result_puts_the_faces_on_review(core):
    from attendance.web import create_app

    app, _, _ = core
    client = create_app(app).test_client()
    page = client.get("/employees").get_data(as_text=True)
    assert "Faces from photos (.zip)" in page
    token = re.search(r'name="csrf-token" content="([^"]*)"', page)
    extra = {"csrf_token": token.group(1)} if token else {}
    answer = client.post("/employees/faces-from-photos", data={**extra, "file": (io.BytesIO(b"no zip"), "x.zip")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert "not a .zip" in answer.get_data(as_text=True) and not app.store.edge_jobs()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.jpg", b"\xff\xd8")
    client.post("/employees/faces-from-photos", data={**extra, "file": (io.BytesIO(buf.getvalue()), "trip.zip")},
                content_type="multipart/form-data")
    job = app.store.edge_jobs()[-1]
    assert job["kind"] == "faces" and job["payload"]["filename"] == "trip.zip"
    assert "is waiting for" in client.get("/employees").get_data(as_text=True)

    crops = app.data_dir / "crops" / "2026-11-26"
    crops.mkdir(parents=True)
    (crops / "faces_ab_000.jpg").write_bytes(b"\xff\xd8")
    (crops / "faces_ab_000_scene.jpg").write_bytes(b"\xff\xd8")
    samples = [{"image": "crops/2026-11-26/faces_ab_000.jpg", "vector": base64.b64encode((vec(3) * 2).tobytes()).decode()},
               {"image": "crops/2026-11-26/missing.jpg", "vector": base64.b64encode(vec(4).tobytes()).decode()},
               {"image": "../etc/passwd", "vector": "x"}]
    assert photobatch.add_to_review(app, samples, app.service.model_id) == 1
    card = app.store.unknowns(status=None)[-1]
    assert card.source == "seed" and card.image == "crops/2026-11-26/faces_ab_000.jpg"
    assert np.allclose(card.vector, vec(3)) and not app.store.checkins(card.ts.date())


def test_the_camera_computer_reads_the_zip_outside_the_camera_hours_and_sends_the_faces(core, tmp_path):
    from attendance.core.edge import EdgeClient

    app, _, _ = core
    app.cfg.edge.server_url, app.cfg.edge.token = "https://server", "t"
    edge = EdgeClient(app.cfg.edge, app.store, tmp_path)
    calls = []

    posted = []

    def send(method, path, body, kind):
        calls.append((method, path))
        if method == "POST":
            posted.append(json.loads(body))
        return (200, b"PK zip bytes") if method == "GET" else (200, b"{}")
    edge._send = send

    def faces(archive, folder, stem):
        folder.mkdir(parents=True)
        (folder / f"{stem}_000.jpg").write_bytes(b"\xff\xd8")
        (folder / f"{stem}_000_scene.jpg").write_bytes(b"\xff\xd8")
        return [{"file": f"{stem}_000.jpg", "scene": f"{stem}_000_scene.jpg", "vector": vec(2), "photo": "a.jpg"}], \
            {"photos": 1, "people": 1}, "w600k_r50"
    edge.faces_from_photos = faces
    job = {"id": 7, "kind": "faces", "payload": {"image": "uploads/" + "a" * 32, "batch": "ab"}}
    edge.faces_busy = lambda: True
    edge._do_job(job)
    assert not calls and not edge._face_jobs  # the camera hours: not started
    edge.faces_busy = lambda: False
    edge._do_job(job)
    edge._face_jobs["7"]["thread"].join(5)
    edge._do_job(job)
    puts = [p for m, p in calls if m == "PUT"]
    assert len(puts) == 2 and puts[0].endswith("faces_ab_000.jpg") and puts[1].endswith("faces_ab_000_scene.jpg")
    assert calls[-1] == ("POST", "/api/edge/jobs/7/result")
    sent = posted[-1]
    assert sent["ok"] and sent["model"] == "w600k_r50" and sent["samples"][0]["image"].endswith("/faces_ab_000.jpg")
    assert np.allclose(np.frombuffer(base64.b64decode(sent["samples"][0]["vector"]), np.float32), vec(2))
    assert not (tmp_path / "photo-batches" / "7").exists()  # the photos are not kept here


def test_a_big_zip_comes_in_parts_and_starts_the_job_with_the_last(core):
    from attendance.web import create_app

    app, _, _ = core
    client = create_app(app).test_client()
    page = client.get("/employees").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]*)"', page)
    headers = {"X-CSRF-Token": token.group(1)} if token else {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.jpg", b"\xff\xd8" * 1000)
    data, upload = buf.getvalue(), "c" * 32
    half = len(data) // 2

    def part(offset, chunk):
        return client.post(f"/employees/faces-from-photos/part?upload={upload}&offset={offset}&total={len(data)}&name=trip.zip",
                           data=chunk, headers=headers, content_type="application/octet-stream")
    assert part(half, data[half:]).status_code == 409  # out of order
    first = part(0, data[:half])
    assert first.get_json() == {"ok": True, "done": False} and not app.store.edge_jobs()
    last = part(half, data[half:])
    assert last.get_json() == {"ok": True, "done": True}
    job = app.store.edge_jobs()[-1]
    assert job["kind"] == "faces" and job["payload"]["image"] == f"uploads/{upload}"
    assert (app.data_dir / job["payload"]["image"]).read_bytes() == data
    assert "faces-progress" in client.get("/employees").get_data(as_text=True)
