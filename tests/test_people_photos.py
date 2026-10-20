"""Reference photo quality on the People page, photos captured from the camera, and seed import."""
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
import zipfile

import pytest

from attendance.store import Store
from attendance.vision.faces import encode_jpeg
from conftest import FakeRecognizer, at, face_image, person_vector
from test_web import client

BLURRED_PERSON = 5  # FakeRecognizerWithQuality: this person's photos are blurred


class FakeRecognizerWithQuality(FakeRecognizer):
    def largest_face(self, image, mirror=False):
        face = super().largest_face(image, mirror)
        if face is not None and round(float(image.mean()) / 10) == BLURRED_PERSON:
            face.quality_ok = False
        return face


def jpeg(person: int, size: int = 200) -> bytes:
    return encode_jpeg(face_image(person, size))


def add_person(app, email, name):
    return app.store.add_employee(email, name)


def test_photo_quality_is_stored_counted_and_shown(core):
    app, _, _ = core
    app._recognizer = FakeRecognizerWithQuality()
    dan = add_person(app, "dan@example.com", "Dan")
    assert app.library.add_photo(dan, face_image(4)).ok
    result = app.library.add_photo(dan, face_image(BLURRED_PERSON))
    assert result.ok and any("blurred" in w for w in result.warnings)
    assert app.library.quality(dan)[dan] == (2, 1, 0)
    c = client(app)
    page = c.get("/employees").get_data(as_text=True)
    assert "1/2 good" in page and "need photos" in page and 'data-needs="1"' in page
    page = c.get(f"/employees/{dan}").get_data(as_text=True)
    assert "1/2 good photos" in page and "blurred</span>" in page


def test_people_with_enough_good_photos_are_not_flagged(core):
    app, _, _ = core
    dan = add_person(app, "dan@example.com", "Dan")
    for _ in range(5):
        app.library.add_photo(dan, face_image(4))
    page = client(app).get("/employees").get_data(as_text=True)
    assert "5/5 good" in page
    assert re.search(r'data-name="dan dan@example.com" data-needs="0"', page)


def test_old_photos_are_rated_once(core):
    app, _, _ = core
    app._recognizer = FakeRecognizerWithQuality()
    dan = add_person(app, "dan@example.com", "Dan")
    for person, name in ((4, "good.jpg"), (BLURRED_PERSON, "blurred.jpg")):
        (app.data_dir / "faces" / dan).mkdir(parents=True, exist_ok=True)
        (app.data_dir / "faces" / dan / name).write_bytes(jpeg(person))
        app.store.add_embedding(app.library.model_id, dan, person_vector(person), "upload", at(8, 0),
                                f"faces/{dan}/{name}")
    assert app.library.quality(dan)[dan] == (2, 0, 2)
    assert "quality not checked yet" in client(app).get("/employees").get_data(as_text=True)
    assert app.library.rate_missing() == 2
    assert app.library.quality(dan)[dan] == (2, 1, 0)
    assert app.library.rate_missing() == 0


def test_old_database_gets_the_quality_column(tmp_path):
    path = tmp_path / "old.db"
    Store(path).close()
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE embeddings DROP COLUMN quality_ok")
    store = Store(path)
    store.add_embedding("m", "A", person_vector(1), "upload", at(8, 0), "faces/A/x.jpg", True)
    assert store.references("m")[0].quality_ok is True


def test_employee_page_lists_look_alikes(core):
    app, _, _ = core
    dan = add_person(app, "dan@example.com", "Dan")
    app.library.add_photo(dan, face_image(1))  # looks like Alice
    page = client(app).get(f"/employees/{dan}").get_data(as_text=True)
    assert "Reference photos of Dan look like" in page and "Alice" in page


def test_capture_from_the_camera_adds_a_reference_photo(core):
    app, _, _ = core
    dan = add_person(app, "dan@example.com", "Dan")
    c = client(app)
    resp = c.post("/live/capture", follow_redirects=True)
    assert "No camera picture right now" in resp.get_data(as_text=True)

    app.live.set_frame(b"jpeg", face_image(4, 300))
    resp = c.post("/live/capture")
    assert resp.status_code == 302
    token = resp.headers["Location"].rsplit("/", 1)[1]
    page = c.get(f"/live/capture/{token}").get_data(as_text=True)
    assert "Who is this?" in page and "Sharp, frontal" in page
    assert c.get(f"/media/captures/{token}_0.jpg").status_code == 200

    page = c.post(f"/live/capture/{token}/0", data={"employee_id": dan}, follow_redirects=True).get_data(as_text=True)
    assert "Photo added for Dan" in page and "Added for Dan" in page
    refs = app.store.references(app.library.model_id, dan)
    assert len(refs) == 1 and refs[0].source == "capture" and refs[0].quality_ok is True
    assert any(action == "capture photo" for _, _, action, _ in app.store.audit())
    assert c.post(f"/live/capture/{token}/7", data={"employee_id": dan}).status_code == 404
    assert c.get("/live/capture/../../x").status_code == 404


def test_capture_suggests_the_person_recognised(core):
    app, _, _ = core
    app.live.set_frame(b"jpeg", face_image(1, 300))  # Alice
    c = client(app)
    token = c.post("/live/capture").headers["Location"].rsplit("/", 1)[1]
    assert '<option value="A" selected>' in c.get(f"/live/capture/{token}").get_data(as_text=True)


# seed import -------------------------------------------------------------------------------------

def make_seed(folder, people=(("alice@example.com", "Alice", 1), ("dan@example.com", "Dan", 4))):
    """A seed folder: employees.csv, two photos per person, manifest.json."""
    folder.mkdir(parents=True)
    with (folder / "employees.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["email", "name"])
        for email, name, _ in people:
            writer.writerow([email, name])
    for email, _, person in people:
        (folder / "faces" / email).mkdir(parents=True)
        for i in range(2):
            (folder / "faces" / email / f"{i}.jpg").write_bytes(jpeg(person))
    manifest = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in folder.rglob("*") if p.is_file()}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


def test_seed_import_adds_people_and_photos_with_a_backup_and_report(core, tmp_path):
    from attendance.cli.seed import import_seed

    app, _, sent = core
    report = import_seed(app, make_seed(tmp_path / "seed"), progress=lambda text: None)
    assert report["employees"] == 2 and report["imported"] == 4 and report["failed"] == []
    dan = app.store.employee_by_email("dan@example.com")
    assert dan is not None and len(app.store.references(app.library.model_id, dan.id)) == 2
    assert (app.data_dir / "backups").glob("before-seed-*")
    assert list(app.data_dir.glob("seed-import-*.json"))
    assert not sent  # no Chat alerts for a bulk import
    assert app.gallery.counts().get(dan.id) == 2

    # Again: photos from the seed replace those from the previous import instead of piling up.
    import_seed(app, make_seed(tmp_path / "again"), progress=lambda text: None)
    assert len(app.store.references(app.library.model_id, dan.id)) == 2


def test_a_changed_seed_is_refused_before_anything_is_written(core, tmp_path):
    from attendance.cli.seed import import_seed

    app, _, _ = core
    seed = make_seed(tmp_path / "seed")
    (seed / "faces" / "dan@example.com" / "0.jpg").write_bytes(jpeg(6))
    with pytest.raises(ValueError, match="missing or changed"):
        import_seed(app, seed, progress=lambda text: None)
    assert app.store.employee_by_email("dan@example.com") is None
    assert not (app.data_dir / "backups").exists()


def zipped(seed) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path in seed.rglob("*"):
            if path.is_file():
                z.write(path, "seed/" + path.relative_to(seed).as_posix())
    return buf.getvalue()


def wait_for_import(c, timeout_s=10.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = c.get("/employees/seed.json").get_json()
        if status.get("state") != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("the seed import did not finish")


def test_seed_zip_uploaded_on_the_people_page_is_imported_with_the_camera_paused(core, tmp_path, monkeypatch):
    from attendance.cli import seed as seed_module

    app, _, _ = core
    paused = []
    real = seed_module.import_seed

    def watching(*args, **kwargs):
        paused.append(app.maintenance)
        return real(*args, **kwargs)

    monkeypatch.setattr(seed_module, "import_seed", watching)
    c = client(app)
    resp = c.post("/employees/seed", data={"file": (io.BytesIO(zipped(make_seed(tmp_path / "seed"))), "seed.zip")},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "Seed import started" in resp.get_data(as_text=True)
    status = wait_for_import(c)
    assert status["state"] == "done" and "2 employees, 4/4 photos" in status["message"]
    assert paused == ["importing a seed"] and app.maintenance is None
    assert app.store.employee_by_email("dan@example.com") is not None
    assert not list((app.data_dir / "seed-upload").iterdir())  # the upload is removed
    assert "Seed imported" in c.get("/employees").get_data(as_text=True)


def test_a_bad_seed_upload_reports_the_problem(core, tmp_path):
    app, _, _ = core
    c = client(app)
    page = c.post("/employees/seed", data={"file": (io.BytesIO(b"not a zip"), "seed.zip")},
                  content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "not a zip file" in page

    seed = make_seed(tmp_path / "seed")
    (seed / "employees.csv").write_text("email,name\nother@example.com,Other\n")
    c.post("/employees/seed", data={"file": (io.BytesIO(zipped(seed)), "seed.zip")}, content_type="multipart/form-data")
    status = wait_for_import(c)
    assert status["state"] == "failed" and "missing or changed" in status["message"]
    assert "Seed import failed" in c.get("/employees").get_data(as_text=True)


def test_seed_zip_is_found_inside_its_folder(tmp_path):
    from attendance.cli.seed import find_seed, unpack

    archive = tmp_path / "seed.zip"
    archive.write_bytes(zipped(make_seed(tmp_path / "seed")))
    folder = unpack(archive, tmp_path / "unpacked")
    assert folder == tmp_path / "unpacked" / "seed" and find_seed(tmp_path / "unpacked") == folder
    with pytest.raises(ValueError, match="no manifest"):
        find_seed(tmp_path / "unpacked" / "seed" / "faces")


def test_zip_names_without_the_utf8_flag_are_read_as_utf8(tmp_path):
    from attendance.cli.seed import unpack

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("seed/manifest.json", "{}")
        z.writestr("seed/XXXnh.jpg", b"x")  # as long as "Ảnh" in UTF-8: swapped in below without the flag
    archive = tmp_path / "seed.zip"
    archive.write_bytes(buf.getvalue().replace(b"seed/XXXnh.jpg", "seed/Ảnh.jpg".encode("utf-8")))
    assert (unpack(archive, tmp_path / "out") / "Ảnh.jpg").read_bytes() == b"x"


def test_seed_folder_script_still_works(core, tmp_path, monkeypatch):
    from attendance.cli.seed import script_main

    app, _, _ = core
    seed = make_seed(tmp_path / "seed")
    config = tmp_path / "config.yaml"
    config.write_text("data_dir: data\nemployees_file: employees.csv\n")
    monkeypatch.setattr("sys.argv", ["import_seed.py", "--config", str(config)])
    monkeypatch.setattr("attendance.core.pipeline.App.recognizer", lambda self: FakeRecognizer())
    script_main(seed)
    assert Store(tmp_path / "data" / "attendance.db").employee_by_email("dan@example.com") is not None


def test_seed_import_with_some_photos_rejected_is_done_not_failed(core, tmp_path, monkeypatch):
    import json
    import threading

    from attendance.cli import seed as seed_module

    app, _, _ = core

    def partly(app_, seed_dir, review_only, progress):
        raise seed_module.PhotosRejected("1 photos were rejected", {"employees": 2, "imported": 3, "failed": [{}], "warnings": [],
                                                                     "backup": "b", "expected_photos": 4})
    monkeypatch.setattr(seed_module, "import_seed", partly)
    monkeypatch.setattr(seed_module, "unpack", lambda archive, work: work)
    archive = tmp_path / "s.zip"
    archive.write_bytes(b"")
    seed_module._running.acquire()  # held by the page's start(); released by _run
    seed_module._run(app, archive, False, "admin")
    status = json.loads((app.data_dir / seed_module.STATUS_FILE).read_text())
    assert status["state"] == "done" and "rejected" in status["message"]
    assert any(a == "import seed" for _, _, a, _ in app.store.audit())
