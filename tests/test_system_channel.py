import datetime as dt

from attendance.integrations.notifier import GoogleChat

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)


def make(store, system_url=""):
    posts = []
    chat = GoogleChat("https://chat.example/main", store, post=lambda url, payload: posts.append((url, payload)),
                      system_url=system_url)
    return chat, posts


def test_system_messages_go_to_the_system_webhook(core):
    app, _, _ = core
    chat, posts = make(app.store, "https://chat.example/system")
    chat.send("late", None, NOW)
    chat.send("power unplugged", None, NOW, system=True)
    assert [(u, p["text"]) for u, p in posts] == [("https://chat.example/main", "late"),
                                                  ("https://chat.example/system", "power unplugged")]
    assert all("system" not in p for _, p in posts)


def test_system_messages_use_the_main_webhook_when_no_system_one_is_set(core):
    app, _, _ = core
    chat, posts = make(app.store)
    chat.send("camera down", None, NOW, system=True)
    assert [(u, p["text"]) for u, p in posts] == [("https://chat.example/main", "camera down")]
