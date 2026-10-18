import datetime as dt
import hashlib
import io
import json
import zipfile

import numpy as np
from conftest import DAY, at, face_image, person_vector
from test_web import client

from attendance.evidence import build, evidence_digest, zip_bytes
from attendance.pipeline import save_crop
from attendance.store import body_photo, scene_photo
from attendance.tracker import Decision


def seen(app, who, ts, score=0.9, kind="identified", frame=None):
    """What the camera loop does for one decision: save the pictures, hash them, record it."""
    frame = np.full((480, 640, 3), 90, np.uint8) if frame is None else frame
    d = Decision(kind, ts, who, score, face_image(1, 80), person_vector(1), body=face_image(1, 120),
                 scene=(frame, (300.0, 100.0, 380.0, 200.0)))
    image = save_crop(app, d, ts)
    digest = evidence_digest(app.data_dir, image)
    if kind == "identified":
        app.service.on_identified(who, ts, score, image, person_vector(1), digest)
    else:
        app.service.on_unknown(ts, image, person_vector(1), who, score, digest)
    return image, digest


def test_scene_picture_has_the_time_burned_in_and_is_hashed(core):
    import cv2

    app, clock, _ = core
    frame = np.full((720, 1600, 3), 90, np.uint8)  # wider than the evidence picture: scaled down
    image, digest = seen(app, "A", at(9, 5), frame=frame)
    scene = cv2.imread(str(app.data_dir / scene_photo(image)))
    assert scene.shape[1] == 1280
    assert scene[2:6, 2:6].max() < 10  # the black label strip, top left
    assert scene[20:40, 300:600].max() > 200  # white text inside it
    assert scene[157:165, 272, 2].max() > 200  # the box around the face (scaled by 0.8)
    assert (app.data_dir / body_photo(image)).exists()

    files = (app.data_dir / image).read_bytes() + (app.data_dir / scene_photo(image)).read_bytes()
    assert digest == hashlib.sha256(files).hexdigest()
    [s] = app.store.sightings(DAY, "A")
    assert s.sha256 == digest


def test_evidence_page_and_zip_tell_the_whole_story(core):
    app, clock, sent = core
    c = client(app)
    clock.t = at(10, 30)
    seen(app, "A", at(9, 20), 0.55)   # the camera said Alice, but it was Bob
    seen(app, "A", at(10, 15), 0.92)  # Alice herself, late
    first = app.store.sightings(DAY, "A")[0]
    app.service.move_sighting(first.id, "B", user="hr")

    ev = build(app.service, DAY, "A")
    assert ev.status == "Late" and ev.minutes_late == 15 and ev.arrival_time == "10:15:00"
    assert [it.time for it in ev.items] == ["10:15:00"]
    assert ev.items[0].intact is True and set(ev.items[0].files) == {"scene", "face", "body"}
    assert [(who, action) for _, who, action, _ in ev.corrections] == [("hr", "move sighting")]
    assert "Alice -> Bob" in ev.corrections[0][3]

    bob = build(app.service, DAY, "B")
    assert bob.status == "On time" and bob.items[0].camera_said == "A"

    page = c.get(f"/day/{DAY}/A/evidence").get_data(as_text=True)
    assert "Late" in page and "15 min late" in page and "files unchanged since capture" in page
    assert "move sighting" in page and "_scene.jpg" in page
    assert c.get(f"/day/{DAY}/ZZ/evidence").status_code == 404

    resp = c.get(f"/day/{DAY}/A/evidence.zip")
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    names = z.namelist()
    assert "summary.txt" in names and "summary.json" in names and sum(n.startswith("photos/") for n in names) == 3
    summary = json.loads(z.read("summary.json"))
    assert summary["status"] == "Late" and summary["minutes_late"] == 15
    assert summary["sightings"][0]["integrity"] == "files unchanged since capture"
    assert set(summary["sightings"][0]["sha256_now"]) == {"scene", "face", "body"}
    assert summary["employee"] == {"email": "alice@example.com", "name": "Alice"}
    assert "Alice (alice@example.com)" in z.read("summary.txt").decode()
    assert "export evidence" in c.get("/audit").get_data(as_text=True)

    # Someone edits a picture afterwards: the page says so.
    (app.data_dir / scene_photo(ev.items[0].sighting.image)).write_bytes(b"edited")
    assert build(app.service, DAY, "A").items[0].intact is False
    assert "differ from the hash" in c.get(f"/day/{DAY}/A/evidence").get_data(as_text=True)


def test_statuses_without_an_arrival(core):
    app, clock, _ = core
    clock.t = at(9, 0)
    app.service.tick(True)  # the camera worked that morning
    clock.t = at(10, 30)
    assert build(app.service, DAY, "C").status == "Not seen"
    app.store.replace_external(DAY, DAY, [], [("B", DAY, DAY, "sick", "")])
    assert build(app.service, DAY, "B").status == "On leave (Sick)"
    sunday = DAY + dt.timedelta(days=4)
    assert build(app.service, sunday, "A").status == "Not a work day"


def test_pictures_of_late_days_are_kept_longer(core):
    app, clock, _ = core
    old = DAY - dt.timedelta(days=100)      # a Monday, past retention_days (90) but within late_evidence_days
    ancient = DAY - dt.timedelta(days=400)  # past both
    assert old.weekday() == 0
    on_time, _ = seen(app, "A", at(9, 0, day=old))
    late_old, _ = seen(app, "B", at(10, 20, day=old))
    late_ancient, _ = seen(app, "C", at(10, 30, day=ancient))
    # A face named on the review page: the unknown row and the sighting share the same files.
    visitor, _ = seen(app, None, at(10, 40, day=old), 0.3, kind="unknown")
    uid = app.store.unknowns(old)[0].id
    app.service.resolve_unknown(uid, "C")

    clock.t = at(8, 0)
    app.service.tick()  # runs the daily cleanup
    exists = lambda rel: (app.data_dir / rel).exists()  # noqa: E731
    assert not exists(on_time) and not exists(scene_photo(on_time))
    assert exists(late_old) and exists(scene_photo(late_old)) and exists(body_photo(late_old))
    assert exists(visitor) and exists(scene_photo(visitor))  # Carol was late that day: kept
    assert not exists(late_ancient)
    arrivals = app.store.checkins(old)
    assert arrivals["A"].image is None and arrivals["B"].image == late_old and arrivals["C"].image == visitor
    assert app.store.unknowns(old, status=None) == []
    assert app.store.checkins(ancient)["C"].image is None
