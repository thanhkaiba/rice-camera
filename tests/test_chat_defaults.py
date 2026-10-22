"""What Google Chat gets by default: the late message, and problems. Nothing else."""
import pytest

from attendance.config import Config
from conftest import at
from test_service import run_until


@pytest.fixture
def quiet(env):
    service, store, clock, sent = env
    defaults = Config().google_chat
    chat = service.cfg.google_chat
    chat.notify_late_arrivals, chat.late_time_list, chat.daily_summary = (
        defaults.notify_late_arrivals, defaults.late_time_list, defaults.daily_summary)
    return service, store, clock, sent


def test_by_default_only_the_late_message_is_posted(quiet):
    service, store, clock, sent = quiet
    assert not (service.cfg.google_chat.notify_late_arrivals or service.cfg.google_chat.late_time_list
                or service.cfg.google_chat.daily_summary)
    run_until(service, clock, at(8, 30))
    service.on_identified("A", at(8, 30), 0.8, None)  # on time
    run_until(service, clock, at(10, 30))
    service.on_identified("B", at(10, 30), 0.8, None)  # late: not posted on its own
    assert sent == []  # no late-time list either
    run_until(service, clock, at(11, 30))
    assert len(sent) == 1 and sent[0].startswith("Morning, coming late today:") and "@Bob" in sent[0]
    assert store.notice_sent(at(11, 30).date(), "summary")  # handled, so not posted later either


def test_without_the_summary_its_warnings_still_go_out(quiet):
    service, store, clock, sent = quiet
    clock.t = at(14, 0)  # the computer was off all morning
    service.tick()
    assert len(sent) == 1 and sent[0].startswith("🛠") and "No camera data at all" in sent[0]
