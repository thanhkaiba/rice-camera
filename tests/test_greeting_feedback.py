import datetime as dt
import json
import re

import pytest

from attendance.core import greeting

DAY = dt.date(2026, 11, 19)


def said(app, clock, emp, text, kind="on_time", day=DAY):
    app.store.add_greeting(day, emp, clock(), text, kind)


def people(app):
    return [app.store.add_employee(f"{n}@example.com", n) for n in ("an", "binh", "chi")]


def test_rating_is_kept_changed_and_taken_back(core):
    app, clock, _ = core
    a, b, _ = people(app)
    said(app, clock, a, "[excited] Anh An bước vào bá khí ghê!")
    store = app.store
    assert store.rate_greeting(DAY, a, "good", "  hay,   giữ kiểu này ", "admin", clock()) == {"rating": "good", "comment": "hay, giữ kiểu này"}
    assert store.greeting_feedback(DAY)[a]["text"] == "[excited] Anh An bước vào bá khí ghê!"
    store.rate_greeting(DAY, a, "bad", "", "admin", clock())
    assert store.greeting_feedback(DAY)[a]["rating"] == "bad"
    store.rate_greeting(DAY, a, "", "", "admin", clock())
    assert store.greeting_feedback(DAY) == {}
    assert store.rate_greeting(DAY, a, "", "chỉ ghi chú", "admin", clock())["rating"] == "ok"
    with pytest.raises(ValueError):
        store.rate_greeting(DAY, b, "good", "", "admin", clock())  # nothing was said to them
    with pytest.raises(ValueError):
        store.rate_greeting(DAY, a, "great", "", "admin", clock())


def test_claude_is_shown_earlier_days_only_each_sentence_once(core):
    app, clock, _ = core
    a, b, c = people(app)
    for emp in (a, b):
        said(app, clock, emp, "[laughs] Cả nhóm tới rồi nè!", "together")
        app.store.rate_greeting(DAY, emp, "good", "", "admin", clock())
    said(app, clock, c, "Chị Chi đúng giờ như đồng hồ.")
    app.store.rate_greeting(DAY, c, "bad", "nhạt, đừng ví với đồng hồ", "admin", clock())
    assert greeting.office_feedback(app.store, DAY) == {}  # today's sentences are written already
    tomorrow = greeting.office_feedback(app.store, DAY + dt.timedelta(days=1))
    assert tomorrow == {"liked": [{"text": "Cả nhóm tới rồi nè!"}],
                        "disliked": [{"text": "Chị Chi đúng giờ như đồng hồ.", "remark": "nhạt, đừng ví với đồng hồ"}]}
    assert greeting.office_feedback(app.store, DAY + dt.timedelta(days=greeting.FEEDBACK_DAYS + 1)) == {}


def test_page_lists_what_was_said_and_saves_a_mark(core):
    from attendance.web import create_app
    app, clock, _ = core
    a, _, _ = people(app)
    said(app, clock, a, "Anh An tới rồi nè!")
    client = create_app(app).test_client()
    page = client.get(f"/greetings?day={DAY}").get_data(as_text=True)
    assert "Anh An tới rồi nè!" in page and 'data-rate="good"' in page and ">Greetings</a>" in page
    token = re.search(r'name="csrf-token" content="([^"]*)"', page).group(1)
    form = {"csrf_token": token, "day": DAY.isoformat(), "employee_id": a, "rating": "good", "comment": "hay"}
    answer = client.post("/greetings/rate", data=form)
    assert answer.get_json() == {"ok": True, "rating": "good", "comment": "hay"}
    assert app.store.greeting_feedback(DAY)[a]["rating"] == "good"
    assert 'value="hay"' in client.get(f"/greetings?day={DAY}").get_data(as_text=True)
    assert client.post("/greetings/rate", data={**form, "employee_id": "nobody"}).status_code == 400
    assert "Nothing was said" in client.get("/greetings?day=2020-01-01").get_data(as_text=True)


def test_the_brief_for_claude_carries_the_feedback(core):
    from attendance.core.greeting import Greeter
    app, clock, _ = core
    a, _, _ = people(app)
    g = Greeter(app.cfg, app.store, clock, lambda d: clock())
    day = clock().date()
    g.set_rules({"day": day.isoformat(), "workday": True, "people": {},
                 "feedback": {"liked": [{"text": "Anh An bước vào bá khí ghê!"}]}})
    system, _, _ = g.prompt([app.store.employee(a)], day)
    assert "bá khí" in system and "Do not recycle a liked sentence" in system
    g.set_rules({"day": day.isoformat(), "workday": True, "people": {}})
    assert "rated greetings" not in g.prompt([app.store.employee(a)], day)[0]


def test_review_has_filters_plain_words_and_boss_context(core):
    from attendance.web import create_app
    app, clock, _ = core
    a, _, _ = people(app)
    app.store.set_greetings({a: {"honorific": "sep"}})
    said(app, clock, a, "[cheerfully] Hello An!")
    page = create_app(app).test_client().get(f"/greetings?day={DAY}").get_data(as_text=True)
    assert 'id="review-filter"' in page and 'id="review-search"' in page
    assert 'data-person-kind="boss"' in page
    assert '<div class="greet-text">“Hello An!”</div>' in page
    assert 'Voice direction' in page and 'More respectful' in page
    assert 'Save review' in page


def test_today_the_page_shows_what_the_door_is_still_going_to_say(core):
    from attendance.core.greeting import LINES_KEY, Greeter
    from attendance.web import create_app, edge_api
    app, clock, _ = core
    a, b, _ = people(app)
    day = clock().date()
    app.store.put(LINES_KEY, json.dumps({"day": day.isoformat(), "people": {
        a: {"on_time": ["[cheerfully] Anh An tới sớm ghê!"], "late": ["Anh An vô thôi!"], "timed": True, "note": ""},
        b: {"on_time": ["Chị Bình chào buổi sáng!"], "late": ["Chị Bình tới rồi!"], "timed": False, "note": ""}}}))
    written = Greeter(app.cfg, app.store, clock, lambda d: clock()).upcoming(day)
    assert written["people"][a]["on_time"] == ["Anh An tới sớm ghê!"] and written["people"][b]["timed"] is False
    app.store.put(edge_api.UPCOMING_KEY, json.dumps({**written, "at": clock().isoformat()}))
    said(app, clock, a, "Anh An tới sớm ghê!", day=day)
    client = create_app(app).test_client()
    coming = client.get("/greetings").get_data(as_text=True).split("Still to come today")[1].split("</details>")[0]
    assert "1 person not greeted yet" in coming and "any time:</span> “Chị Bình tới rồi!”" in coming
    assert "Anh An" not in coming  # greeted already: in the list below, to be marked
    yesterday = (day - dt.timedelta(days=1)).isoformat()
    assert "Still to come today" not in client.get(f"/greetings?day={yesterday}").get_data(as_text=True)


def test_the_page_opens_on_today_once_the_sentences_are_written_even_before_anyone_is_greeted(core):
    from attendance.web import create_app, edge_api
    app, clock, _ = core
    a, _, _ = people(app)
    day = clock().date()
    said(app, clock, a, "Hôm qua nè", day=day - dt.timedelta(days=1))
    client = create_app(app).test_client()
    assert "Still to come today" not in client.get("/greetings").get_data(as_text=True)  # nothing written: yesterday
    app.store.put(edge_api.UPCOMING_KEY, json.dumps({"day": day.isoformat(), "at": clock().isoformat(), "together": [],
                                                     "people": {a: {"on_time": ["Anh An sớm ghê!"], "late": [], "timed": True, "note": ""}}}))
    page = client.get("/greetings").get_data(as_text=True)
    assert "Still to come today" in page and "Anh An sớm ghê!" in page
