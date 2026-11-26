"""A seed uploaded on the server, which has no face models: the office computer reads the faces."""
import base64
import csv
import hashlib
import json
import zipfile

import numpy as np

from attendance.cli import seed
from conftest import at, person_vector
from test_edge import live  # noqa: F401


def seed_zip(tmp_path, with_people=True):
    root = tmp_path / "build" / "seed"
    (root / "review").mkdir(parents=True)
    sample = b"review face bytes"
    (root / "review/sample.jpg").write_bytes(sample)
    row = {"file": "review/sample.jpg", "captured_at": at(9, 0).isoformat(), "sha256": hashlib.sha256(sample).hexdigest()}
    with (root / "review.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    names = ["review.csv", "review/sample.jpg"]
    if with_people:
        (root / "faces/bob@example.com").mkdir(parents=True)
        (root / "faces/bob@example.com/a.jpg").write_bytes(b"bob photo")
        (root / "employees.csv").write_text("email,name\nbob@example.com,Bob\n", encoding="utf-8")
        names += ["faces/bob@example.com/a.jpg"]
    (root / "manifest.json").write_text(json.dumps({n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}))
    archive = tmp_path / "seed.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in root.rglob("*"):
            z.write(path, path.relative_to(root.parent).as_posix())
    return archive, row["sha256"]


def run(core, archive, review_only=False):
    seed._running.acquire()
    seed._run_remote(core, archive, review_only, "admin")


def test_a_seed_on_the_server_sends_photos_and_review_faces_to_the_office_computer(live, tmp_path):
    _, core, _ = live
    core.recognizer = lambda: (_ for _ in ()).throw(FileNotFoundError("det_10g.onnx not found"))
    archive, digest = seed_zip(tmp_path)
    run(core, archive)
    status = seed.read_status(core)
    assert status["state"] == "waiting" and "1 photos sent" in status["message"], status
    bob = core.store.employee_by_email("bob@example.com")
    jobs = {j["kind"]: j for j in core.store.edge_jobs()}
    assert jobs["add_photo"]["payload"]["employee_id"] == bob.id and jobs["add_photo"]["payload"]["source"] == "enroll"
    assert (core.data_dir / jobs["add_photo"]["payload"]["image"]).read_bytes() == b"bob photo"
    job = jobs["seed_vectors"]
    kept = core.data_dir / job["payload"]["image"]
    assert kept.is_file() and not archive.exists()
    assert core.store.unknowns(status=None) == []  # not before the office computer read the faces
    vector = person_vector(3).astype(np.float32)
    answer = {"ok": True, "model": core.service.model_id, "vectors": {digest: base64.b64encode(vector.tobytes()).decode()}}
    seed.finish_remote(core, job["id"], job["payload"], answer).join(10)
    status = seed.read_status(core)
    assert status["state"] == "done" and "1 review samples added" in status["message"], status
    [u] = core.store.unknowns(status=None)
    assert u.source == "seed" and np.allclose(u.vector, vector / np.linalg.norm(vector), atol=1e-6)
    assert not kept.exists() and not core.store.get(seed.REMOTE_KEY)


def test_the_office_computer_failing_is_shown_and_an_old_answer_is_ignored(live, tmp_path):
    _, core, _ = live
    archive, _ = seed_zip(tmp_path, with_people=False)
    run(core, archive, review_only=True)
    job = core.store.edge_jobs()[0]
    seed.finish_remote(core, job["id"] + 99, job["payload"], {"ok": True, "vectors": {}}).join(10)
    assert seed.read_status(core)["state"] == "waiting"  # not the answer to this seed
    seed.finish_remote(core, job["id"], job["payload"], {"ok": False, "message": "Camera: no models"}).join(10)
    status = seed.read_status(core)
    assert status["state"] == "failed" and "no models" in status["message"]
    assert core.store.unknowns(status=None) == []


def test_the_office_computer_reads_the_review_faces_of_a_seed(core, tmp_path, monkeypatch):
    app, _, _ = core
    archive, digest = seed_zip(tmp_path, with_people=False)
    vector = person_vector(5).astype(np.float32)
    monkeypatch.setattr(seed, "_review_vector", lambda app, path, row=None, analyze=True: vector)
    answer = seed.review_vectors(app, archive, tmp_path / "work")
    assert answer["ok"] and answer["model"] == app.service.model_id
    assert np.allclose(np.frombuffer(base64.b64decode(answer["vectors"][digest]), np.float32), vector)


def test_the_mac_runs_a_seed_vectors_job_outside_the_camera_hours(core, tmp_path):
    from attendance.core.edge import EdgeClient
    app, _, _ = core
    app.cfg.edge.server_url, app.cfg.edge.token = "https://server", "t"
    edge = EdgeClient(app.cfg.edge, app.store, tmp_path)
    calls, posted = [], []

    def send(method, path, body, kind):
        calls.append((method, path))
        if method == "POST":
            posted.append(json.loads(body))
        return (200, b"PK zip") if method == "GET" else (200, b"{}")
    edge._send = send
    edge.seed_vectors = lambda archive, folder: {"ok": True, "model": "m", "vectors": {"x": "AAAA"},
                                                 "message": "1 of 1"} if archive.read_bytes() == b"PK zip" else {}
    job = {"id": 8, "kind": "seed_vectors", "payload": {"kind": "seed_vectors", "image": "uploads/" + "b" * 32}}
    edge.faces_busy = lambda: True
    edge._do_job(job)
    assert not calls
    edge.faces_busy = lambda: False
    edge._do_job(job)
    edge._face_jobs["8"]["thread"].join(5)
    edge._do_job(job)
    assert calls[-1] == ("POST", "/api/edge/jobs/8/result") and posted[-1]["vectors"] == {"x": "AAAA"}
    assert not (tmp_path / "seed-vectors" / "8").exists()
