"""Attendance rules, admin corrections and scheduled Google Chat notices.

Rules for a work day (times from config.schedule):
- First seen before late_after (e.g. 10:00): on time. Being seen again later changes nothing.
- First seen at or after late_after: late, and announced as soon as they are seen.
- At late_after: one alert listing everyone not seen yet.
- At window_end (e.g. 11:00): daily summary.
- People on leave are never counted as late or missing; holidays send nothing.
- Exempt people (e.g. the boss) have their arrival recorded but are never counted as late or missing.
- A camera that stops working during the camera hours is reported, and so is its recovery.
- Faces the system could not identify are reported to the admin with a link to review them.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import numpy as np

from ..integrations import leave_api
from .config import Config, parse_clock
from ..vision.gallery import Gallery
from ..vision.tracker import vote_for
from ..integrations.notifier import GoogleChat
from ..store import LEAVE_KINDS, Store, body_photo, scene_photo

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
        self._last_unknown_notice: dt.datetime | None = None
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

    def send_late_message(self, day: dt.date, user: str = "system", auto: bool = False) -> bool:
        """Post the day's late message (as its own message, not in the day's thread).

        Marks "late_message" only when something was sent, and "late_message_skipped" when nobody
        was late, so the page can tell the two apart. An automatic send gives up when the message
        was marked meanwhile (the admin's "Send now" and the scheduler racing)."""
        text = self.late_text(day)
        if text is None:
            self.store.mark_notice(day, "late_message_skipped", self.now())
            return False
        if not self.store.mark_notice(day, "late_message", self.now()) and auto:
            return False
        self.chat.send(text, None, self.now())
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
            self._announce(employee_id, ts, suffix="")

    def on_unknown(self, ts: dt.datetime, image: str | None, vector: np.ndarray | None,
                   guess_id: str | None, guess_score: float | None, sha256: str | None = None) -> int | None:
        ts = self._local(ts)
        seen_again = self.cfg.recognition.seen_again_score  # 0 switches this off
        if (guess_id and guess_score is not None and seen_again and guess_score >= seen_again
                and guess_id in self.store.checkins(ts.date())):
            log.info("Unclear face at %s resembles %s, who is already checked in; not reported",
                     ts.strftime("%H:%M:%S"), guess_id)
            return None
        group_id, ignored = self._find_group(vector, ts)
        if ignored:
            log.info("Face at %s belongs to a group ignored today; not reported", ts.strftime("%H:%M:%S"))
        else:
            log.info("Unknown face at %s (closest %s %.2f, group %s)", ts.strftime("%H:%M:%S"), guess_id,
                     guess_score or 0, group_id or "new")
        return self.store.add_unknown(ts.date(), ts, image, self.model_id, vector, guess_id, guess_score,
                                      group_id, ignored=ignored, sha256=sha256)

    def _find_group(self, vector: np.ndarray | None, ts: dt.datetime) -> tuple[int | None, bool]:
        """(group, ignored): the group of the most similar unknown face if it is clearly the same person,
        and whether that group was ignored today. This way someone not enrolled yet shows up once to
        review however often they pass, and a visitor ignored today stays hidden for the rest of the day."""
        if vector is None:
            return None, False
        v = self.gallery.prepare(vector)  # compared as the gallery compares faces
        candidates = [(u, False) for u in self.store.unknowns()]
        candidates += [(u, True) for g in self.store.ignored_groups(ts.date()) for u in g]
        best, best_ignored, best_score = None, False, self.cfg.recognition.accept_score
        within = dt.timedelta(days=self.cfg.recognition.group_days)
        for u, ignored in candidates:
            if u.source != "camera" or u.vector is None or u.model != self.model_id or ts - self._local(u.ts) > within:
                continue
            score = float(np.dot(v, self.gallery.prepare(u.vector)))
            if score >= best_score:
                best, best_ignored, best_score = u.group_id, ignored, score
        return best, best_ignored

    # admin actions -------------------------------------------------------------

    def review_guess(self, members) -> tuple[str, float] | None:
        """A current, unambiguous suggestion supported by most usable faces in the group."""
        count = len(self.gallery.counts())
        if not count:
            return None
        active = self.store.employees()
        matches = []
        for u in members:
            if (u.model != self.model_id or u.vector is None
                    or not np.all(np.isfinite(u.vector)) or np.linalg.norm(u.vector) < 1e-12):
                continue
            candidates = [(person, score) for person, score in self.gallery.match(u.vector, k=count)
                          if person in active]
            if candidates:
                matches.append(candidates)
        if not matches:
            return None
        totals = {}
        for candidates in matches:
            for person, score in candidates:
                totals[person] = totals.get(person, 0.0) + score
        ranked = sorted(((person, score / len(matches)) for person, score in totals.items()),
                        key=lambda item: item[1], reverse=True)
        params = self.cfg.recognition
        winner = vote_for(ranked, True, True, params)
        if winner and sum(vote_for(candidates, True, True, params) == winner
                          for candidates in matches) > len(matches) / 2:
            return ranked[0]
        return None

    def _pending_group(self, unknown_id: int) -> list:
        u = self.store.unknown(unknown_id)
        members = self.store.group(u.group_id) if u is not None and u.status == "pending" else []
        if not members:
            raise ValueError("this face has already been handled")
        return members

    def resolve_unknown(self, unknown_id: int, employee_id: str, user: str = "admin") -> None:
        """Admin says this face (and every face grouped with it) is `employee_id`: learn the face and
        record the arrivals."""
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        members = self._pending_group(unknown_id)
        self.store.resolve_group(members[0].group_id, "assigned", employee_id)
        with_vector = [u for u in members if u.vector is not None and u.model]
        # Learn a few faces spread over the group (different visits, angles and light).
        per_group = self.cfg.recognition.learn_per_group
        step = max(1, len(with_vector) // per_group)
        for u in with_vector[::step][:per_group]:
            self._learn(employee_id, u.vector, u.image, u.model)
        today = self.now().date()
        for u in members:
            if u.source == "seed":
                continue
            ts = self._local(u.ts)
            result = self.store.record_checkin(ts.date(), employee_id, ts, "admin", u.guess_score, u.image,
                                               sha256=u.sha256)
            # Earlier days are corrected quietly; only today's arrival is posted.
            if result in ("new", "earlier") and ts.date() == today:
                self._announce(employee_id, ts, suffix=" (confirmed by admin)")
        when = ", ".join(sorted({f"{self._local(u.ts):%Y-%m-%d %H:%M}" for u in members})[:5])
        more = f" (+{len(members) - 5} more)" if len(members) > 5 else ""
        self.store.log(self.now(), user, "confirm face", f"{self._name(employee_id)}: {when}{more}")

    def _learn(self, employee_id: str, vector: np.ndarray, image: str | None, model: str) -> bool:
        """Keep a face the admin confirmed as a reference photo of `employee_id`."""
        if self.library is not None:
            return self.library.learn(employee_id, vector, image, model)
        self.store.add_embedding(model, employee_id, vector, "admin", self.now())
        if model == self.model_id:
            self.gallery.add(employee_id, vector)
        return True

    def add_employee_from_unknown(self, unknown_id: int, email: str, name: str, user: str = "admin") -> None:
        """Create a new employee from a face the camera collected, and confirm it as them."""
        self._pending_group(unknown_id)
        employee_id = self.store.add_employee(email, name)
        self.store.log(self.now(), user, "add employee", f"{email.strip()} {name.strip()} (from camera)")
        self.resolve_unknown(unknown_id, employee_id, user)

    def ignore_unknown(self, unknown_id: int, user: str = "admin") -> None:
        """Hide this face for today only: later sightings today are ignored too, but if the person
        comes back on another day they are shown for review again. Nothing is remembered for good."""
        members = self._pending_group(unknown_id)
        self.store.ignore_group(members[0].group_id, self.now().date())
        self.store.log(self.now(), user, "ignore face (today)",
                       f"{len(members)} photo(s) from {self._local(members[0].ts):%Y-%m-%d %H:%M}")

    def delete_unknown(self, unknown_id: int, user: str = "admin") -> None:
        """Discard unusable review photos without labelling a person or deleting source files."""
        self.delete_unknowns([unknown_id], user)

    def delete_unknowns(self, unknown_ids: list[int], user: str = "admin") -> None:
        if not unknown_ids:
            raise ValueError("Choose at least one review card to delete")
        groups = {}
        for unknown_id in unknown_ids:
            members = self._pending_group(unknown_id)
            groups[members[0].group_id] = members
        for group_id, members in groups.items():
            self.store.resolve_group(group_id, "discarded", None)
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
        self._announce(employee_id, ts, suffix=" (entered by admin)")

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
        people = list(dict.fromkeys([s.employee_id, change.get("employee_id") or s.employee_id]))
        before = {e: self.store.checkins(s.day).get(e) for e in people}
        self.store.update_sighting(s.id, **change)
        self.store.log(self.now(), user, action, f"{s.day} {self._local(s.ts):%H:%M} {what}")
        after = self.store.checkins(s.day)
        for e in people:
            self._announce_change(e, before[e], after.get(e))

    def _announce_change(self, employee_id: str, before, after) -> None:
        """After a correction, post only what changes someone's status: they turn out to be late
        (or on time after being listed as not seen)."""
        if after is None or (before is not None and after.first_seen == before.first_seen):
            return
        ts = self._local(after.first_seen)
        if ts.date() != self.now().date():
            return  # earlier days are corrected quietly
        late = ts >= self.window(ts.date()).late_after
        was_late = before is not None and self._local(before.first_seen) >= self.window(ts.date()).late_after
        if late != was_late or before is None:
            self._announce(employee_id, ts, suffix=" (corrected by admin)")

    def _announce(self, employee_id: str, ts: dt.datetime, suffix: str) -> None:
        day = ts.date()
        if (not self.is_workday(day) or employee_id in self.store.leaves_on(day)
                or employee_id in self.store.exempt_ids()):
            return
        w = self.window(day)
        when = f"{ts:%H:%M}" if day == self.now().date() else f"{ts:%H:%M} on {day:%Y-%m-%d}"
        if ts >= w.late_after:
            if self.cfg.google_chat.notify_late_arrivals:
                late = int((ts - w.late_after).total_seconds() // 60)
                self._send(f"🟠 {self.who(employee_id)} arrived at {when}, {late} min late{suffix}.", day)
        elif self.store.notice_sent(day, "deadline"):
            # They were on the "not seen" list, but turned out to have arrived on time.
            self._send(f"✅ Update: *{self.chat_name(employee_id)}* arrived at {when}{suffix}, not late.", day)

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
                (rep.late if a.ts >= w.late_after else rep.on_time).append(a)
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

    def deadline_text(self, rep: DayReport) -> str:
        w = rep.window
        if not rep.not_seen:
            lines = [f"⏰ {w.late_after:%H:%M}: all {rep.total} people arrived on time 🎉"]
        else:
            lines = [f"⏰ *{w.late_after:%H:%M}: {len(rep.not_seen)}/{rep.total} people not seen yet* (counted as late):"]
            lines += [f"• {self.who(emp_id, name)}" for emp_id, name in rep.not_seen]
        if rep.pending_unknowns:
            lines.append(f"❓ {rep.pending_unknowns} unidentified faces, possibly people on this list · "
                         f"{self._link('/unknowns?review=first', 'Review')}")
        lines += self._extra_lines(rep)
        lines.append(f"Details: {self._link('/')}")
        return "\n".join(lines)

    def summary_text(self, rep: DayReport) -> str:
        w = rep.window
        lines = [
            f"📋 *Attendance summary {rep.day:%Y-%m-%d}*",
            f"On time: {len(rep.on_time)} · Late: {len(rep.late)} · Not seen: {len(rep.not_seen)}"
            f" · On leave: {len(rep.on_leave)} · Unidentified faces: {rep.pending_unknowns}",
        ]
        if rep.all_in and rep.last_arrival is not None:
            lines.append(f"✅ Everyone was in by {rep.last_arrival.time} 🎉")
        if rep.late:
            lines.append("*Late:*")
            lines += [f"• {self.chat_name(a.employee_id, a.name)}: {a.time} ({minutes_late(a, w)} min late)"
                      for a in rep.late]
        if rep.not_seen:
            lines.append(f"*Not seen on camera between {w.start:%H:%M} and {w.end:%H:%M}:*")
            lines += [f"• {self.chat_name(emp_id, name)}" for emp_id, name in rep.not_seen]
        if rep.pending_unknowns:
            lines.append(f"❓ {rep.pending_unknowns} unidentified faces to review · "
                         f"{self._link('/unknowns?review=first', 'Review')}")
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
        if end == now.date() and now < self.window(end).late_after:
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

    def unknown_text(self, unknowns) -> str:
        people = len({u.group_id for u in unknowns})
        times = ", ".join(self._local(u.ts).strftime("%H:%M") for u in unknowns[:10])
        who = "1 face" if people == 1 else f"{people} different faces"
        return (f"❓ {who} to review, seen at {times}{'…' if len(unknowns) > 10 else ''} · "
                f"{self._link('/unknowns?review=first', 'Say who it is')}")

    # scheduler -----------------------------------------------------------------

    def tick(self, camera_ok: bool = True, camera_state: str = "") -> None:
        """Call about once a second from the main loop. `camera_state` says what is wrong when the
        camera is not ok, for the alert."""
        now = self.now()
        day = now.date()
        if self.is_workday(day):
            w = self.window(day)
            self._track_health(now, w, camera_ok, camera_state)

            if (now >= w.late_after and not self.store.notice_sent(day, "deadline")
                    and not self.store.notice_sent(day, "deadline_skipped")):
                rep = self.report(day)
                if rep.all_in:
                    # Everyone was in before the late time: a list of nobody is just noise.
                    log.info("Everyone arrived on time; no %s message", w.late_after.strftime("%H:%M"))
                    self.store.mark_notice(day, "deadline_skipped", now)
                else:
                    if now < w.end:  # after window_end the summary covers it
                        self._send(self.deadline_text(rep), day)
                    self.store.mark_notice(day, "deadline", now)

            batch = dt.timedelta(minutes=self.cfg.google_chat.unknown_batch_minutes)
            if now < w.end and (self._last_unknown_notice is None or now - self._last_unknown_notice >= batch):
                pending = self.store.unnotified_unknowns(day)
                if pending:
                    self._send(self.unknown_text(pending), day)
                    self.store.mark_unknowns_notified([u.id for u in pending])
                    self._last_unknown_notice = now

            lm = self.cfg.late_message
            send_at = dt.datetime.combine(day, parse_clock(lm.send_at), tzinfo=self.tz) if lm.send_at else w.end
            if (lm.enabled and lm.auto_send and now >= send_at
                    and not self.store.notice_sent(day, "late_message")
                    and not self.store.notice_sent(day, "late_message_skipped")):
                self.send_late_message(day, auto=True)

            if now >= w.end and not self.store.notice_sent(day, "summary"):
                self.store.mark_unknowns_notified([u.id for u in self.store.unnotified_unknowns(day)])
                self._send(self.summary_text(self.report(day)), day)
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
