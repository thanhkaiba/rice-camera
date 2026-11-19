import datetime as dt
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
    assert "bá khí" in system and "one person in eight" in system
    g.set_rules({"day": day.isoformat(), "workday": True, "people": {}})
    assert "rated greetings" not in g.prompt([app.store.employee(a)], day)[0]
