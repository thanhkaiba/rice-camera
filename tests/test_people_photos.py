"""Reference photo quality on the People page, photos captured from the camera, and seed import."""
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sqlite3
import time
import zipfile
from zoneinfo import ZoneInfo

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
            face.quality = 0.2
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


def test_photo_scores_are_saved_and_shown_per_photo_and_person(core):
    app, _, _ = core
    app._recognizer = FakeRecognizerWithQuality()
    dan = add_person(app, "dan@example.com", "Dan")
    app.library.add_photo(dan, face_image(4))
    app.library.add_photo(dan, face_image(BLURRED_PERSON))
    refs = app.store.references(app.library.model_id, dan)
    assert [r.quality_score for r in refs] == [0.8, 0.2]
    c = client(app)
    page = c.get(f"/employees/{dan}").get_data(as_text=True)
    assert "80/100" in page and "20/100" in page
    assert "50/100" in c.get("/employees").get_data(as_text=True)


def test_profile_groups_photos_by_local_monday_weeks_newest_first(core):
    app, _, _ = core
    app.store._exec('DELETE FROM embeddings WHERE employee_id = ?', ('A',))
    model = app.library.model_id
    # UTC Sunday evening is already Monday in the configured timezone.
    for timestamp, source in [('2026-10-18T16:59:00+00:00', 'admin'),
                              ('2026-10-18T17:00:00+00:00', 'admin'),
                              ('2026-10-20T08:00:00+07:00', 'capture'),
                              ('2026-10-18T10:00:00+07:00', 'capture')]:
        app.store.add_embedding(model, 'A', person_vector(1), source, dt.datetime.fromisoformat(timestamp))
    refs = app.store.references(model, 'A')
    page = client(app).get('/employees/A').get_data(as_text=True)
    weeks = re.findall(r'<section class="reference-week".*?</section>', page, re.S)
    assert len(weeks) == 2
    assert '19/10/2026 – 25/10/2026' in weeks[0]
    assert '12/10/2026 – 18/10/2026' in weeks[1]
    assert '2 photos' in weeks[0] and '2 photos' in weeks[1]
    ids = lambda section: re.findall(r'id="reference-(\d+)"', section)
    assert ids(weeks[0]) == [str(refs[2].id), str(refs[1].id)]
    assert ids(weeks[1]) == [str(refs[0].id), str(refs[3].id)]
    assert len(re.findall('form="bulk-delete-photos"', page)) == 4


def test_only_camera_and_learned_photos_are_grouped_into_weeks(core):
    app, _, _ = core
    dan = add_person(app, 'dan@example.com', 'Dan')
    sources = ['admin', 'capture', 'upload', 'enroll', 'enroll:seed', 'admin:correction', 'capture:door']
    for source in sources:
        app.store.add_embedding(app.library.model_id, dan, person_vector(1), source, at(8, 0))
    refs = app.store.references(app.library.model_id, dan)
    page = client(app).get(f'/employees/{dan}').get_data(as_text=True)
    weeks = re.findall(r'<section class="reference-week".*?</section>', page, re.S)
    other = re.findall(r'<section class="reference-other".*?</section>', page, re.S)
    assert len(weeks) == len(other) == 1
    assert set(re.findall(r'id="reference-(\d+)"', weeks[0])) == {str(refs[i].id) for i in (0, 1, 5, 6)}
    assert set(re.findall(r'id="reference-(\d+)"', other[0])) == {str(refs[i].id) for i in (2, 3, 4)}
    assert '4 photos' in weeks[0] and '3 photos' in other[0]
    assert len(re.findall('form="bulk-delete-photos"', page)) == len(refs)


def test_uploaded_photos_stay_visible_without_a_week_group(core):
    app, _, _ = core
    page = client(app).get('/employees/A').get_data(as_text=True)
    assert 'class="reference-week"' not in page
    assert 'class="reference-other"' in page
    assert 'id="reference-' in page


def test_photo_weeks_span_the_year_boundary_and_empty_profiles_have_no_groups(core):
    app, _, _ = core
    dan = add_person(app, 'dan@example.com', 'Dan')
    c = client(app)
    assert 'class="reference-week"' not in c.get(f'/employees/{dan}').get_data(as_text=True)
    for timestamp in ['2027-01-03T23:59:00+07:00', '2026-12-28T00:00:00+07:00',
                      '2027-01-04T00:00:00+07:00']:
        app.store.add_embedding(app.library.model_id, dan, person_vector(1), 'admin',
                                dt.datetime.fromisoformat(timestamp))
    page = c.get(f'/employees/{dan}').get_data(as_text=True)
    weeks = re.findall(r'<section class="reference-week".*?</section>', page, re.S)
    assert len(weeks) == 2
    assert '04/01/2027 – 10/01/2027' in weeks[0] and '1 photo' in weeks[0]
    assert '28/12/2026 – 03/01/2027' in weeks[1] and '2 photos' in weeks[1]


def test_photo_order_stays_chronological_when_daylight_saving_time_ends(core):
    app, _, _ = core
    app.service.tz = ZoneInfo('America/New_York')
    dan = add_person(app, 'dan@example.com', 'Dan')
    for timestamp in ['2026-11-01T01:45:00-04:00', '2026-11-01T01:15:00-05:00']:
        app.store.add_embedding(app.library.model_id, dan, person_vector(1), 'admin',
                                dt.datetime.fromisoformat(timestamp))
    refs = app.store.references(app.library.model_id, dan)
    page = client(app).get(f'/employees/{dan}').get_data(as_text=True)
    assert re.findall(r'id="reference-(\d+)"', page) == [str(refs[1].id), str(refs[0].id)]


def test_existing_checked_photos_get_scores_without_counting_missing_images(core):
    app, _, _ = core
    dan = add_person(app, "dan@example.com", "Dan")
    folder = app.data_dir / "faces" / dan
    folder.mkdir(parents=True)
    (folder / "old.jpg").write_bytes(jpeg(4))
    app.store.add_embedding(app.library.model_id, dan, person_vector(4), "upload", at(8, 0),
                            f"faces/{dan}/old.jpg", True)
    app.store.add_embedding(app.library.model_id, dan, person_vector(4), "upload", at(8, 0),
                            f"faces/{dan}/missing.jpg")
    assert app.library.rate_missing() == 1
    refs = app.store.references(app.library.model_id, dan)
    assert [r.quality_score for r in refs] == [0.8, None]
    assert app.library.rate_missing() == 0
    page = client(app).get("/employees").get_data(as_text=True)
    assert "80/100" in page and "1/2 scored" in page


def test_saved_photo_without_a_face_scores_zero(core):
    app, _, _ = core
    folder = app.data_dir / "faces" / "A"
    folder.mkdir(parents=True)
    (folder / "empty.jpg").write_bytes(jpeg(0))
    app.store.add_embedding(app.library.model_id, "A", person_vector(1), "admin", at(8, 0),
                            "faces/A/empty.jpg")
    assert app.library.rate_missing() == 1
    ref = app.store.references(app.library.model_id, "A")[-1]
    assert ref.quality_score == 0 and ref.quality_ok is False
    assert "0/100" in client(app).get("/employees/A").get_data(as_text=True)


def test_bulk_photo_delete_only_removes_selected_references_and_all_their_models(core):
    from test_review_controls import ReviewControls

    app, _, _ = core
    app.library.add_photo("A", face_image(1))
    app.library.add_photo("A", face_image(1))
    refs = app.store.references(app.library.model_id, "A")
    selected, kept = refs[-2:]
    app.store.add_embedding("other-model", "A", person_vector(1), "upload", at(8, 0), selected.image)
    c = client(app)
    controls = ReviewControls(c.get("/employees/A").get_data(as_text=True))
    bulk = next(f for f in controls.forms if f.get("id") == "bulk-delete-photos")
    boxes = [i for i in controls.inputs if i.get("type") == "checkbox" and i.get("name") == "ref_id"]
    assert {i["value"] for i in boxes} == {str(r.id) for r in refs}
    assert all(i.get("form") == bulk["id"] for i in boxes)
    assert bulk.get("data-confirm")
    response = c.post(bulk["action"], data={"ref_id": [str(refs[0].id), str(selected.id), str(selected.id)]})
    assert response.status_code == 302
    assert [r.id for r in app.store.references(app.library.model_id, "A")] == [kept.id]
    assert not app.store.references("other-model", "A")
    assert not (app.data_dir / selected.image).exists()
    assert (app.data_dir / kept.image).exists()
    assert app.gallery.counts()["A"] == 1
    assert len(app.store.references(app.library.model_id, "B")) == 1
    assert any(action == "delete photo" and "alice@example.com" in detail
               for _, _, action, detail in app.store.audit())


@pytest.mark.parametrize("selection", [[], ["no-number"], ["999999"], ["other-person"], ["valid", "other-person"]])
def test_bulk_photo_delete_rejects_invalid_or_foreign_selection_before_deleting(core, selection):
    app, _, _ = core
    app.library.add_photo("A", face_image(1))
    refs = app.store.references(app.library.model_id, "A")
    other = app.store.references(app.library.model_id, "B")[0]
    ids = {"valid": str(refs[-1].id), "other-person": str(other.id)}
    response = client(app).post("/employees/A/photos/delete",
                                data={"ref_id": [ids.get(value, value) for value in selection]})
    assert response.status_code == 400
    assert len(app.store.references(app.library.model_id, "A")) == len(refs)
    assert (app.data_dir / refs[-1].image).exists()


def test_delete_photos_below_score_defaults_to_twenty_and_uses_displayed_points(core):
    from test_review_controls import ReviewControls

    app, _, _ = core
    refs = []
    for score in (0, 0.19, 0.199, 0.20, 0.8, None):
        app.library.add_photo("A", face_image(1))
        ref = app.store.references(app.library.model_id, "A")[-1]
        app.store.set_reference_quality(ref.image, True, score)
        refs.append(ref)
    app.store.add_embedding("other-model", "A", person_vector(1), "upload", at(8, 0), refs[0].image)
    app.library.add_photo("B", face_image(2))
    other = app.store.references(app.library.model_id, "B")[-1]
    app.store.set_reference_quality(other.image, False, 0)

    c = client(app)
    controls = ReviewControls(c.get("/employees/A").get_data(as_text=True))
    low = next(f for f in controls.forms if f.get("id") == "delete-low-score-photos")
    assert low["fields"] == {"below_score": "20"} and low.get("data-confirm")
    response = c.post(low["action"], data=low["fields"], follow_redirects=True)
    assert response.status_code == 200 and "2 photos deleted" in response.get_data(as_text=True)
    remaining = {r.id for r in app.store.references(app.library.model_id, "A")}
    assert not remaining & {r.id for r in refs[:2]}
    assert {r.id for r in refs[2:]} <= remaining  # displayed 20/100 and unscored photos stay
    assert not app.store.references("other-model", "A")
    assert all(not (app.data_dir / r.image).exists() for r in refs[:2])
    assert (app.data_dir / other.image).exists()
    assert app.gallery.counts()["A"] == len(remaining)


@pytest.mark.parametrize("threshold", ["", "-1", "101", "20.5", "nan", "inf", "invalid"])
def test_delete_below_score_rejects_invalid_threshold_without_deleting(core, threshold):
    app, _, _ = core
    app.library.add_photo("A", face_image(1))
    ref = app.store.references(app.library.model_id, "A")[-1]
    app.store.set_reference_quality(ref.image, False, 0)
    response = client(app).post("/employees/A/photos/delete", data={"below_score": threshold})
    assert response.status_code == 400
    assert (app.data_dir / ref.image).exists()
    assert any(r.id == ref.id for r in app.store.references(app.library.model_id, "A"))


@pytest.mark.parametrize("threshold,score", [("0", 0), ("100", 1)])
def test_delete_below_score_keeps_equal_scores_and_reports_no_matches(core, threshold, score):
    app, _, _ = core
    app.library.add_photo("A", face_image(1))
    ref = app.store.references(app.library.model_id, "A")[-1]
    app.store.set_reference_quality(ref.image, True, score)
    response = client(app).post("/employees/A/photos/delete", data={"below_score": threshold}, follow_redirects=True)
    assert response.status_code == 200
    assert "No photos below" in response.get_data(as_text=True)
    assert (app.data_dir / ref.image).exists()


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
        db.execute("ALTER TABLE embeddings DROP COLUMN quality_score")
    store = Store(path)
    store.add_embedding("m", "A", person_vector(1), "upload", at(8, 0), "faces/A/x.jpg", True)
    assert store.references("m")[0].quality_ok is True
    assert store.references("m")[0].quality_score is None


def test_employee_page_lists_look_alikes(core):
    app, _, _ = core
    dan = add_person(app, "dan@example.com", "Dan")
    app.library.add_photo(dan, face_image(1))  # looks like Alice
    c = client(app)
    c.post('/employees/quality')
    page = c.get(f"/employees/{dan}").get_data(as_text=True)
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
