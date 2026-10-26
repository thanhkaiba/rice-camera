"""Attendance rules, admin corrections and scheduled Google Chat notices.

Rules for a work day (times from config.schedule):
- First seen through the late_after minute (e.g. 10:00:59): on time. Later sightings change nothing.
- First seen from the next minute (e.g. 10:01): late, and announced as soon as they are seen.
- At that next minute: one alert listing everyone not seen yet.
- At window_end (e.g. 11:00): daily summary.
- People on leave are never counted as late or missing; holidays send nothing.
- Exempt people (e.g. the boss) have their arrival recorded but are never counted as late or missing.
- A camera that stops working during the camera hours is reported, and so is its recovery.
- Faces the system could not identify are reported to the admin with a link to review them.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import numpy as np

from . import latephoto
from ..integrations import leave_api
from .config import Config, parse_clock
from ..vision.gallery import Gallery
from ..vision.tracker import vote_for
from ..integrations.notifier import GoogleChat
from ..store import LEAVE_KINDS, Employee, Store, body_photo, scene_photo

if TYPE_CHECKING:
    from ..vision.faces import FaceLibrary

log = logging.getLogger(__name__)

GAP = dt.timedelta(seconds=60)  # no frames for longer than this counts as an outage
CAMERA_ALERT_AFTER = dt.timedelta(minutes=2)  # a camera down this long during the camera hours is reported
CAMERA_ALERT_KEY = "camera_alert"  # set while a "camera not working" alert is out, so it is sent once
ALIVE_SAVE_EVERY = dt.timedelta(seconds=10)
FLUSH_EVERY = dt.timedelta(seconds=30)
SOURCES = {"camera": "Camera", "admin": "Admin confirmed photo", "manual": "Entered by admin",
           "corrected": "Corrected by admin"}


@dataclass
class Window:
    start: dt.datetime
    late_after: dt.datetime
    end: dt.datetime

    @property
    def late_from(self) -> dt.datetime:
        return self.late_after + dt.timedelta(minutes=1)


@dataclass
class Arrival:
    employee_id: str
    name: str
    ts: dt.datetime
    source: str
    image: str | None
    score: float | None = None

    @property
    def time(self) -> str:
        return self.ts.strftime("%H:%M")


@dataclass
class DayReport:
    day: dt.date
    workday: bool
    day_note: str
    window: Window
    on_time: list[Arrival] = field(default_factory=list)
    late: list[Arrival] = field(default_factory=list)
    not_seen: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    on_leave: list[tuple[str, str, str]] = field(default_factory=list)  # (id, name, kind label)
    # People not counted as late or absent (e.g. the boss): (id, name, arrival if seen)
    exempt: list[tuple[str, str, Arrival | None]] = field(default_factory=list)
    no_photos: list[tuple[str, str]] = field(default_factory=list)
    pending_unknowns: int = 0
    outages: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)
    # True when the camera never worked that day (e.g. before the system was installed or the
    # computer was off): "not seen" then says nothing about who was absent.
    no_camera_data: bool = False

    @property
    def total(self) -> int:
        return len(self.on_time) + len(self.late) + len(self.not_seen)

    @property
    def last_arrival(self) -> Arrival | None:
        arrivals = self.on_time + self.late
        return max(arrivals, key=lambda a: a.ts) if arrivals else None

    @property
    def all_in(self) -> bool:
        """Everyone expected has arrived on time: nobody is missing or late and nothing is left to
        review, so the late-time message would have nothing to say."""
        return self.workday and bool(self.on_time) and not self.not_seen and not self.late and not self.pending_unknowns


def email_name(email: str) -> str:
    """ruby@example.com → Ruby, tan.nguyen@… → Tan Nguyen: how a team that uses email names calls people."""
    local = email.partition("@")[0]
    return " ".join(w[:1].upper() + w[1:] for w in local.replace("_", ".").replace("-", ".").split(".") if w) or email


def minutes_late(a: Arrival, w: Window) -> int:
    return int((a.ts - w.late_after).total_seconds() // 60)


@dataclass
class PersonSummary:
    employee_id: str
    name: str
    work_days: int = 0
    on_time: int = 0
    late: int = 0
    late_minutes: int = 0
    not_seen: int = 0
    on_leave: int = 0


@dataclass
class PeriodReport:
    start: dt.date
    end: dt.date
    days: list[DayReport]  # work days only
    people: list[PersonSummary]

    @property
    def no_data_days(self) -> list[DayReport]:
        return [d for d in self.days if d.no_camera_data]


class AttendanceService:
    def __init__(self, cfg: Config, store: Store, chat: GoogleChat, gallery: Gallery,
                 now: Callable[[], dt.datetime] | None = None):
        self.cfg = cfg
        self.tz = cfg.tz
        self.store = store
        self.chat = chat
        self.gallery = gallery
        self.library: FaceLibrary | None = None  # set by the app; used to learn admin-confirmed faces
        self.backup = None  # set by the app: the daily backup (backup.Backup)
        self.placement = None  # set by the app: how well the camera sees faces (placement.Placement)
        self._now = now or (lambda: dt.datetime.now(self.tz))
        saved = store.get("last_alive")
        self._last_alive = dt.datetime.fromisoformat(saved) if saved else None
        self._last_alive_saved = self._last_alive
        self._last_flush: dt.datetime | None = None
        self._camera_seen_day: dt.date | None = None
        self._bad_since: dt.datetime | None = None  # when the camera stopped working, in this run

    @property
    def model_id(self) -> str:
        return self.cfg.recognition.model_id()

    # time helpers ------------------------------------------------------------

    def now(self) -> dt.datetime:
        return self._now()

    def window(self, day: dt.date) -> Window:
        start, late, end = self.cfg.schedule.clocks()
        return Window(*(dt.datetime.combine(day, t, tzinfo=self.tz) for t in (start, late, end)))

    def day_info(self, day: dt.date) -> tuple[bool, str]:
        """(is a work day, note) taking the holiday calendar into account."""
        override = self.store.calendar_day(day)
        if override:
            kind, note = override
            label = "Holiday" if kind == "holiday" else "Extra work day"
            return kind == "workday", f"{label}: {note}" if note else label
        return day.weekday() in self.cfg.schedule.work_days, ""

    def is_workday(self, day: dt.date) -> bool:
        return self.day_info(day)[0]

    def in_window(self, now: dt.datetime) -> bool:
        w = self.window(now.date())
        return w.start <= now < w.end and self.is_workday(now.date())

    def _local(self, ts: dt.datetime) -> dt.datetime:
        return ts.astimezone(self.tz)

    def _link(self, path: str = "", text: str | None = None) -> str:
        """The admin page's address; with `text`, as a Google Chat link showing that text."""
        url = self.cfg.web.public_url.rstrip("/") + path
        return f"<{url}|{text}>" if text else url

    def _send(self, text: str, day: dt.date) -> None:
        self.chat.send(text, f"attendance-{day.isoformat()}", self.now())

    def alert(self, text: str) -> None:
        """System alert, posted in today's thread."""
        self._send(text, self.now().date())

    def _name(self, employee_id: str) -> str:
        e = self.store.employee(employee_id)
        return e.name if e else employee_id

    def chat_name(self, employee_id: str, name: str | None = None) -> str:
        """The name used in Chat messages: the People page name, or with google_chat.names = "email"
        the part of the email before @ (ruby@… → Ruby, tan.nguyen@… → Tan Nguyen)."""
        if self.cfg.google_chat.names == "email":
            e = self.store.employee(employee_id)
            if e and e.email:
                return email_name(e.email)
        return name or self._name(employee_id)

    def shown_name(self, e: Employee) -> str:
        """chat_name for an employee already at hand: how the admin pages name people too."""
        return email_name(e.email) if self.cfg.google_chat.names == "email" and e.email else e.name

    def shown_names(self, active_only: bool = True) -> dict[str, str]:
        """id -> shown_name, in alphabetical order of that name (pickers, look-alike warnings)."""
        people = [e for e in self.store.all_employees() if e.active or not active_only]
        return dict(sorted(((e.id, self.shown_name(e)) for e in people), key=lambda kv: kv[1].lower()))

    def mention(self, employee_id: str) -> str:
        """A Google Chat @mention when the employee's Chat user ID is known, else "@Name" as plain text."""
        e = self.store.employee(employee_id)
        if e and e.chat_user:
            return f"<users/{e.chat_user}>"
        return f"@{self.chat_name(employee_id)}"

    def who(self, employee_id: str, name: str | None = None) -> str:
        """How a person is named in alerts: a mention if that is switched on, else their name in bold."""
        if self.cfg.google_chat.mention_people:
            return self.mention(employee_id)
        return f"*{self.chat_name(employee_id, name)}*"

    # daily late message ----------------------------------------------------------

    def late_people(self, day: dt.date) -> list[Arrival]:
        """Late arrivals of the day (people on leave who came in anyway are left out)."""
        if not self.is_workday(day):
            return []
        on_leave = self.store.leaves_on(day)
        return [a for a in self.report(day).late if a.employee_id not in on_leave]

    def late_text(self, day: dt.date, preview: bool = False) -> str | None:
        """'Morning, coming late today: @A (10:15), and @B (10:32)'. With preview=True names are
        shown as @Name instead of Google Chat mention codes. A message about another day than
        today starts with that day, so "today" in the wording cannot mislead."""
        people = self.late_people(day)
        if not people:
            return None
        lm, w = self.cfg.late_message, self.window(day)
        items = [lm.item.format(mention=f"@{self.chat_name(a.employee_id, a.name)}" if preview
                                else self.mention(a.employee_id), name=self.chat_name(a.employee_id, a.name),
                                time=a.time, minutes=minutes_late(a, w)) for a in people]
        joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]
        prefix = "" if day == self.now().date() else f"{day:%A %d %b}: "
        return f"{prefix}{lm.intro} {joined}"

    def late_photo(self, day: dt.date) -> str | None:
        """One picture of the day's late people (the whole person as the camera saw them, name and
        time burned in), made or refreshed as needed: its path under the data folder. None when
        the picture is switched off, nobody is late, or none of the late people was on camera."""
        if not self.cfg.late_message.photo:
            return None
        people = self.late_people(day)
        if not people:
            return None
        data_dir, w = self.cfg.path(self.cfg.data_dir), self.window(day)
        items = []
        for a in people:
            photo = None
            if a.image:  # the whole person when it was kept, else the face
                photo = next((data_dir / rel for rel in (body_photo(a.image), a.image) if (data_dir / rel).exists()),
                             None)
            items.append(latephoto.Person(self.chat_name(a.employee_id, a.name), a.time, minutes_late(a, w), photo))
        if not any(p.photo for p in items):  # nobody on camera (all entered by hand): nothing to show
            latephoto.remove(data_dir, day)
            return None
        try:
            return latephoto.write(data_dir, day, items, f"Late on {day:%A %d %b %Y}", self.cfg.late_message.occasions)
        except Exception:
            log.exception("Could not make the picture of the late people")
            return None

    def _photo_secret(self) -> str:
        secret = self.store.get("late_photo_secret")
        if not secret:
            secret = secrets.token_hex(32)
            self.store.put("late_photo_secret", secret)
        return secret

    def late_photo_token(self, day: dt.date) -> str:
        return latephoto.token(self._photo_secret(), day)

    def late_photo_url(self, day: dt.date) -> str | None:
        """Address of the day's picture, fetchable without the admin password (the token stands in
        for it, so Google Chat can load the picture). None when there is no picture."""
        rel = self.late_photo(day)
        if rel is None:
            return None
        version = (self.cfg.path(self.cfg.data_dir) / rel).with_suffix(".key").read_text()[:8]
        return self._link(f"/late-photo/{day.isoformat()}/{self.late_photo_token(day)}.jpg?v={version}")

    def send_late_message(self, day: dt.date, user: str = "system", auto: bool = False) -> bool:
        """Post the day's late message (as its own message, not in the day's thread), with the
        picture of the late people when there is one: shown in the message when Google Chat can
        fetch it (an https address), otherwise as a link.

        Marks "late_message" only when something was sent, and "late_message_skipped" when nobody
        was late, so the page can tell the two apart. An automatic send gives up when the message
        was marked meanwhile (the admin's "Send now" and the scheduler racing)."""
        text = self.late_text(day)
        if text is None:
            self.store.mark_notice(day, "late_message_skipped", self.now())
            return False
        if not self.store.mark_notice(day, "late_message", self.now()) and auto:
            return False
        cards = None
        # Signed in to the Chat API: posted as the admin, the picture attached (shown in any case).
        api = {"text": text, "image": self.late_photo(day)} if self.chat.api_ready else None
        url = self.late_photo_url(day)
        if url and url.startswith("https://"):
            cards = [{"cardId": f"late-photo-{day.isoformat()}", "card": {"sections": [{"widgets": [
                {"image": {"imageUrl": url, "altText": "The people who came late", "onClick": {"openLink": {"url": url}}}}
            ]}]}}]
        elif url:
            text += f"\n📷 <{url}|Picture of the late people>"
        self.chat.send(text, None, self.now(), cards, api=api)
        self.store.log(self.now(), user, "send late message", ", ".join(a.name for a in self.late_people(day)))
        return True

    # events from the camera pipeline ----------------------------------------------

    def on_identified(self, employee_id: str, ts: dt.datetime, score: float, image: str | None,
                      vector: np.ndarray | None = None, sha256: str | None = None) -> None:
        """The camera identified someone. The face embedding is kept with the sighting, so that
        if the admin later moves it to the right person, that person's face can be learned."""
        ts = self._local(ts)
        result = self.store.record_checkin(ts.date(), employee_id, ts, "camera", score, image,
                                           self.model_id if vector is not None else None, vector, sha256)
        log.info("Identified %s at %s (score %.2f): %s", employee_id, ts.strftime("%H:%M:%S"), score, result)
        if result == "new":
            self._announce(employee_id, ts)

    def on_unknown(self, ts: dt.datetime, image: str | None, vector: np.ndarray | None,
                   guess_id: str | None, guess_score: float | None, sha256: str | None = None,
                   quality: float | None = None) -> int | None:
        ts = self._local(ts)
        again = self._seen_again(vector, guess_id, guess_score, ts.date())
        if again:
            log.info("Unclear face at %s resembles %s (%.2f, next not checked in: %s), who is already checked in; "
                     "not reported", ts.strftime("%H:%M:%S"), guess_id, guess_score, again)
            return None
        group_id = self._ignored_group(vector, ts)
        ignored = group_id is not None
        if ignored:
            log.info("Face at %s belongs to a group ignored today; not reported", ts.strftime("%H:%M:%S"))
        else:
            log.info("Unknown face at %s (closest %s %.2f, group %s)", ts.strftime("%H:%M:%S"), guess_id,
                     guess_score or 0, group_id or "new")
        return self.store.add_unknown(ts.date(), ts, image, self.model_id, vector, guess_id, guess_score,
                                      group_id, ignored=ignored, sha256=sha256, quality=quality)

    def _seen_again(self, vector, guess_id: str | None, guess_score: float | None, day: dt.date) -> str | None:
        """Whether an unclear face is someone already checked in today passing again (not worth a
        review): it resembles them from seen_again_score up, never below accept_score, and clearly
        more than anyone who is not in yet. Without the second condition a face only somewhat like an
        early arrival (a mask, a side view, a newcomer) was dropped once most people were in, and its
        arrival with it; below accept_score the resemblance is too weak to say it is them at all (a
        0.37 match to an early arrival turned out to be a different person walking in).
        Returns a note for the log ("none" or the closest person not in and their score), else None."""
        r = self.cfg.recognition
        floor = max(r.seen_again_score, r.accept_score)
        if not (r.seen_again_score and guess_id and guess_score is not None and guess_score >= floor):
            return None
        checked = self.store.checkins(day)
        if guess_id not in checked:
            return None
        if vector is None:
            return "unknown"
        # Both scores from the same picture: guess_score is the track's best frame against them,
        # which would make anyone else look further away than they are.
        ranked = self.gallery.match(vector, k=len(self.gallery.counts()) or 1)
        theirs = next((s for p, s in ranked if p == guess_id), None)
        rivals = [(p, s) for p, s in ranked if p not in checked]
        if theirs is None or (rivals and theirs - rivals[0][1] < r.min_margin):
            return None  # could just as well be someone not in yet: let the admin decide
        return f"{rivals[0][0]} {rivals[0][1]:.2f}" if rivals else "none"

    def _ignored_group(self, vector: np.ndarray | None, ts: dt.datetime) -> int | None:
        """Match only faces skipped today. Pending tracks stay separate: look-alikes may be different people."""
        if vector is None:
            return None
        v = self.gallery.prepare(vector)  # compared as the gallery compares faces
        candidates = [u for g in self.store.ignored_groups(ts.date()) for u in g]
        best, best_score = None, self.cfg.recognition.accept_score
        within = dt.timedelta(days=self.cfg.recognition.group_days)
        for u in candidates:
            if u.source != "camera" or u.vector is None or u.model != self.model_id or ts - self._local(u.ts) > within:
                continue
            score = float(np.dot(v, self.gallery.prepare(u.vector)))
            if score >= best_score:
                best, best_score = u.group_id, score
        return best

    # admin actions -------------------------------------------------------------

    def review_suggestions(self, members) -> tuple[tuple[str, float] | None, list[tuple[str, float]]]:
        """(review_guess, other people it may be) from one pass over the gallery. With a guess, the
        next two are offered too: on small or blurred camera faces the most similar person was the
        right one about 63% of the time, and one of the three most similar 77% (October 2026)."""
        ranked, matches = self._review_ranking(members)
        guess = self._clear_guess(ranked, matches)
        if guess:
            return guess, [c for c in self._candidates(ranked) if c[0] != guess[0]][:2]
        return None, self._candidates(ranked)

    def review_guess(self, members) -> tuple[str, float] | None:
        """A current, unambiguous suggestion supported by most usable faces in the group."""
        return self._clear_guess(*self._review_ranking(members))

    def _clear_guess(self, ranked, matches) -> tuple[str, float] | None:
        if not ranked:
            return None
        params = self.cfg.recognition
        winner = vote_for(ranked, True, True, params)
        if winner and sum(vote_for(candidates, True, True, params) == winner
                          for candidates in matches) > len(matches) / 2:
            return ranked[0]
        return None

    def review_candidates(self, members, limit: int = 3) -> list[tuple[str, float]]:
        """People this group may be, best first: the group's average similarity to each person, from
        recognition.suggest_score up. Shown on the Review page as "Maybe" choices when there is no
        clear suggestion; unlike review_guess they are not reliable enough to confirm unseen."""
        return self._candidates(self._review_ranking(members)[0], limit)

    def _candidates(self, ranked, limit: int = 3) -> list[tuple[str, float]]:
        return [(person, score) for person, score in ranked[:limit] if score >= self.cfg.recognition.suggest_score]

    def _review_ranking(self, members) -> tuple[list[tuple[str, float]], list]:
        """(active people ranked by average similarity over the group's usable faces, per-face matches)."""
        count = len(self.gallery.counts())
        if not count:
            return [], []
        active = self.store.employees()
        matches = []
        for u in members:
            if not self._usable(u):
                continue
            candidates = [(person, score) for person, score in self.gallery.match(u.vector, k=count)
                          if person in active]
            if candidates:
                matches.append(candidates)
        if not matches:
            return [], []
        totals = {}
        for candidates in matches:
            for person, score in candidates:
                totals[person] = totals.get(person, 0.0) + score
        ranked = sorted(((person, score / len(matches)) for person, score in totals.items()),
                        key=lambda item: item[1], reverse=True)
        return ranked, matches

    def _usable(self, u) -> bool:
        """A review face with an embedding from the current model that can be compared."""
        return (u.model == self.model_id and u.vector is not None
                and bool(np.all(np.isfinite(u.vector))) and np.linalg.norm(u.vector) >= 1e-12)

    def similar_pending(self, confirmed: list) -> list[tuple[list, float]]:
        """Pending camera cards from the same days whose faces look like the photos just confirmed,
        most similar first. Review offers them as "N more cards look like X" after a confirmation:
        a person seen many times (a doorway camera at a distance breaks one visit into several
        tracks) is then named once, not once per card. Faces are compared with the confirmed faces
        directly, not with the gallery, so a poor reference set does not pull strangers in."""
        threshold = self.cfg.recognition.same_face_score
        known = [self.gallery.prepare(u.vector) for u in confirmed if self._usable(u)]
        if not threshold or not known:
            return []
        days = {self._local(u.ts).date() for u in confirmed}
        done = {u.id for u in confirmed}
        found = []
        for group in self.store.pending_groups():
            if group[-1].source == "seed" or any(u.id in done for u in group):
                continue
            if self._local(group[-1].ts).date() not in days:
                continue
            sims = [max(float(v @ self.gallery.prepare(u.vector)) for v in known) for u in group if self._usable(u)]
            if sims and (score := sum(sims) / len(sims)) >= threshold:
                found.append((group, score))
        return sorted(found, key=lambda item: item[1], reverse=True)

    def _pending_group(self, unknown_id: int, photo_ids: list[int] | None = None) -> list:
        u = self.store.unknown(unknown_id)
        members = self.store.group(u.group_id) if u is not None and u.status == "pending" else []
        if not members:
            raise ValueError("this face has already been handled")
        if photo_ids is not None:
            selected = set(photo_ids)
            if not selected or not selected <= {u.id for u in members}:
                raise ValueError("Choose at least one pending photo from this card. Reload if it has changed.")
            members = [u for u in members if u.id in selected]
        return members

    def resolve_unknowns(self, unknown_ids: list[int], employee_id: str, user: str = "admin") -> list:
        """Confirm several whole cards as one person at once (the cards selected on Review, or the
        cards offered as the same person after a confirmation); returns the faces confirmed."""
        if not unknown_ids:
            raise ValueError("Choose at least one review card to confirm")
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        groups = {}
        for unknown_id in unknown_ids:  # all checked before any is confirmed
            members = self._pending_group(unknown_id)
            groups[members[0].group_id] = members
        confirmed = []
        for members in groups.values():
            confirmed += self.resolve_unknown(members[0].id, employee_id, user)
        return confirmed

    def resolve_unknown(self, unknown_id: int, employee_id: str, user: str = "admin", *,
                        photo_ids: list[int] | None = None) -> list:
        """Confirm selected photos (or the whole group when omitted), learn them and record arrivals.
        Returns the faces confirmed."""
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        members = self._pending_group(unknown_id, photo_ids)
        self.store.resolve_group(members[0].group_id, "assigned", employee_id, photo_ids=[u.id for u in members])
        with_vector = [u for u in members if u.vector is not None and u.model]
        # Learn a few faces spread over the group (different visits, angles and light).
        per_group = self.cfg.recognition.learn_per_group
        step = max(1, len(with_vector) // per_group)
        for u in with_vector[::step][:per_group]:
            self._learn(employee_id, u.vector, u.image, u.model)
        for u in members:
            if u.source in ("seed", "collect"):  # a sample, or a face seen outside the camera hours: no arrival
                continue
            ts = self._local(u.ts)
            # Not posted to Chat: Chat is for late arrivals the camera sees and for problems, and the
            # late message and the summary list everyone late, confirmed by the admin or not.
            self.store.record_checkin(ts.date(), employee_id, ts, "admin", u.guess_score, u.image, sha256=u.sha256)
        when = ", ".join(sorted({f"{self._local(u.ts):%Y-%m-%d %H:%M}" for u in members})[:5])
        more = f" (+{len(members) - 5} more)" if len(members) > 5 else ""
        self.store.log(self.now(), user, "confirm face", f"{self._name(employee_id)}: {when}{more}")
        return members

    def _learn(self, employee_id: str, vector: np.ndarray, image: str | None, model: str) -> bool:
        """Keep a face the admin confirmed as a reference photo of `employee_id`."""
        if self.library is not None:
            return self.library.learn(employee_id, vector, image, model)
        self.store.add_embedding(model, employee_id, vector, "admin", self.now())
        if model == self.model_id:
            self.gallery.add(employee_id, vector)
        return True

    def add_employee_from_unknown(self, unknown_id: int, email: str, name: str, user: str = "admin", *,
                                  photo_ids: list[int] | None = None) -> tuple[str, list]:
        """Create a new employee from a face the camera collected, and confirm it as them.
        Returns (employee id, the faces confirmed)."""
        # Keep another request from handling these photos between validation and employee creation.
        with self.store._lock:
            self._pending_group(unknown_id, photo_ids)
            employee_id = self.store.add_employee(email, name)
            self.store.log(self.now(), user, "add employee", f"{email.strip()} {name.strip()} (from camera)")
            return employee_id, self.resolve_unknown(unknown_id, employee_id, user, photo_ids=photo_ids)

    def ignore_unknown(self, unknown_id: int, user: str = "admin") -> None:
        """Hide this face for today only: later sightings today are ignored too, but if the person
        comes back on another day they are shown for review again. Nothing is remembered for good."""
        members = self._pending_group(unknown_id)
        self.store.ignore_group(members[0].group_id, self.now().date())
        self.store.log(self.now(), user, "ignore face (today)",
                       f"{len(members)} photo(s) from {self._local(members[0].ts):%Y-%m-%d %H:%M}")

    def delete_unknown(self, unknown_id: int, user: str = "admin", *, photo_ids: list[int] | None = None) -> None:
        """Discard unusable review photos without labelling a person or deleting source files."""
        self.delete_unknowns([unknown_id], user, photo_ids=photo_ids)

    def delete_unknowns(self, unknown_ids: list[int], user: str = "admin", *, photo_ids: list[int] | None = None) -> None:
        if not unknown_ids:
            raise ValueError("Choose at least one review card to delete")
        groups = {}
        for unknown_id in unknown_ids:
            members = self._pending_group(unknown_id, photo_ids)
            groups[members[0].group_id] = members
        for group_id, members in groups.items():
            self.store.resolve_group(group_id, "discarded", None, photo_ids=[u.id for u in members])
            self.store.log(self.now(), user, "delete review",
                           f"group {group_id}: {len(members)} photo(s) removed from review")

    def restore_unknown(self, unknown_id: int, user: str = "admin") -> None:
        """Undo today's ignore."""
        u = self.store.unknown(unknown_id)
        if u is None or not self.store.restore_group(u.group_id, self.now().date()):
            raise ValueError("this face can no longer be restored")
        self.store.log(self.now(), user, "restore face", f"from {self._local(u.ts):%Y-%m-%d %H:%M}")

    def set_arrival(self, day: dt.date, employee_id: str, time: dt.time, user: str = "admin") -> None:
        """Enter or change an arrival time by hand."""
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        ts = dt.datetime.combine(day, time, tzinfo=self.tz)
        old = self.store.checkins(day).get(employee_id)
        self.store.set_checkin(day, employee_id, ts, "manual", None, old.image if old else None)
        before = f" (was {self._local(old.first_seen):%H:%M})" if old else ""
        self.store.log(self.now(), user, "set arrival", f"{day} {self._name(employee_id)} {time:%H:%M}{before}")

    def reassign_arrival(self, day: dt.date, from_id: str, to_id: str, user: str = "admin") -> bool:
        """The camera recorded the wrong person: move the sighting the arrival came from (time and
        photo) to the right one. The first person's arrival is then recomputed from their other
        sightings, so if they really came later that time counts. Returns whether a face was learned."""
        s = self.store.arrival_sighting(day, from_id)
        if s is None:
            raise ValueError("there is no arrival to move")
        return self.move_sighting(s.id, to_id, user, action="reassign arrival")

    def delete_arrival(self, day: dt.date, employee_id: str, user: str = "admin") -> None:
        c = self.store.delete_checkin(day, employee_id)
        if c is None:
            raise ValueError("there is no arrival to delete")
        self.store.log(self.now(), user, "delete arrival",
                       f"{day} {self._name(employee_id)} {self._local(c.first_seen):%H:%M}")

    def move_sighting(self, sighting_id: int, to_id: str, user: str = "admin", action: str = "move sighting") -> bool:
        """One sighting was someone else. The admin has just confirmed whose face it is, so it is
        also learned as a reference photo of that person (these are the faces the camera found
        hardest). Returns whether a face was learned."""
        s = self.store.sighting(sighting_id)
        if s is None:
            raise ValueError("this sighting no longer exists")
        if to_id not in self.store.employees():
            raise ValueError(f"unknown employee {to_id}")
        if to_id == s.employee_id:
            raise ValueError("choose a different employee")
        learned = (self.cfg.recognition.learn_corrections and s.vector is not None and s.model is not None
                   and self._learn(to_id, s.vector, s.image, s.model))
        self._correct(s, f"{self._name(s.employee_id)} -> {self._name(to_id)}{' (face learned)' if learned else ''}",
                      action, user, employee_id=to_id, status="ok")
        return learned

    def reject_sighting(self, sighting_id: int, user: str = "admin") -> None:
        """One sighting was not this person (and nobody in particular, e.g. a visitor)."""
        s = self.store.sighting(sighting_id)
        if s is None:
            raise ValueError("this sighting no longer exists")
        self._correct(s, self._name(s.employee_id), "reject sighting", user, status="rejected")

    def restore_sighting(self, sighting_id: int, user: str = "admin") -> None:
        s = self.store.sighting(sighting_id)
        if s is None:
            raise ValueError("this sighting no longer exists")
        self._correct(s, self._name(s.employee_id), "restore sighting", user, status="ok")

    def _correct(self, s, what: str, action: str, user: str, **change) -> None:
        """Corrections are not posted to Chat; the late message and the summary show the result."""
        self.store.update_sighting(s.id, **change)
        self.store.log(self.now(), user, action, f"{s.day} {self._local(s.ts):%H:%M} {what}")

    def _announce(self, employee_id: str, ts: dt.datetime) -> None:
        """A camera arrival after the late time, or after the "not seen" list (admin changes are not posted)."""
        day = ts.date()
        if (not self.is_workday(day) or employee_id in self.store.leaves_on(day)
                or employee_id in self.store.exempt_ids()):
            return
        w = self.window(day)
        when = f"{ts:%H:%M}" if day == self.now().date() else f"{ts:%H:%M} on {day:%Y-%m-%d}"
        if not self.cfg.google_chat.notify_late_arrivals:
            return  # the late message lists them all once
        if ts >= w.late_from:
            late = int((ts - w.late_after).total_seconds() // 60)
            self._send(f"🟠 {self.who(employee_id)} arrived at {when}, {late} min late.", day)
        elif self.store.notice_sent(day, "deadline"):
            # They were on the "not seen" list, but turned out to have arrived on time.
            self._send(f"✅ Update: {self.who(employee_id)} arrived at {when}, not late.", day)

    # reports -------------------------------------------------------------------

    def report(self, day: dt.date) -> DayReport:
        w = self.window(day)
        workday, note = self.day_info(day)
        rep = DayReport(day, workday, note, w)
        employees = self.store.employees()
        checkins = self.store.checkins(day)
        leave = self.store.leaves_on(day)
        enrolled = self.gallery.counts()
        exempt = self.store.exempt_ids()
        for emp_id, name in employees.items():
            c = checkins.get(emp_id)
            if emp_id in exempt:
                arrival = Arrival(emp_id, name, self._local(c.first_seen), c.source, c.image, c.score) if c else None
                rep.exempt.append((emp_id, name, arrival))
                continue
            if c is not None:
                a = Arrival(emp_id, name, self._local(c.first_seen), c.source, c.image, c.score)
                (rep.late if a.ts >= w.late_from else rep.on_time).append(a)
            elif emp_id in leave:
                rep.on_leave.append((emp_id, name, LEAVE_KINDS.get(leave[emp_id], leave[emp_id])))
            else:
                rep.not_seen.append((emp_id, name))
            if emp_id not in enrolled:
                rep.no_photos.append((emp_id, name))
        rep.on_time.sort(key=lambda a: a.ts)
        rep.late.sort(key=lambda a: a.ts)
        rep.pending_unknowns = self.store.pending_group_count(day)
        now = self.now()
        rep.outages = self._outages(day, now)
        rep.no_camera_data = (workday and (day < now.date() or now >= w.start)
                              and not self.store.notice_sent(day, "camera_ok"))
        return rep

    def _outages(self, day: dt.date, now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
        """Recorded outages plus the one still open right now, if any."""
        found = [(self._local(a), self._local(b)) for a, b in self.store.outages(day)]
        w = self.window(day)
        if self.is_workday(day) and w.start <= now and day == now.date():
            ref = self._alive_ref(w)
            end = min(now, w.end)
            if ref < w.end and end - ref > GAP:
                found.append((ref, end))
        return found

    def _extra_lines(self, rep: DayReport) -> list[str]:
        lines = []
        stale = leave_api.warning(self.cfg, self.store, self.now())
        if stale:
            lines.append(stale)
        if rep.on_leave:
            lines.append("🏖 On leave: " + ", ".join(f"{self.chat_name(e, n)} ({k})" for e, n, k in rep.on_leave))
        if rep.no_photos:
            lines.append("⚠️ No reference photos, cannot be recognised: " + ", ".join(n for _, n in rep.no_photos))
        if rep.outages:
            spans = ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in rep.outages)
            lines.append(f"⚠️ Camera/system was down: {spans}. Arrivals during this time may be missing.")
        return lines

    def problems_text(self, rep: DayReport) -> str | None:
        """What went wrong during the camera hours, for when the summary is not posted; None if nothing."""
        lines = []
        stale = leave_api.warning(self.cfg, self.store, self.now())
        if stale:
            lines.append(stale)
        if rep.no_camera_data:
            lines.append(f"⚠️ No camera data at all between {rep.window.start:%H:%M} and {rep.window.end:%H:%M}: "
                         "nobody's arrival was recorded.")
        elif rep.outages:
            spans = ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in rep.outages)
            lines.append(f"⚠️ Camera/system was down: {spans}. Arrivals during this time may be missing.")
        if not lines:
            return None
        return "\n".join([f"🛠 *Camera hours {rep.day:%Y-%m-%d}*", *lines, f"Details: {self._link('/')}"])

    def deadline_text(self, rep: DayReport) -> str:
        w = rep.window
        if not rep.not_seen:
            lines = [f"⏰ {w.late_from:%H:%M}: all {rep.total} people arrived on time 🎉"]
        else:
            lines = [f"⏰ *{w.late_from:%H:%M}: {len(rep.not_seen)}/{rep.total} people not seen yet* (counted as late):"]
            lines += [f"• {self.who(emp_id, name)}" for emp_id, name in rep.not_seen]
        lines += self._extra_lines(rep)
        lines.append(f"Details: {self._link('/')}")
        return "\n".join(lines)

    def summary_text(self, rep: DayReport) -> str:
        w = rep.window
        lines = [
            f"📋 *Attendance summary {rep.day:%Y-%m-%d}*",
            f"On time: {len(rep.on_time)} · Late: {len(rep.late)} · Not seen: {len(rep.not_seen)}"
            f" · On leave: {len(rep.on_leave)}",
        ]
        if rep.all_in and rep.last_arrival is not None:
            lines.append(f"✅ Everyone was in by {rep.last_arrival.time} 🎉")
        if rep.late:
            lines.append("*Late:*")
            lines += [f"• {self.who(a.employee_id, a.name)}: {a.time} ({minutes_late(a, w)} min late)"
                      for a in rep.late]
        if rep.not_seen:
            lines.append(f"*Not seen on camera between {w.start:%H:%M} and {w.end:%H:%M}:*")
            lines += [f"• {self.who(emp_id, name)}" for emp_id, name in rep.not_seen]
        lines += self._extra_lines(rep)
        if self.placement is not None:
            lines += self.placement.hints(rep.day, self.cfg.recognition.min_face_px)
        lines.append(f"Details: {self._link('/')}")
        return "\n".join(lines)

    def period_report(self, start: dt.date, end: dt.date) -> PeriodReport:
        """Late and absence totals per employee over a range of days. Today is included once
        the late time has passed; days that are not work days are skipped."""
        now = self.now()
        end = min(end, now.date())
        if end == now.date() and now < self.window(end).late_from:
            end -= dt.timedelta(days=1)
        people: dict[str, PersonSummary] = {}
        days = []
        day = start
        while day <= end:
            rep = self.report(day)
            day += dt.timedelta(days=1)
            if not rep.workday:
                continue
            days.append(rep)
            for a in rep.on_time:
                p = people.setdefault(a.employee_id, PersonSummary(a.employee_id, a.name))
                p.work_days, p.on_time = p.work_days + 1, p.on_time + 1
            for a in rep.late:
                p = people.setdefault(a.employee_id, PersonSummary(a.employee_id, a.name))
                p.work_days, p.late = p.work_days + 1, p.late + 1
                p.late_minutes += minutes_late(a, rep.window)
            if not rep.no_camera_data:
                for emp_id, name in rep.not_seen:
                    p = people.setdefault(emp_id, PersonSummary(emp_id, name))
                    p.work_days, p.not_seen = p.work_days + 1, p.not_seen + 1
            for emp_id, name, _ in rep.on_leave:
                p = people.setdefault(emp_id, PersonSummary(emp_id, name))
                p.on_leave += 1
        ranked = sorted(people.values(), key=lambda p: (-(p.late + p.not_seen), -p.late_minutes, p.name))
        return PeriodReport(start, end, days, ranked)

    def tick(self, camera_ok: bool = True, camera_state: str = "") -> None:
        """Call about once a second from the main loop. `camera_state` says what is wrong when the
        camera is not ok, for the alert."""
        now = self.now()
        day = now.date()
        if self.is_workday(day):
            w = self.window(day)
            self._track_health(now, w, camera_ok, camera_state)

            if (self.cfg.google_chat.late_time_list and now >= w.late_from
                    and not self.store.notice_sent(day, "deadline")
                    and not self.store.notice_sent(day, "deadline_skipped")):
                rep = self.report(day)
                if rep.all_in:
                    # Everyone was in before the late time: a list of nobody is just noise.
                    log.info("Everyone arrived on time; no %s message", w.late_from.strftime("%H:%M"))
                    self.store.mark_notice(day, "deadline_skipped", now)
                else:
                    if now < w.end:  # after window_end the summary covers it
                        self._send(self.deadline_text(rep), day)
                    self.store.mark_notice(day, "deadline", now)

            lm = self.cfg.late_message
            send_at = dt.datetime.combine(day, parse_clock(lm.send_at), tzinfo=self.tz) if lm.send_at else w.end
            if (lm.enabled and lm.auto_send and now >= send_at
                    and not self.store.notice_sent(day, "late_message")
                    and not self.store.notice_sent(day, "late_message_skipped")):
                self.send_late_message(day, auto=True)

            if now >= w.end and not self.store.notice_sent(day, "summary"):
                rep = self.report(day)
                # Without the summary, its warnings (the camera or computer was down, leave data
                # is stale) still go out: they are problems to fix, not attendance news.
                text = self.summary_text(rep) if self.cfg.google_chat.daily_summary else self.problems_text(rep)
                if text:
                    self._send(text, day)
                self.store.mark_notice(day, "summary", now)

        if self.store.get("cleanup_day") != day.isoformat():
            self.cleanup(day)
            self.store.put("cleanup_day", day.isoformat())

        if self.backup is not None and self.backup.due(now):
            self.backup.start()

        if self._last_flush is None or now - self._last_flush >= FLUSH_EVERY:
            self.chat.kick(now)
            self._last_flush = now

    def cleanup(self, today: dt.date) -> int:
        """Delete camera snapshots and unknown faces older than the retention period. Snapshots of
        days on which a person was late are kept for late_evidence_days instead, as evidence."""
        cutoff = today - dt.timedelta(days=self.cfg.retention_days)
        late_cutoff = today - dt.timedelta(days=max(self.cfg.late_evidence_days, self.cfg.retention_days))
        keep: set[tuple[dt.date, str]] = set()
        if late_cutoff < cutoff:
            days = {d for d, _ in self.store.checkins_between(late_cutoff, cutoff - dt.timedelta(days=1))}
            for day in sorted(days):
                keep |= {(day, a.employee_id) for a in self.report(day).late}
        data_dir = self.cfg.path(self.cfg.data_dir)
        removed = 0
        for image in self.store.expire_snapshots(cutoff, keep):
            if not image.startswith("crops/"):
                continue
            path = data_dir / image
            if path.exists():
                path.unlink()
                removed += 1
            for extra in (body_photo(image), scene_photo(image)):
                (data_dir / extra).unlink(missing_ok=True)
        crops = data_dir / "crops"
        if crops.exists():
            for folder in crops.iterdir():  # folders are named by day
                if folder.is_dir() and folder.name < cutoff.isoformat() and not any(folder.iterdir()):
                    folder.rmdir()
        removed += latephoto.expire(data_dir, late_cutoff)
        if removed:
            log.info("Retention: deleted %d snapshots older than %s", removed, cutoff)
        return removed

    def _alive_ref(self, w: Window) -> dt.datetime:
        """Last moment we know the camera was working today, never earlier than window start."""
        last = self._last_alive
        if last is None or self._local(last) < w.start:
            return w.start
        return self._local(last)

    def _track_health(self, now: dt.datetime, w: Window, camera_ok: bool, camera_state: str = "") -> None:
        day = now.date()
        if now < w.start:
            return
        ref = self._alive_ref(w)
        if now >= w.end:
            # Close an outage that was still open when the window ended (also covers a PC that was off).
            if ref < w.end:
                if w.end - ref > GAP:
                    self.store.add_outage(day, ref, w.end)
                self._set_alive(w.end, force=True)
            self._bad_since = None
            if self.store.get(CAMERA_ALERT_KEY):
                self.store.put(CAMERA_ALERT_KEY, "")
            return
        if camera_ok:
            if now - ref > GAP:
                self.store.add_outage(day, ref, now)
            self._set_alive(now, force=now - ref > GAP)
            if self._camera_seen_day != day:  # remember that the camera worked on this day
                self.store.mark_notice(day, "camera_ok", now)
                self._camera_seen_day = day
            self._bad_since = None
            if self.store.get(CAMERA_ALERT_KEY):
                self._send(f"✅ The camera is working again ({now:%H:%M}).", day)
                self.store.put(CAMERA_ALERT_KEY, "")
            return
        # Wait a while in this run of the program too, so a restart or a short hiccup is not reported.
        self._bad_since = self._bad_since or now
        if now - self._bad_since >= CAMERA_ALERT_AFTER and not self.store.get(CAMERA_ALERT_KEY):
            reason = f" ({camera_state})" if camera_state else ""
            self._send(f"⚠️ *The camera is not working* since {ref:%H:%M}{reason}. Arrivals are not being recorded. "
                       f"Check the camera's power and network, and its address in Settings: {self._link('/live')}", day)
            self.store.put(CAMERA_ALERT_KEY, now.isoformat())

    def _set_alive(self, ts: dt.datetime, force: bool = False) -> None:
        self._last_alive = ts
        if force or self._last_alive_saved is None or ts - self._last_alive_saved >= ALIVE_SAVE_EVERY:
            self.store.put("last_alive", ts.isoformat())
            self._last_alive_saved = ts
