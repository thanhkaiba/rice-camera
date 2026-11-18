"""The morning's "cold fine" notice, the public page where people appeal, and the approvers' page.

All of it runs on the web server (Settings → Notifications → Fine notice):

- At `send_at` one Google Chat message goes to the notifications space: a mock-solemn opening line,
  who is late and who is not seen, the picture of the late people, and the link to the appeal page.
- The appeal page needs no login: its address carries a token that is different every day. It lists
  who is fined (the late with their camera photo, the not seen by name only) and lets someone pick
  their name and write why. Arrivals up to `list_until` still move people on the list.
- Each appeal is announced in the approvers' space with the link to the review page (another token).
  An accepted appeal counts the person as on time for the day (see AttendanceService.report).

Every time in the settings names a whole minute that is still included: with appeal_until 10:10 the
page closes when the clock turns 10:11, the same way an arrival in the late_after minute is on time.

The opening line is written by Claude on the office Mac together with the day's greetings
(greeting.py) and comes with the Mac's heartbeat; without one, a line from FALLBACK_INTROS is used.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..store import body_photo

log = logging.getLogger(__name__)

NOTICE = "fine_notice"
SKIPPED = "fine_notice_skipped"
SECRET_KEY = "fine_secret"
INTROS_KEY = "fine_intros"  # {"day", "lines"}: the opening lines the Mac sent for that day
CHOSEN_KEY = "fine_intro_chosen"  # {"day", "text", "source"}: the one the day's message opened with
USED_KEY = "fine_intros_used"  # [[day, text], ...]: the lines of the last days, not to be used again so soon
USED_DAYS = 7
ROUTE = "approvers"  # GoogleChat route of the approvers' space
MAX_MESSAGE = 500
MAX_NOTE = 200
MAX_EDITS = 3  # times someone may rewrite an appeal nobody has decided yet
MAX_INTRO = 300
MAX_INTROS = 10
APPEAL, REVIEW = "appeal", "review"
FALLBACK_INTROS = [
    "🚨 Hệ thống phạt nguội văn phòng trân trọng thông báo: camera đã ghi nhận một số trường hợp vượt vạch giờ làm sáng nay.",
    "Thông báo phạt nguội: qua trích xuất camera, tổ tuần tra giờ giấc đã lập danh sách các phương tiện về bến chưa đúng lịch trình hôm nay.",
    "Trung tâm giám sát giờ giấc xin thông báo: hình ảnh phạt nguội sáng nay đã được trích xuất, kính mời các chủ phương tiện có tên kiểm tra.",
    "Biên bản phạt nguội đã lên sóng 📸 Kính mời quý đồng nghiệp tra cứu xem mình có vinh dự góp mặt hay không.",
    "Kính gửi quý cư dân văn phòng: đợt xử lý phạt nguội buổi sáng đã hoàn tất khâu trích xuất hình ảnh.",
    "Cổng tra cứu phạt nguội hôm nay chính thức mở. Mọi thắc mắc của quý đồng nghiệp sẽ được tiếp nhận một cách nghiêm túc.",
    "Thông báo từ đội camera: ca tuần tra buổi sáng đã kết thúc và danh sách phạt nguội đã được niêm yết công khai.",
    "Hệ thống phạt nguội xin phép làm phiền cả nhà một chút ⏰ Danh sách sáng nay đã có, kính mời mọi người kiểm tra.",
]
# What Claude is asked for, added to the greetings request (greeting.py).
INTRO_COUNT = 5
INTRO_RULES = f"""
Also write "fine_intros": {INTRO_COUNT} different opening lines, in Vietnamese, for this morning's message of the office's \
"hệ thống phạt nguội" (cold fines for coming late, a running office joke). Each one sounds like a solemn official \
traffic-camera fine announcement and is in fact light teasing.
- One sentence, at most 30 words, and at most one emoji.
- Never name or hint at a person, never say how late anyone is, never mock or shame anyone: it must suit a workplace.
- No links, no times of day, no amounts of money: the list, the deadline and the link are added after it.
- Use today's context given before the list (the weekday, an occasion near today, the weather) where it fits, in \
some of them; never mention weather, a date or an occasion that is not given there.
- "recent_fine_intros", when given, are the lines of the last days: do not repeat them or open the same way."""
INTRO_SCHEMA = {"type": "array", "items": {"type": "string"}}

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌚-⏿]")
_NOT_IN_INTRO = re.compile(r"https?:|://|www\.|[<>@]|\b\d{1,2}\s*(?::|h|giờ)\s*\d{0,2}\b", re.IGNORECASE)


def clean_intro(text) -> str | None:
    """An opening line fit to post: one line, no link, mention or time of day, at most one emoji. None if not."""
    if not isinstance(text, str):
        return None
    text = " ".join(unicodedata.normalize("NFC", text).split())
    if not text or len(text) > MAX_INTRO or _NOT_IN_INTRO.search(text) or len(_EMOJI.findall(text)) > 1:
        return None
    return text


def clean_intros(lines) -> list[str]:
    if not isinstance(lines, list):
        return []
    return list(dict.fromkeys(t for t in map(clean_intro, lines[:MAX_INTROS * 2]) if t))[:MAX_INTROS]


def keep_intros(store, day: dt.date, lines) -> None:
    """Remember the opening lines the Mac sent for `day` (edge heartbeat)."""
    lines = clean_intros(lines)
    saved = json.loads(store.get(INTROS_KEY) or "{}")
    if lines and (saved.get("day"), saved.get("lines")) != (day.isoformat(), lines):
        store.put(INTROS_KEY, json.dumps({"day": day.isoformat(), "lines": lines}, ensure_ascii=False))


def intro(store, day: dt.date, choose=random.choice) -> tuple[str, str]:
    """(the day's opening line, "claude" or "fallback"). Chosen once a day at random, from Claude's lines
    for that day when there are some, and never one of the lines of the last USED_DAYS days while
    another is left."""
    chosen = json.loads(store.get(CHOSEN_KEY) or "{}")
    if chosen.get("day") == day.isoformat():
        return chosen["text"], chosen["source"]
    since = (day - dt.timedelta(days=USED_DAYS)).isoformat()
    used = [e for e in json.loads(store.get(USED_KEY) or "[]") if since <= e[0] < day.isoformat()]
    saved = json.loads(store.get(INTROS_KEY) or "{}")
    written = clean_intros(saved.get("lines")) if saved.get("day") == day.isoformat() else []
    source, lines = ("claude", written) if written else ("fallback", FALLBACK_INTROS)
    seen = {text for _, text in used}
    text = choose([t for t in lines if t not in seen] or lines)
    store.put(CHOSEN_KEY, json.dumps({"day": day.isoformat(), "text": text, "source": source}, ensure_ascii=False))
    store.put(USED_KEY, json.dumps(used + [[day.isoformat(), text]], ensure_ascii=False))
    return text, source


# times and links ------------------------------------------------------------------------------

@dataclass
class Times:
    send: dt.datetime
    list_close: dt.datetime  # the first moment after list_until, and so on
    appeal_close: dt.datetime
    review_close: dt.datetime


def times(service, day: dt.date) -> Times:
    minute = dt.timedelta(minutes=1)
    send, list_until, appeal_until, review_until = (dt.datetime.combine(day, c, tzinfo=service.tz)
                                                    for c in service.cfg.fine_notice.clocks())
    return Times(send, list_until + minute, appeal_until + minute, review_until + minute)


def token(service, day: dt.date, page: str) -> str:
    secret = service.store.get(SECRET_KEY)
    if not secret:
        secret = secrets.token_hex(32)
        service.store.put(SECRET_KEY, secret)
    return hmac.new(secret.encode(), f"{page}:{day.isoformat()}".encode(), hashlib.sha256).hexdigest()[:40]


def link(service, day: dt.date, page: str) -> str:
    path = "/fines/review/" if page == REVIEW else "/fines/"
    return service.cfg.web.public_url.rstrip("/") + path + token(service, day, page)


def open_day(service, page: str, given: str) -> dt.date | None:
    """Today, when `given` is today's token of `page` and the page is open now; else None.
    Yesterday's link never matches, and today's stops working when its time is over."""
    fn = service.cfg.fine_notice
    now = service.now()
    day = now.date()
    if not fn.enabled or not hmac.compare_digest(given.encode(), token(service, day, page).encode()):
        return None
    t = times(service, day)
    return day if t.send <= now < (t.review_close if page == REVIEW else t.appeal_close) else None


# who is fined ---------------------------------------------------------------------------------

@dataclass
class Fined:
    employee_id: str
    name: str
    kind: str  # late or not_seen
    time: str = ""  # when they arrived, for the late
    photo: Path | None = None  # the camera's picture of that arrival; never one of someone not seen
    appeal: dict | None = None


def fined(service, day: dt.date, accepted: bool = False) -> list[Fined]:
    """Who the day's notice is about: the late (with the camera's photo of their arrival), then the
    not seen. Someone whose appeal was accepted counts as on time and is not here, unless `accepted`
    asks for them too (the review page). An arrival after `list_until` no longer moves anyone: they
    stay listed as not seen. A day the camera never worked fines nobody for not being seen."""
    rep = service.report(day, appeals=not accepted)
    if not rep.workday:
        return []
    data_dir, close = service.cfg.path(service.cfg.data_dir), times(service, day).list_close
    appeals = {a["employee_id"]: a for a in service.store.appeals(day)}
    late, missing = [], []
    for a in service.late_people(day, appeals=not accepted):
        name = service.chat_name(a.employee_id, a.name)
        if a.ts >= close:
            missing.append(Fined(a.employee_id, name, "not_seen", appeal=appeals.get(a.employee_id)))
            continue
        photo = None
        if a.image and a.source != "manual":
            photo = next((data_dir / rel for rel in (body_photo(a.image), a.image) if (data_dir / rel).exists()), None)
        late.append(Fined(a.employee_id, name, "late", a.time, photo, appeals.get(a.employee_id)))
    if not rep.no_camera_data:
        missing += [Fined(emp, service.chat_name(emp, name), "not_seen", appeal=appeals.get(emp))
                    for emp, name in service.not_seen_people(day, appeals=not accepted)]
    return late + sorted(missing, key=lambda f: f.name.lower())


def approvers(service) -> dict[str, str]:
    """id -> name of the people who may decide, as chosen in Settings (and still on the People page)."""
    names = service.shown_names()
    return {i: names[i] for i in service.cfg.fine_notice.approvers.split(",") if i in names}


# the message ----------------------------------------------------------------------------------

def notice_text(service, day: dt.date, preview: bool = False) -> str | None:
    """The day's message, or None when nobody is fined. A preview does not settle the opening line."""
    people = fined(service, day)
    if not people:
        return None
    listed = service.late_text(day, preview=preview, intro="", not_seen=not service.report(day).no_camera_data)
    opening = FALLBACK_INTROS[0] if preview else intro(service.store, day)[0]
    until = service.cfg.fine_notice.appeal_until
    return (f"{opening}\n\n{listed}\n\n🙋 Ai thấy oan xin lên tiếng trước {until} · "
            f"<{link(service, day, APPEAL)}|Xem danh sách và kêu oan>")


def send(service, day: dt.date, user: str = "system") -> bool:
    """Post the day's notice; False when nobody is fined or it was posted already."""
    now = service.now()
    text = notice_text(service, day)
    if text is None:
        service.store.mark_notice(day, SKIPPED, now)
        return False
    if not service.store.mark_notice(day, NOTICE, now):
        return False
    service.post_with_late_photo(text, day)
    service.store.log(now, user, "send fine notice", ", ".join(f.name for f in fined(service, day)))
    return True


def send_if_due(service, now: dt.datetime) -> None:
    """From `send_at` until the appeal page closes (so a server that was down at that minute still
    sends it), once a day."""
    day = now.date()
    if not service.cfg.fine_notice.enabled or not service.is_workday(day):
        return
    t = times(service, day)
    if (t.send <= now < t.appeal_close and not service.store.notice_sent(day, NOTICE)
            and not service.store.notice_sent(day, SKIPPED)):
        send(service, day)


# appeals --------------------------------------------------------------------------------------

def submit(service, day: dt.date, employee_id: str, message: str) -> str | None:
    """Record someone's appeal and tell the approvers. Returns what is wrong, in Vietnamese, or None."""
    message = " ".join((message or "").split())
    person = next((f for f in fined(service, day) if f.employee_id == employee_id), None)
    if person is None:
        return "Tên này không có trong danh sách hôm nay."
    if not message:
        return "Chưa kể gì thì chưa xét được, bạn viết đôi dòng nhé."
    if len(message) > MAX_MESSAGE:
        return f"Lời nhắn dài quá {MAX_MESSAGE} ký tự."
    before = person.appeal
    if before and before["status"] != "pending":
        return "Lời kêu oan của bạn đã được xử, không sửa được nữa."
    if before and before["edits"] >= MAX_EDITS:
        return "Lời kêu oan này đã sửa quá số lần cho phép."
    now = service.now()
    service.store.put_appeal(day, employee_id, message, person.kind, now)
    service.store.log(now, "appeal page", "appeal changed" if before else "appeal", f"{person.name}: {message}")
    what = f"đến lúc {person.time}" if person.kind == "late" else "chưa thấy trên camera"
    review_until = service.cfg.fine_notice.review_until
    mentions = " ".join(service.mention(i) for i in approvers(service))
    text = (f"🙋 {'Lời kêu oan được sửa' if before else 'Có người kêu oan'}: *{person.name}* ({what})\n“{message}”\n"
            f"Xử trước {review_until}: <{link(service, day, REVIEW)}|Vào phòng xử>" + (f"\n{mentions}" if mentions else ""))
    if service.cfg.fine_notice.approver_webhook_url:
        service.chat.send(text, None, now, route=ROUTE)
    else:
        log.warning("An appeal came in, but Settings name no approvers' space: nobody was told")
    return None


def decide(service, day: dt.date, employee_id: str, accept: bool, approver_id: str, note: str = "") -> str | None:
    """An approver's decision; it may be changed while the review page is open. Returns what is wrong or None."""
    who = approvers(service).get(approver_id)
    if who is None:
        return "Hãy chọn tên người cầm cân nảy mực."
    appeal = service.store.appeal(day, employee_id)
    if appeal is None:
        return "Không có lời kêu oan này."
    note = " ".join((note or "").split())[:MAX_NOTE]
    now = service.now()
    status = "accepted" if accept else "rejected"
    service.store.decide_appeal(day, employee_id, status, who, note, now)
    service.store.log(now, who, f"appeal {status}",
                      f"{service.chat_name(employee_id)}: {appeal['message']}" + (f" | {note}" if note else ""))
    return None
