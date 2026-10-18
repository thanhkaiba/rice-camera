import sqlite3

import pytest

from attendance.store import Store
from conftest import DAY, at, person_vector
from test_web import client


def test_email_is_unique_normalized_and_names_can_repeat(tmp_path):
    store = Store(tmp_path / "db")
    first = store.add_employee(" Linh+Office@Example.COM ", "Linh")
    second = store.add_employee("other@example.com", "Linh")
    assert first != second
    assert store.employee(first).email == "linh+office@example.com"
    assert store.employee_by_email(" LINH+OFFICE@example.com ").id == first
    with pytest.raises(ValueError, match="already"):
        store.add_employee("linh+office@example.com", "Another name")
    store.update_employee(first, "Linh Nguyen", True)
    assert store.employee_by_email("linh+office@example.com").name == "Linh Nguyen"


@pytest.mark.parametrize("email", ["", "NV001", "a@@example.com", "../a@example.com", "a@", "a\n@example.com"])
def test_invalid_email_cannot_create_an_employee(tmp_path, email):
    store = Store(tmp_path / "db")
    with pytest.raises(ValueError, match="email"):
        store.add_employee(email, "Linh")
    assert store.employee_count() == 0


def test_csv_connects_a_legacy_employee_without_changing_face_or_attendance_links(tmp_path):
    with sqlite3.connect(tmp_path / "db") as old:
        old.execute("CREATE TABLE employees (id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
    store = Store(tmp_path / "db")
    store._exec("INSERT INTO employees (id,name) VALUES (?,?)", ("NV001", "Linh"))
    store.add_embedding("model", "NV001", person_vector(1), "upload", at(8, 0), "faces/NV001/ref.jpg")
    store.record_checkin(DAY, "NV001", at(9, 0), "camera", .9, "crops/old.jpg")
    assert store.import_employees("email,name\nlinh@example.com,Linh\n") == 1
    assert store.employee_by_email("linh@example.com").id == "NV001"
    store.import_employees("email,name\nLINH@example.com,Linh Nguyen\n")
    assert store.employee_count() == 1
    assert store.employee("NV001").name == "Linh Nguyen"
    assert store.references("model")[0].image == "faces/NV001/ref.jpg"
    assert "NV001" in store.checkins(DAY)
    assert store.sightings(DAY)[0].camera_id == "NV001"
    store.close()
    reopened = Store(tmp_path / "db")
    assert reopened.employee_by_email("linh@example.com").id == "NV001"


def test_import_does_not_merge_people_just_because_their_names_match(tmp_path):
    store = Store(tmp_path / "db")
    store.import_employees("email,name\na@example.com,Linh\nb@example.com,Linh\n")
    assert store.employee_count() == 2
    a = store.employee_by_email("a@example.com")
    store.import_employees("email,name\na@example.com,Linh Nguyen\n")
    assert store.employee_by_email("a@example.com").id == a.id
    assert store.employee_by_email("b@example.com").name == "Linh"


def test_duplicate_email_edit_rolls_back_other_fields(tmp_path):
    store = Store(tmp_path / "db")
    a = store.add_employee("a@example.com", "A")
    store.add_employee("b@example.com", "B")
    with pytest.raises(ValueError, match="already"):
        store.update_employee(a, "Changed", False, email="B@example.com")
    assert store.employee(a).name == "A" and store.employee(a).active
    assert store.employee(a).email == "a@example.com"


def test_enroll_uses_email_folders_and_keeps_reference_links(core, tmp_path, monkeypatch):
    from attendance.cli.enroll import enroll
    from attendance.core import pipeline
    from attendance.vision.faces import encode_jpeg
    from conftest import face_image

    app, _, _ = core
    folder = tmp_path / "faces" / "ALICE@example.com"
    folder.mkdir(parents=True)
    (folder / "ref.jpg").write_bytes(encode_jpeg(face_image(1)))
    monkeypatch.setattr(pipeline, "App", lambda cfg: app)
    enroll(app.cfg, folder.parent)
    assert any(ref.source == "enroll" for ref in app.store.references(app.library.model_id, "A"))


def test_ambiguous_legacy_names_are_not_guessed(tmp_path):
    store = Store(tmp_path / "db")
    store._exec("INSERT INTO employees (id,name) VALUES (?,?)", ("OLD", "Linh"))
    with pytest.raises(ValueError, match="ambiguous"):
        store.import_employees("email,name\na@example.com,Linh\nb@example.com,Linh\n")
    assert store.employee_count() == 1
    assert not store.employee("OLD").email


@pytest.mark.parametrize("rows", ["good@example.com,Good\nbad,Bad\n", "a@example.com,A\nA@example.com,B\n"])
def test_csv_validation_is_atomic(tmp_path, rows):
    store = Store(tmp_path / "db")
    with pytest.raises(ValueError):
        store.import_employees("email,name\n" + rows)
    assert store.employee_count() == 0


def test_people_forms_use_email_and_changing_email_keeps_the_employee(core):
    app, _, _ = core
    c = client(app)
    response = c.post("/employees", data={"email": " LINH@Example.com ", "name": "Linh"})
    person = app.store.employee_by_email("linh@example.com")
    assert response.status_code == 302 and person is not None
    app.store.add_embedding(app.library.model_id, person.id, person_vector(4), "upload", at(8, 0))
    app.store.record_checkin(DAY, person.id, at(9, 0), "camera", .9, None)
    c.post(f"/employees/{person.id}", data={"email": "new@example.com", "name": "Linh Nguyen", "active": "on"})
    assert app.store.employee_by_email("new@example.com").id == person.id
    assert app.store.references(app.library.model_id, person.id)
    assert person.id in app.store.checkins(DAY)
    page = c.get("/employees").get_data(as_text=True)
    assert "new@example.com" in page and 'name="email"' in page and 'name="id"' not in page


def test_old_csv_does_not_prevent_assigning_emails_in_the_web_ui(tmp_path):
    from attendance.config import Config
    from attendance.pipeline import App

    (tmp_path / "employees.csv").write_text("id,name\nOLD,Linh\n")
    seed = Store(tmp_path / "data" / "attendance.db")
    seed._exec("INSERT INTO employees (id,name) VALUES ('OLD','Linh')")
    seed.close()
    app = App(Config(base_dir=tmp_path))
    assert app.store.employee("OLD").name == "Linh"
    assert client(app).get("/employees/OLD").status_code == 200


@pytest.mark.parametrize("manual_import", [False, True])
def test_csv_migration_does_not_repeat_when_a_legacy_person_has_no_email(tmp_path, manual_import):
    import io

    from attendance.config import Config
    from attendance.pipeline import App

    roster = "email,name\nlinh@example.com,Linh\n"
    seed = Store(tmp_path / "data" / "attendance.db")
    seed._exec("INSERT INTO employees (id,name) VALUES ('OLD','Linh')")
    seed._exec("INSERT INTO employees (id,name) VALUES ('UNMATCHED','Other')")
    seed.close()
    cfg = Config(base_dir=tmp_path)
    if manual_import:
        app = App(cfg)
        assert client(app).post("/employees/import", data={"file": (io.BytesIO(roster.encode()), "people.csv")}).status_code == 302
        (tmp_path / "employees.csv").write_text(roster)
    else:
        (tmp_path / "employees.csv").write_text(roster)
        app = App(cfg)
    app.store.update_employee("OLD", "Linh Edited", False)
    app.store.close()
    restarted = App(cfg)
    assert restarted.store.employee("OLD").name == "Linh Edited"
    assert not restarted.store.employee("OLD").active
    assert not restarted.store.employee("UNMATCHED").email


def test_today_distinguishes_duplicate_names_by_email(core):
    app, _, _ = core
    app.store.update_employee("B", "Alice", True)
    page = client(app).get("/").get_data(as_text=True)
    assert 'class="cell-sub">alice@example.com</div>' in page
    assert 'class="cell-sub">bob@example.com</div>' in page
    assert '>Alice (alice@example.com)</option>' in page
    assert '>Alice (bob@example.com)</option>' in page
