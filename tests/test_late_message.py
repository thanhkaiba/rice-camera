import datetime as dt
import io

import numpy as np
import pytest

from attendance import config
from attendance.web import create_app
from conftest import DAY, at
from test_service import run_until

D = dt.date(2026, 10, 15)


def late(app, emp, hh, mm, day=DAY):
    app.service.on_identified(emp, at(hh, mm, day=day), 0.8, None)


def test_message_mentions_each_late_person(core):
    app, clock, _ = core
    app.store.update_employee("B", "Bob", True, "111222333")
    clock.t = at(10, 40)
    late(app, "A", 9, 0)  # on time: not listed
    late(app, "B", 10, 15)
    assert app.service.late_text(DAY) == "Morning, coming late today:\nLate arrivals (1):\n• <users/111222333> (10:15)\n\nNot seen (1):\n• @Carol"
    late(app, "C", 10, 32)
    assert app.service.late_text(DAY) == \
        "Morning, coming late today:\nLate arrivals (2):\n• <users/111222333> (10:15)\n• @Carol (10:32)"  # Carol has no Chat ID
    assert app.service.late_text(DAY, preview=True) == "Morning, coming late today:\nLate arrivals (2):\n• @Bob (10:15)\n• @Carol (10:32)"

    app.cfg.late_message.intro = "Good morning! Late today:"
    app.cfg.late_message.item = "{mention} {minutes} min"
    assert app.service.late_text(DAY) == "Good morning! Late today:\nLate arrivals (2):\n• <users/111222333> 15 min\n• @Carol 32 min"


def test_late_message_without_intro_has_no_empty_header(core):
    app, clock, _ = core
    app.cfg.late_message.intro = ''
    app.cfg.late_message.include_not_seen = False
    clock.t = at(10, 40)
    late(app, 'B', 10, 15)
    assert app.service.late_text(DAY) == 'Late arrivals (1):\n• @Bob (10:15)'


def test_sent_once_at_the_set_time_as_its_own_message(core):
    app, clock, _ = core
    payloads = []
    app.chat._post = lambda url, payload: payloads.append(payload)
    run_until(app.service, clock, at(10, 20))
    late(app, "B", 10, 15)
    run_until(app.service, clock, at(10, 59))
    assert not any(p["text"].startswith("Morning") for p in payloads)
    run_until(app.service, clock, at(11, 30))
    morning = [p for p in payloads if p["text"].startswith("Morning")]
    assert len(morning) == 1 and "thread" not in morning[0]  # a message of its own, not in the day's thread
    assert app.store.notice_time(DAY, "late_message") is not None


def test_on_leave_arrival_is_excluded_but_not_seen_are_sent(core):
    app, clock, sent = core
    app.store.replace_external(DAY, DAY, [], [("B", DAY, DAY, "leave", "")])
    clock.t = at(10, 30)
    late(app, "B", 10, 20)  # on leave but came in late: not listed
    run_until(app.service, clock, at(11, 30))
    assert not any(m.startswith("Morning") for m in sent)
    assert sent.count("Not seen (2):\n• @Alice\n• @Carol") == 1


def test_disabled_or_manual_only(core):
    app, clock, sent = core
    app.cfg.late_message.auto_send = False
    clock.t = at(10, 30)
    late(app, "B", 10, 20)
    run_until(app.service, clock, at(11, 30))
    assert not any(m.startswith("Morning") for m in sent)
    assert app.service.send_late_message(DAY, user="hr")
    assert sent[-1].startswith("Morning, coming late today:\nLate arrivals (1):\n• @Bob")


def test_alerts_mention_people_unless_switched_off(core):
    app, clock, sent = core
    app.store.update_employee("B", "Bob", True, "999")
    app.cfg.google_chat.mention_people = False
    clock.t = at(10, 30)
    late(app, "B", 10, 20)
    assert sent[-1] == "🟠 *Bob* arrived at 10:20, 20 min late."
    app.cfg.google_chat.mention_people = True
    late(app, "C", 10, 25)
    assert sent[-1] == "🟠 @Carol arrived at 10:25, 25 min late."


def test_config_checks_send_time_and_placeholders(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('schedule:\n  late_after: "12:30"\n  window_end: "13:00"\n')
    assert config.load(p).late_message.send_at == ""  # default follows the camera hours, any late time works
    p.write_text('late_message:\n  send_at: "09:30"\n')
    with pytest.raises(ValueError, match="late time"):
        config.load(p)
    p.write_text('late_message:\n  item: "{who}"\n')
    with pytest.raises(ValueError, match="mention"):
        config.load(p)


def test_chat_ids_from_csv_and_employee_page(core):
    app, clock, _ = core
    app.store.import_employees("email,name,chat_user\nalice@example.com,Alice,123\ndan@example.com,Dan,\n")
    assert app.store.employee("A").chat_user == "123" and app.store.employee_by_email("dan@example.com").chat_user == ""
    app.store.import_employees("email,name\nalice@example.com,Alice Nguyen\n")
    assert app.store.employee("A").chat_user == "123"  # not wiped by a CSV without the column

    flask_app = create_app(app)
    flask_app.testing = True
    c = flask_app.test_client()
    c.post("/employees/B", data={"name": "Bob", "active": "on", "chat_user": " 456 "})
    assert app.store.employee("B").chat_user == "456"
    urls = []
    app.chat._post = lambda url, payload: urls.append(payload["text"])
    page = c.post("/employees/B/test-mention", follow_redirects=True).get_data(as_text=True)
    assert "Test sent" in page and urls == ["Test mention from the attendance system: <users/456>"]

    clock.t = at(10, 40)
    late(app, "B", 10, 15)
    page = c.get("/").get_data(as_text=True)
    assert "Morning, coming late today:\nLate arrivals (1):\n• @Bob (10:15)" in page
    page = c.post(f"/day/{DAY}/late-message", follow_redirects=True).get_data(as_text=True)
    assert "Late message posted" in page and urls[-1] == "Morning, coming late today:\nLate arrivals (1):\n• <users/456> (10:15)\n\nNot seen (3):\n• <users/123>\n• @Carol\n• @Dan"


def test_send_time_must_be_after_the_late_time(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('late_message:\n  send_at: "10:00"\n')  # equal to late_after: nobody is late yet at that moment
    with pytest.raises(ValueError, match="after the late time"):
        config.load(p)
    p.write_text('late_message:\n  send_at: "10:01"\n')
    assert config.load(p).late_message.send_at == "10:01"


def test_placeholders_cannot_reach_into_objects(tmp_path):
    for item in ("{mention.__class__}", "{mention.foo}", "{}", "{0}", "{name[0]}", "{mention"):
        with pytest.raises(ValueError, match="mention"):
            config.check_late_message_item(item)
    config.check_late_message_item("{mention} ({time}, {minutes:02d} min, {name})")


def test_message_about_another_day_starts_with_its_date(core):
    app, clock, sent = core
    clock.t = at(10, 40)
    late(app, "B", 10, 15)
    clock.t = at(9, 0, day=D)  # the next day
    assert app.service.late_text(DAY) == "Wednesday 14 Oct: Morning, coming late today:\nLate arrivals (1):\n• @Bob (10:15)\n\nNot seen (2):\n• @Alice\n• @Carol"
    assert app.service.send_late_message(DAY, user="hr")
    assert sent[-1].startswith("Wednesday 14 Oct: ")


def test_nobody_late_or_not_seen_is_remembered_but_not_shown_as_sent(core):
    app, clock, sent = core
    for emp in "ABC":
        late(app, emp, 9, 0)
    run_until(app.service, clock, at(11, 30))
    assert app.store.notice_time(DAY, "late_message") is None
    assert app.store.notice_time(DAY, "late_message_skipped") is not None
    flask_app = create_app(app)
    flask_app.testing = True
    page = flask_app.test_client().get("/").get_data(as_text=True)
    assert "Nobody was late or not seen at 11:00, so nothing was sent" in page and "Sent at" not in page
    # The admin later finds someone the camera missed and can still send the message by hand.
    app.service.set_arrival(DAY, "B", dt.time(10, 20))
    assert app.service.send_late_message(DAY, user="hr") and sent[-1].startswith("Morning")
    run_until(app.service, clock, at(12, 0))
    assert len([m for m in sent if m.startswith("Morning")]) == 1


def test_automatic_send_does_not_repeat_a_manual_one(core):
    app, clock, sent = core
    clock.t = at(10, 30)
    late(app, "B", 10, 20)
    assert app.service.send_late_message(DAY, user="hr")
    run_until(app.service, clock, at(11, 30))
    assert len([m for m in sent if m.startswith("Morning")]) == 1
    assert app.store.mark_notice(DAY, "late_message", clock.t) is False  # already marked


def test_chat_ids_are_normalised_and_checked(core):
    app, _, _ = core
    app.store.update_employee("A", "Alice", True, " users/123 ")
    assert app.store.employee("A").chat_user == "123"
    app.store.update_employee("A", "Alice", True, "<users/456>")
    assert app.store.employee("A").chat_user == "456"
    with pytest.raises(ValueError, match="number"):
        app.store.update_employee("A", "Alice", True, "alice@example.com")
    assert app.store.employee("A").chat_user == "456"  # nothing changed
    with pytest.raises(ValueError, match="row 2"):
        app.store.import_employees("email,name,chat_user\nbob@example.com,Bob,bob\n")
    app.store.import_employees("email,name,chat_user\nbob@example.com,Bob,users/789\n")
    assert app.store.employee("B").chat_user == "789"

    flask_app = create_app(app)
    flask_app.testing = True
    page = flask_app.test_client().post("/employees/C", data={"name": "Carol", "active": "on", "chat_user": "x"},
                                        follow_redirects=True).get_data(as_text=True)
    assert "is a number" in page


def test_picture_of_the_late_people(core):
    import cv2
    import numpy as np
    from test_evidence import seen

    from attendance.core import latephoto

    app, clock, _ = core
    payloads = []
    app.chat._post = lambda url, payload: payloads.append(payload)
    clock.t = at(10, 40)
    seen(app, "A", at(9, 0))  # on time: not in the picture
    late(app, "C", 10, 32)  # entered by hand: no photo
    assert app.service.late_photo(DAY) is None  # nobody on camera: no picture
    seen(app, "B", at(10, 15))  # camera: the whole person was kept
    rel = app.service.late_photo(DAY)
    assert rel == f"late/{DAY}.jpg"
    picture = cv2.imread(str(app.data_dir / rel))
    assert picture.shape == (latephoto.HEIGHT, latephoto.MIN_WIDTH, 3)
    behind = latephoto.day_background(app.data_dir, DAY)  # nothing uploaded: the day's bundled old picture
    assert behind is not None and behind.parent == latephoto.BUNDLED
    scene = latephoto.load_background(behind, latephoto.MIN_WIDTH, latephoto.HEIGHT)
    sky = lambda p: np.abs(p[30, 1000].astype(int) - scene[30, 1000]).max() <= 12  # noqa: E731  (JPEG is not exact)
    assert sky(picture)
    key = (app.data_dir / rel).with_suffix(".key").read_text()

    # Unchanged arrivals: the same file. A correction, or a new background: the picture is made again.
    assert app.service.late_photo(DAY) == rel and (app.data_dir / rel).with_suffix(".key").read_text() == key
    app.service.delete_arrival(DAY, "C")
    assert app.service.late_photo(DAY) == rel
    key2 = (app.data_dir / rel).with_suffix(".key").read_text()
    assert key2 != key
    bg = latephoto.background_path(app.data_dir)
    bg.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(bg), np.full((90, 160, 3), (10, 20, 30), np.uint8))
    assert app.service.late_photo(DAY) == rel
    assert (app.data_dir / rel).with_suffix(".key").read_text() != key2
    picture = cv2.imread(str(app.data_dir / rel))
    uploaded = latephoto.load_background(bg, picture.shape[1], picture.shape[0])
    assert not sky(picture) and np.abs(picture[30, 1000].astype(int) - uploaded[30, 1000]).max() <= 6

    # A plain http address (the office network): Google Chat cannot fetch it, so the message links to it.
    assert app.service.send_late_message(DAY, user="hr")
    p = payloads[-1]
    assert "cardsV2" not in p
    assert p["text"].startswith("Morning, coming late today:\nLate arrivals (1):\n• @Bob (10:15)\n\nNot seen (1):\n• @Carol\n📷 <http://pc:8000/late-photo/2026-10-14/")
    # An https address: the picture is shown in the message, under the text.
    app.cfg.web.public_url = "https://office.example.ts.net"
    assert app.service.send_late_message(DAY, user="hr")
    p = payloads[-1]
    assert p["text"] == "Morning, coming late today:\nLate arrivals (1):\n• @Bob (10:15)\n\nNot seen (1):\n• @Carol"
    image = p["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["image"]
    assert image["imageUrl"].startswith(f"https://office.example.ts.net/late-photo/{DAY}/")
    # Switched off: text only, and no picture is made.
    app.cfg.late_message.photo = False
    assert app.service.late_photo(DAY) is None and app.service.late_photo_url(DAY) is None
    assert app.service.send_late_message(DAY, user="hr")
    assert "cardsV2" not in payloads[-1] and "late-photo" not in payloads[-1]["text"]


def test_picture_is_served_by_its_token_without_the_password(core):
    from test_evidence import seen
    from test_web import client

    app, clock, _ = core
    clock.t = at(10, 40)
    seen(app, "B", at(10, 15))
    url = app.service.late_photo_url(DAY)
    assert url.startswith("http://pc:8000/late-photo/")
    path = url[len("http://pc:8000"):]
    c = client(app, password="s3cret")
    assert c.get("/").status_code == 401
    resp = c.get(path)
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg" and resp.data[:2] == b"\xff\xd8"
    token = app.service.late_photo_token(DAY)
    assert c.get(f"/late-photo/{DAY}/{'0' * 32}.jpg").status_code == 404  # wrong token
    assert c.get(f"/late-photo/{D}/{app.service.late_photo_token(D)}.jpg").status_code == 404  # nobody late that day
    assert c.get(f"/late-photo/not-a-day/{token}.jpg").status_code == 404
    # The Today page shows the picture and says it will only be linked from an http address.
    page = c.get("/", headers={"Authorization": "Basic YWRtaW46czNjcmV0"}).get_data(as_text=True)
    assert f'<img src="{url}"' in page and "only link to this picture" in page


def test_pictures_of_late_people_follow_the_evidence_retention(core):
    from test_evidence import seen

    from attendance.core import latephoto

    app, clock, _ = core
    old = DAY - dt.timedelta(days=100)  # within late_evidence_days
    ancient = DAY - dt.timedelta(days=400)  # past it
    seen(app, "B", at(10, 20, day=old))
    seen(app, "C", at(10, 30, day=ancient))
    assert app.service.late_photo(old) and app.service.late_photo(ancient)
    clock.t = at(8, 0)
    app.service.tick()  # runs the daily cleanup
    assert (app.data_dir / latephoto.photo_path(old)).exists()
    assert not (app.data_dir / latephoto.photo_path(ancient)).exists()
    assert not (app.data_dir / latephoto.photo_path(ancient)).with_suffix(".key").exists()


def test_labels_are_drawn_without_accents():
    from attendance.core.latephoto import ascii_label

    assert ascii_label("Nguyễn Văn Đức") == "Nguyen Van Duc"
    assert ascii_label("Trần Thị Hồng đào") == "Tran Thi Hong dao"
    assert ascii_label("日本") == "?"


def test_own_pictures_are_uploaded_for_the_rotation_and_for_an_occasion(core):
    from test_web import client, png

    import numpy as np

    from attendance.core import latephoto, occasions

    app, _, _ = core
    c = client(app)
    pic = lambda shade: (io.BytesIO(png(np.full((90, 160, 3), shade, np.uint8))), f"p{shade}.png")
    resp = c.post("/settings/late-background", data={"for": "rotation", "background": [pic(10), pic(20)]},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "2 picture(s) saved for the days in turn" in resp.get_data(as_text=True)
    resp = c.post("/settings/late-background", data={"for": "tet", "background": [pic(30)]},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "1 picture(s) saved for Tết" in resp.get_data(as_text=True)
    assert latephoto.own_pictures(app.data_dir) == [("", "own-1.jpg"), ("", "own-2.jpg"), ("tet", "tet/tet-1.jpg")]
    # An ordinary day takes one of the rotation pictures, Tết its own.
    assert latephoto.day_background(app.data_dir, DAY).name.startswith("own-")
    tet = occasions.BY_KEY["tet"].on(DAY.year)
    assert latephoto.day_background(app.data_dir, tet).name == "tet-1.jpg"
    page = c.get("/settings").get_data(as_text=True)
    assert "Around Tết" in page and 'value="tet/tet-1.jpg"' in page and "Empty = old paintings" not in page
    assert c.get("/settings/late-backgrounds/tet/tet-1.jpg").status_code == 200
    assert c.get("/settings/late-backgrounds/../2026-10-22.jpg").status_code == 404
    resp = c.post("/settings/late-background", data={"remove": "own-1.jpg"}, follow_redirects=True)
    assert "Picture removed" in resp.get_data(as_text=True)
    assert [rel for _, rel in latephoto.own_pictures(app.data_dir)] == ["own-2.jpg", "tet/tet-1.jpg"]
    resp = c.post("/settings/late-background", data={"remove": "../../attendance.db"}, follow_redirects=True)
    assert "not there any more" in resp.get_data(as_text=True)
    resp = c.post("/settings/late-background", data={"for": "nonsense", "background": [pic(40)]},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "Choose what the pictures are for" in resp.get_data(as_text=True)


def test_background_picture_is_uploaded_and_removed_on_the_settings_page(core):
    from test_web import client, png

    import numpy as np

    from attendance.core import latephoto

    app, _, _ = core
    c = client(app)
    page = c.get("/settings").get_data(as_text=True)
    assert "Automatic changes it each day" in page and 'form="late-bg"' in page
    assert c.get("/settings/late-background.jpg").status_code == 404
    resp = c.post("/settings/late-background", data={"background": (io.BytesIO(b"not a picture"), "x.png")},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "not a readable image" in resp.get_data(as_text=True)
    picture = png(np.full((90, 160, 3), (10, 20, 30), np.uint8))
    resp = c.post("/settings/late-background", data={"background": (io.BytesIO(picture), "office.png")},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "Background saved" in resp.get_data(as_text=True)
    assert latephoto.background_path(app.data_dir).exists()
    assert c.get("/settings/late-background.jpg").mimetype == "image/jpeg"
    assert "Your picture is used" in c.get("/settings").get_data(as_text=True)
    resp = c.post("/settings/late-background", data={"remove": "1"}, follow_redirects=True)
    assert "Picture removed" in resp.get_data(as_text=True)
    assert not latephoto.background_path(app.data_dir).exists()


def test_cards_in_rows_on_a_scene_that_changes_by_day():
    import cv2
    import numpy as np

    from attendance.core import latephoto

    # Up to six in a row; more go on further rows, as evenly as possible.
    assert latephoto.layout(1) == (1, 1) and latephoto.layout(6) == (1, 6)
    assert latephoto.layout(7) == (2, 4) and latephoto.layout(13) == (3, 5)
    # Each person is the whole photo as a card: nothing of it is cut away.
    card, alpha = latephoto._card(latephoto.Person("Enzo", "09:31", 31, None))  # no photo: a placeholder
    assert card.shape == (latephoto.CARD_H, latephoto.CARD_W, 3) and alpha[latephoto.CARD_H // 2, latephoto.CARD_W // 2] == 1
    # The drawn background differs from day to day and fills the canvas; a missing picture is drawn.
    a, b = latephoto.draw_background(1280, 720, seed=1), latephoto.draw_background(1280, 720, seed=2)
    assert a.shape == (720, 1280, 3) and not np.array_equal(a, b)
    assert len({latephoto.draw_background(1280, 720, seed=d)[5, 5].tobytes() for d in range(5)}) == 5  # five scenes
    assert latephoto.load_background(latephoto.Path("/nowhere.jpg"), 640, 360).shape == (360, 640, 3)
    # Thirteen people: three rows, a taller picture, and every name tag keeps its room.
    people = [latephoto.Person(f"Person{i}", "09:10", 10, None) for i in range(13)]
    picture = cv2.imdecode(np.frombuffer(latephoto.compose(people, "Late"), np.uint8), cv2.IMREAD_COLOR)
    assert picture.shape[0] > latephoto.HEIGHT and picture.shape[1] == latephoto.MIN_WIDTH


def test_people_can_be_called_by_their_email_name(core):
    from attendance.core.service import email_name

    assert email_name("ruby@offspringdigital.com") == "Ruby"
    assert email_name("tan.nguyen@offspringdigital.com") == "Tan Nguyen"
    app, clock, sent = core
    app.store._db.execute("UPDATE employees SET name = 'Lương Thế Hùng', email = 'enzo@example.com' WHERE id = 'B'")
    app.store._db.commit()
    clock.t = at(10, 40)
    late(app, "B", 10, 15)
    late(app, "C", 10, 32)
    assert app.service.late_text(DAY) == "Morning, coming late today:\nLate arrivals (2):\n• @Enzo (10:15)\n• @Carol (10:32)\n\nNot seen (1):\n• @Alice"  # the default
    app.cfg.google_chat.names = "name"
    assert app.service.late_text(DAY) == "Morning, coming late today:\nLate arrivals (2):\n• @Lương Thế Hùng (10:15)\n• @Carol (10:32)\n\nNot seen (1):\n• @Alice"
    app.cfg.google_chat.names = "email"
    assert app.service.late_text(DAY) == "Morning, coming late today:\nLate arrivals (2):\n• @Enzo (10:15)\n• @Carol (10:32)\n\nNot seen (1):\n• @Alice"
    assert app.service.late_text(DAY, preview=True) == "Morning, coming late today:\nLate arrivals (2):\n• @Enzo (10:15)\n• @Carol (10:32)\n\nNot seen (1):\n• @Alice"
    app.cfg.google_chat.mention_people = False
    assert app.service.who("B") == "*Enzo*"


def test_email_names_setting_is_validated_and_saved(core):
    app, _, _ = core
    app.cfg.google_chat.names = "nickname"
    with pytest.raises(ValueError):
        app.cfg.validate()
    app.cfg.google_chat.names = "name"
    client = create_app(app).test_client()
    page = client.get("/settings").get_data(as_text=True)
    assert 'name="google_chat.names"' in page


def test_summary_lists_use_email_names(core):
    app, clock, _ = core
    app.store._db.execute("UPDATE employees SET name = 'Lương Thế Hùng', email = 'enzo@example.com' WHERE id = 'B'")
    app.store._db.commit()
    clock.t = at(11, 30)
    late(app, "A", 10, 15)
    text = app.service.summary_text(app.service.report(DAY))
    assert "• @Alice: 10:15" in text and "• @Enzo" in text and "Lương" not in text  # late and not-seen lists


def test_messages_tag_people_by_their_chat_id(core):
    app, clock, sent = core
    app.store.update_employee("A", "Alice", True, "1001")
    app.store.update_employee("B", "Bob", True, "1002")
    clock.t = at(11, 30)
    late(app, "A", 10, 15)
    text = app.service.summary_text(app.service.report(DAY))
    assert "• <users/1001>: 10:15" in text and "• <users/1002>" in text  # late, and not seen
    app.store._db.execute("UPDATE employees SET email = 'cee@example.com' WHERE id = 'C'")
    app.store._db.commit()
    # Carol has no Chat ID: she is called by her email name instead
    assert "• @Cee" in app.service.summary_text(app.service.report(DAY))
    app.cfg.google_chat.mention_people = False  # switched off: email names in bold
    text = app.service.summary_text(app.service.report(DAY))
    assert "• *Alice*: 10:15" in text and "<users/" not in text


def test_backgrounds_take_turns_by_day_and_the_admins_own_come_first(tmp_path):
    import datetime as dt

    import cv2
    import numpy as np

    from attendance.core import latephoto

    days = [dt.date(2026, 10, 20) + dt.timedelta(d) for d in range(5)]
    bundled = {latephoto.day_background(tmp_path, d, occasions=False) for d in days}
    assert len(bundled) == 5 and all(p.parent == latephoto.BUNDLED for p in bundled)  # five old pictures in turn
    own = tmp_path / latephoto.FOLDER / latephoto.BACKGROUNDS
    own.mkdir(parents=True)
    for name in ("a.jpg", "b.png"):
        cv2.imwrite(str(own / name), np.full((90, 160, 3), 128, np.uint8))
    assert {latephoto.day_background(tmp_path, d, occasions=False).name for d in days} == {"a.jpg", "b.png"}  # theirs replace them
    single = latephoto.background_path(tmp_path)
    cv2.imwrite(str(single), np.full((90, 160, 3), 200, np.uint8))
    assert latephoto.day_background(tmp_path, days[0]) == single  # the one uploaded in Settings wins


def test_reference_photo_stands_in_when_the_camera_did_not_see_the_person(core):
    """An arrival entered by hand has no camera picture: the person's best reference photo (the
    highest quality score, then the newest) is shown instead, whole, on a light card."""
    import cv2
    from test_evidence import seen

    from attendance.core import latephoto
    from conftest import face_image

    app, clock, _ = core
    clock.t = at(10, 40)
    late(app, "C", 10, 32)  # by hand, and Carol has no reference photo: no picture
    assert app.service.late_photo(DAY) is None
    folder = app.data_dir / "faces" / "C"
    folder.mkdir(parents=True)
    for name, score, when in (("a.jpg", 0.5, at(9, 0)), ("b.jpg", 0.9, at(9, 1)), ("c.jpg", 0.9, at(9, 2)),
                              ("gone.jpg", 1.0, at(9, 3))):
        if name != "gone.jpg":
            cv2.imwrite(str(folder / name), face_image(3, 120))
        app.store.add_embedding(app.cfg.recognition.model_id(), "C", face_image(3)[0, 0], "upload", when,
                                f"faces/C/{name}", True, score)
    assert app.service.best_reference_photo("C") == folder / "c.jpg"  # best score, newest; a missing file is skipped
    rel = app.service.late_photo(DAY)
    assert rel and (app.data_dir / rel).exists()
    key = (app.data_dir / rel).with_suffix(".key").read_text()
    seen(app, "B", at(10, 15))  # the camera's picture is still preferred for someone it saw
    assert app.service.late_photo(DAY) == rel and (app.data_dir / rel).with_suffix(".key").read_text() != key
    people = [latephoto.Person("Carol", "10:32", 32, folder / "c.jpg", "", True),
              latephoto.Person("Bob", "10:15", 15, folder / "c.jpg")]
    assert latephoto.fingerprint(people, None) != latephoto.fingerprint(
        [latephoto.Person("Carol", "10:32", 32, folder / "c.jpg"), people[1]], None)
    card, _ = latephoto._card(people[0])
    inner = card[latephoto.BORDER:-latephoto.BORDER, latephoto.BORDER:-latephoto.BORDER]
    assert (inner[-1, 0] == 235).all() and (inner[60, inner.shape[1] // 2] == 30).all()  # light card, face in the upper part


def test_background_list_previews_and_choice(core):
    """Settings lists every background with a preview; the clicked one is used every day, holidays too."""
    import cv2
    from test_evidence import seen

    from attendance.core import latephoto, settings

    app, clock, _ = core
    c = create_app(app).test_client()
    clock.t = at(10, 40)
    keys = {b.key: b for b in latephoto.backgrounds(app.data_dir)}
    assert "painting/roses-bowl.jpg" in keys and "holiday/christmas-2.jpg" in keys
    assert keys["holiday/christmas-2.jpg"].label == "Christmas 2" and keys["painting/roses-bowl.jpg"].label == "Roses bowl"
    assert not any(k.endswith(".md") for k in keys)

    page = c.get("/settings").get_data(as_text=True)
    assert 'value="painting/roses-bowl.jpg"' in page and "Automatic" in page
    thumb = c.get("/settings/late-background/preview/painting/roses-bowl.jpg")
    assert thumb.mimetype == "image/jpeg"
    picture = cv2.imdecode(np.frombuffer(thumb.data, np.uint8), cv2.IMREAD_COLOR)
    assert picture.shape[:2] == (latephoto.THUMBNAIL[1], latephoto.THUMBNAIL[0])
    assert c.get("/settings/late-background/preview/painting/roses-bowl.jpg?full=1").status_code == 200
    for bad in ("painting/nope.jpg", "painting/../../attendance.db", "../attendance.db"):
        assert c.get(f"/settings/late-background/preview/{bad}").status_code == 404

    christmas = dt.date(2026, 12, 25)
    resp = c.post("/settings/late-background/choose", data={"choice": "painting/roses-bowl.jpg"}, follow_redirects=True)
    assert "Background chosen" in resp.get_data(as_text=True)
    assert app.cfg.late_message.background == "painting/roses-bowl.jpg"
    assert latephoto.day_background(app.data_dir, christmas, True, app.cfg.late_message.background) == keys["painting/roses-bowl.jpg"].path
    seen(app, "B", at(10, 15))
    rel = app.service.late_photo(DAY)
    key = (app.data_dir / rel).with_suffix(".key").read_text()
    c.post("/settings/late-background/choose", data={"choice": "painting/parrots.jpg"})
    app.service.late_photo(DAY)
    assert (app.data_dir / rel).with_suffix(".key").read_text() != key  # the picture is made again

    # Survives a save of the Settings form (which does not post the field) and a new start.
    assert "background" in settings.EDITABLE["late_message"]
    c.post("/settings", data={"schedule.window_start": "08:00", "schedule.late_after": "10:00", "schedule.window_end": "11:00"})
    assert app.cfg.late_message.background == "painting/parrots.jpg"

    resp = c.post("/settings/late-background/choose", data={"choice": "../../attendance.db"}, follow_redirects=True)
    assert "not there any more" in resp.get_data(as_text=True) and app.cfg.late_message.background == "painting/parrots.jpg"
    assert latephoto.day_background(app.data_dir, DAY, True, "../../attendance.db").parent == latephoto.BUNDLED
    c.post("/settings/late-background/choose", data={"choice": ""})
    assert app.cfg.late_message.background == ""
    assert latephoto.day_background(app.data_dir, DAY, True, "").parent == latephoto.BUNDLED


def test_own_backgrounds_in_the_list_and_how_they_mix_with_a_choice(core):
    import cv2

    from attendance.core import latephoto

    app, _, _ = core
    c = create_app(app).test_client()
    ok, jpg = cv2.imencode(".jpg", np.full((90, 160, 3), 50, np.uint8))

    def pic(name="p.jpg"):
        return (io.BytesIO(jpg.tobytes()), name)

    c.post("/settings/late-background/choose", data={"choice": "painting/parrots.jpg"})
    c.post("/settings/late-background", data={"for": "rotation", "background": [pic()]}, content_type="multipart/form-data")
    assert app.cfg.late_message.background == ""  # new pictures are meant to be used
    assert [b.key for b in latephoto.backgrounds(app.data_dir) if b.group == "own"] == ["own/own-1.jpg"]

    c.post("/settings/late-background/choose", data={"choice": "own/own-1.jpg"})
    assert app.cfg.late_message.background == "own/own-1.jpg"
    page = c.get("/settings").get_data(as_text=True)
    assert 'name="remove" value="own-1.jpg"' in page
    c.post("/settings/late-background", data={"remove": "own-1.jpg"})
    assert app.cfg.late_message.background == ""  # the removed one is no longer chosen

    c.post("/settings/late-background", data={"for": "single", "background": [pic()]}, content_type="multipart/form-data")
    page = c.get("/settings").get_data(as_text=True)
    assert "Your picture is used every day" in page and 'value="" disabled' in page  # automatic needs it removed first
    c.post("/settings/late-background/choose", data={"choice": "single"})
    assert app.cfg.late_message.background == "single"
    c.post("/settings/late-background", data={"remove": "single"})
    assert app.cfg.late_message.background == "" and not latephoto.background_path(app.data_dir).exists()


def test_auto_message_includes_late_and_not_seen_once(core):
    app, clock, sent = core
    app.cfg.google_chat.notify_late_arrivals = False
    app.cfg.google_chat.late_time_list = False
    app.cfg.google_chat.daily_summary = False
    late(app, 'A', 9, 0)
    late(app, 'B', 10, 15)
    run_until(app.service, clock, at(11, 30))
    messages = [text for text in sent if text.startswith('Morning')]
    assert messages == ['Morning, coming late today:\nLate arrivals (1):\n• @Bob (10:15)\n\nNot seen (1):\n• @Carol']
    run_until(app.service, clock, at(12, 0))
    assert len([text for text in sent if text.startswith('Morning')]) == 1


def test_not_seen_only_message_mentions_people_without_attaching_picture(core):
    app, clock, sent = core
    app.store.update_employee('B', 'Bob', True, '123456')
    late(app, 'A', 9, 0)
    app.store.replace_external(DAY, DAY, [], [('C', DAY, DAY, 'leave', '')])
    clock.t = at(11, 0)
    assert app.service.late_text(DAY) == 'Not seen (1):\n• <users/123456>'
    assert app.service.late_text(DAY, preview=True) == 'Not seen (1):\n• @Bob'
    assert app.service.send_late_message(DAY, auto=True)
    assert sent[-1] == 'Not seen (1):\n• <users/123456>'
    assert app.service.late_photo(DAY) is None
    assert app.store.notice_sent(DAY, 'late_message')
    assert not app.store.notice_sent(DAY, 'late_message_skipped')


def test_not_seen_message_excludes_exempt_and_nonworking_days(core):
    app, clock, _ = core
    app.store.update_employee('B', 'Bob', True, exempt=True)
    app.store.replace_external(DAY, DAY, [], [('C', DAY, DAY, 'leave', '')])
    clock.t = at(11, 0)
    assert app.service.late_text(DAY) == 'Not seen (1):\n• @Alice'
    saturday = dt.date(2026, 10, 17)
    assert app.service.late_text(saturday) is None


def test_not_seen_preview_reports_missing_chat_ids_and_audits_delivery(core):
    app, clock, _ = core
    late(app, 'A', 9, 0)
    late(app, 'B', 10, 15)
    clock.t = at(11, 0)
    from attendance.web import late_message_status
    status = late_message_status(app, DAY)
    assert status['preview'].endswith('Not seen (1):\n• @Carol')
    assert 'Carol' in status['missing_chat']
    app.service.send_late_message(DAY)
    assert any(action == 'send late message' and 'Carol' in details
               for _, _, action, details in app.store.audit())


def test_notifications_can_turn_not_seen_off_and_restore_default(core):
    from attendance.core import settings
    from attendance.web import late_message_status

    app, clock, _ = core
    late(app, 'A', 9, 0)
    late(app, 'B', 10, 15)
    app.store.update_employee('B', 'Bob', True, '123456')
    clock.t = at(10, 40)
    client = create_app(app).test_client()
    page = client.get('/settings').get_data(as_text=True)
    assert 'name="late_message.include_not_seen"' in page
    client.post('/settings', data={'late_message.enabled': 'on', 'late_message.auto_send': 'on',
                                   'schedule.work_days': ['0', '1', '2', '3', '4']})
    assert app.service.late_text(DAY) == 'Morning, coming late today:\nLate arrivals (1):\n• <users/123456> (10:15)'
    assert not late_message_status(app, DAY)['missing_chat']
    assert settings.overrides(app.cfg, app.store)['late_message.include_not_seen'] == 'on'
    assert app.service.send_late_message(DAY)
    assert not any(action == 'send late message' and 'Carol' in details
                   for _, _, action, details in app.store.audit())
    app.service.set_arrival(DAY, 'B', dt.time(9, 0))
    assert app.service.late_text(DAY) is None  # No late arrivals; Not seen switched off.
    client.post('/settings/reset', data={'reset': 'late_message.include_not_seen'})
    assert app.service.late_text(DAY) == 'Not seen (1):\n• @Carol'


def test_people_not_seen_are_in_the_picture_when_the_message_lists_them(core, monkeypatch):
    from test_evidence import seen

    from attendance.core import latephoto

    app, clock, _ = core
    clock.t = at(10, 40)
    seen(app, "A", at(9, 0))
    seen(app, "B", at(10, 15))  # late, on camera; Carol is not seen
    reference = app.data_dir / "faces" / "C" / "c.jpg"
    reference.parent.mkdir(parents=True)
    import cv2
    cv2.imwrite(str(reference), np.full((60, 40, 3), 128, np.uint8))
    monkeypatch.setattr(app.service, "best_reference_photo", lambda emp: reference if emp == "C" else None)
    drawn = []
    real = latephoto.write
    monkeypatch.setattr(latephoto, "write", lambda data_dir, day, people, title, *a: (drawn.append((people, title)), real(data_dir, day, people, title, *a))[1])

    app.cfg.late_message.include_not_seen = True
    assert app.service.late_photo(DAY)
    people, title = drawn[-1]
    assert [(p.name, p.not_seen, p.lines()) for p in people] == [("Bob", False, ["10:15  +15 min"]), ("Carol", True, ["Not seen"])]
    assert people[1].photo == reference and title.startswith("Late and not seen on ")

    app.cfg.late_message.include_not_seen = False
    app.service.late_photo(DAY)
    people, title = drawn[-1]
    assert [p.name for p in people] == ["Bob"] and title.startswith("Late on ")


def test_a_picture_of_only_people_not_seen(core, monkeypatch):
    from attendance.core import latephoto

    app, clock, _ = core
    clock.t = at(10, 40)
    late(app, "A", 9, 0)
    late(app, "B", 9, 5)
    reference = app.data_dir / "faces" / "C" / "c.jpg"
    reference.parent.mkdir(parents=True)
    import cv2
    cv2.imwrite(str(reference), np.full((60, 40, 3), 128, np.uint8))
    monkeypatch.setattr(app.service, "best_reference_photo", lambda emp: reference if emp == "C" else None)
    app.cfg.late_message.include_not_seen = True
    rel = app.service.late_photo(DAY)
    assert rel and cv2.imread(str(app.data_dir / rel)) is not None
    app.cfg.late_message.include_not_seen = False
    assert app.service.late_photo(DAY) is None


def test_people_not_seen_start_a_row_of_their_own():
    from attendance.core import latephoto

    late = [latephoto.Person(f"L{i}", "10:00", 5, None) for i in range(3)]
    missing = [latephoto.Person(f"N{i}", "", 0, None, not_seen=True) for i in range(7)]
    rows = latephoto.rows_of(late + missing)
    assert [[p.name for p in row] for row in rows] == [["L0", "L1", "L2"], ["N0", "N1", "N2", "N3"], ["N4", "N5", "N6"]]
    assert latephoto.rows_of(missing[:2]) == [missing[:2]]
    assert latephoto.compose(late + missing, "t")[:2] == b"\xff\xd8"


def test_people_not_seen_with_a_pending_wfh_request_get_their_own_row(core, monkeypatch):
    import json

    from attendance.core import latephoto

    app, clock, _ = core
    clock.t = at(10, 40)
    late(app, "A", 10, 5)
    with app.store._db:
        app.store._db.execute("UPDATE employees SET base_username = 'carol' WHERE id = 'C'")
        app.store._db.execute("UPDATE employees SET base_username = 'bob' WHERE id = 'B'")
    app.store.put(f"base_timeoffs:{DAY}", json.dumps({
        "carol": [{"kind": "WFH", "absence": True, "approved": False, "shift": "9:00-13:00", "approvers": ["boss"]}],
        "bob": [{"kind": "Coming late", "absence": False, "approved": False, "shift": "9:00-09:30"}]}))
    assert app.service.pending_wfh(DAY) == {"C": "9:00-13:00"}
    drawn = []
    monkeypatch.setattr(latephoto, "write", lambda data_dir, day, people, *a: drawn.append(people) or "late/x.jpg")
    monkeypatch.setattr(app.service, "best_reference_photo", lambda emp: app.data_dir / "x.jpg")
    app.cfg.late_message.include_not_seen = True
    app.service.late_photo(DAY)
    rows = latephoto.rows_of(drawn[-1])
    assert [[(p.name, p.lines()) for p in row] for row in rows] == [
        [("Alice", ["10:05  +5 min"])], [("Bob", ["Not seen"])], [("Carol", ["Not seen", "WFH pending 9:00-13:00"])]]


def test_each_group_in_the_picture_has_a_heading():
    from attendance.core import latephoto

    late = [latephoto.Person("L", "10:00", 5, None)]
    missing = [latephoto.Person("N", "", 0, None, not_seen=True)]
    wfh = [latephoto.Person("W", "", 0, None, not_seen=True, wfh_pending="9:00-18:00")]
    assert [h for h, _ in latephoto.sections_of(late + missing + wfh)] == ["Late", "Not seen", "Not seen - WFH pending"]
    assert [h for h, _ in latephoto.sections_of(missing)] == ["Not seen"]


def test_the_message_lists_people_with_a_pending_wfh_request_on_their_own_line(core):
    import json

    app, clock, _ = core
    clock.t = at(10, 40)
    late(app, "A", 10, 5)
    with app.store._db:
        app.store._db.execute("UPDATE employees SET base_username = 'carol' WHERE id = 'C'")
    app.store.put(f"base_timeoffs:{DAY}", json.dumps({
        "carol": [{"kind": "WFH", "absence": True, "approved": False, "shift": "9:00-18:00", "approvers": ["boss"]}]}))
    app.cfg.late_message.include_not_seen = True
    assert app.service.late_text(DAY, preview=True) == (
        "Morning, coming late today:\nLate arrivals (1):\n• @Alice (10:05)\n\nNot seen (1):\n• @Bob"
        "\n\nNot seen, WFH pending (1):\n• @Carol (9:00-18:00)")


def test_the_preview_keeps_the_line_breaks_of_the_message():
    import pathlib
    import re

    css = (pathlib.Path(__file__).parents[1] / "attendance/web/static/app.css").read_text()
    assert "white-space: pre-line" in re.search(r"\.preview-box \{[^}]*\}", css).group(0)
