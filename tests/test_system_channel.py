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


def test_settings_names_the_two_spaces_and_each_test_posts_to_its_own(core):
    from attendance.web import create_app

    app, _, _ = core
    posts = []
    app.chat._post = lambda url, payload: posts.append((url, payload["text"]))
    c = create_app(app).test_client()
    page = c.get("/settings").get_data(as_text=True)
    assert "Notifications space" in page and "Monitoring space" in page and "Not set: these go to the notifications space" in page
    c.post("/settings/test-chat", data={"google_chat.webhook_url": "https://chat.example/n"})
    c.post("/settings/test-chat?system=1", data={"google_chat.system_webhook_url": "https://chat.example/m"})
    assert posts[0][0] == "https://chat.example/n" and "notifications" in posts[0][1]
    assert posts[1][0] == "https://chat.example/m" and "monitoring" in posts[1][1]
    app.cfg.google_chat.system_webhook_url = "https://chat.example/m"
    assert "Sent to its own space" in c.get("/settings").get_data(as_text=True)


def test_every_problem_goes_to_the_monitoring_space(core):
    """Camera and power alerts, the end-of-day problems: nothing of the kind in the notifications space."""
    app, clock, _ = core
    posts = []
    app.chat._post = lambda url, payload: posts.append((url, payload["text"]))
    app.cfg.google_chat.webhook_url = "https://chat.example/n"
    app.cfg.google_chat.system_webhook_url = "https://chat.example/m"
    app.service.alert("power unplugged")
    assert [(url.split("?")[0], text) for url, text in posts] == [("https://chat.example/m", "power unplugged")]


def test_failed_chat_api_warning_goes_to_the_monitoring_space(core):
    from attendance.integrations.chat_api import ChatApiError

    app, _, _ = core
    posts = []
    chat = GoogleChat("https://chat.example/n", app.store, post=lambda url, payload: posts.append((url, payload["text"])),
                      system_url="https://chat.example/m")

    class Api:
        ready = True

        def send(self, text, picture):
            raise ChatApiError("no access", permanent=True)
    chat.api = Api()
    chat.send("late today", None, NOW, api={"text": "late today"})
    assert posts[0][0] == "https://chat.example/m" and "could not be posted" in posts[0][1]
    assert posts[1] == ("https://chat.example/n", "late today")
