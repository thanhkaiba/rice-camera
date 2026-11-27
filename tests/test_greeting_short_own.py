"""A busy door's short hello of their own for some people, from Settings."""
from attendance.core import settings
from conftest import DAY
from test_greeting import make


def test_someone_can_have_a_busy_door_hello_of_their_own(core):
    app, _, g, _, _ = make(core)
    alice, bob = app.store.employee("A"), app.store.employee("B")
    usual = g.short_line(alice, DAY)
    app.cfg.greeting.short_hellos = f"{alice.name} = Em chào Chủ tịch Alice ạ, mời chị vô!\nnobody = x"
    assert g.short_line(alice, DAY) == "Em chào Chủ tịch Alice ạ, mời chị vô!" != usual
    assert g.short_line(bob, DAY) and "Chủ tịch" not in g.short_line(bob, DAY)
    assert g._kept_line("Em chào Chủ tịch Alice ạ, mời chị vô!")  # read once, kept with the names
    app.cfg.greeting.short_hellos = ""
    assert g.short_line(alice, DAY) == usual


def test_the_own_hellos_are_on_the_settings_page(core):
    from attendance.web import create_app

    app, _, _ = core
    page = create_app(app).test_client().get("/settings").get_data(as_text=True)
    assert 'name="greeting.short_hellos"' in page and "short_hellos" in settings.EDITABLE["greeting"]
