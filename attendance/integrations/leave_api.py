"""Holidays, extra work days and employee leave, fetched from the HR system.

Request:  GET <leave_api.url>?from=YYYY-MM-DD&to=YYYY-MM-DD
          with the header "Authorization: Bearer <leave_api.token>" when a token is set.
Response (JSON):
    {
      "holidays": [{"date": "2026-10-20", "name": "Women's Day"}],
      "workdays": [{"date": "2026-10-24", "name": "Make-up day"}],
      "leaves":   [{"email": "person@example.com", "start": "2026-10-15", "end": "2026-10-16",
                    "type": "leave", "period": "full", "note": "Family"}]
    }
- "end" defaults to "start"; "type" is free text (leave, sick, business_trip, remote, ...).
- "period" is "full" (default), "morning" or "afternoon". Attendance is checked in the morning,
  so afternoon-only leave is ignored.
If the HR system answers in a different format, adapt `parse()`; nothing else needs to change.

The data is copied into the local database, so attendance keeps working when the HR system
is unreachable; the last successful update time is shown on the admin page and in chat.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ..core.config import Config
from ..store import Store, normalize_email

log = logging.getLogger(__name__)

STATUS_KEY = "leave_sync"
RETRY_AFTER_FAILURE = dt.timedelta(minutes=2)
BEFORE_DEADLINE = dt.timedelta(minutes=5)  # refresh once more just before the late list goes out
STALE_AFTER = dt.timedelta(hours=24)


def _get_json(url: str, token: str) -> object:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _date(value) -> dt.date:
    return dt.date.fromisoformat(str(value)[:10])


def parse(data: object) -> tuple[list[tuple[dt.date, str, str]], list[tuple[str, dt.date, dt.date, str, str]], int]:
    """Return (calendar days, leaves, number of rows skipped as invalid)."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object with holidays, workdays and leaves")
    days, leaves, skipped = [], [], 0
    for key, kind in (("holidays", "holiday"), ("workdays", "workday")):
        for row in data.get(key) or []:
            try:
                days.append((_date(row["date"]), kind, str(row.get("name") or "")))
            except (KeyError, TypeError, ValueError):
                skipped += 1
    for row in data.get("leaves") or []:
        try:
            if str(row.get("period") or "full").lower() == "afternoon":
                continue
            start = _date(row["start"])
            end = _date(row.get("end") or row["start"])
            email = normalize_email(str(row["email"]))
            if end < start:
                raise ValueError
            leaves.append((email, start, end, str(row.get("type") or "leave"),
                           str(row.get("note") or "")))
        except (KeyError, TypeError, ValueError, AttributeError):
            skipped += 1
    return days, leaves, skipped


def read_status(store: Store) -> dict:
    """{"at", "ok", "message", "last_success"}; timestamps are ISO strings."""
    return json.loads(store.get(STATUS_KEY) or "{}")


class LeaveSync:
    def __init__(self, cfg: Config, store: Store, now: Callable[[], dt.datetime],
                 late_after: Callable[[dt.date], dt.datetime],
                 fetch: Callable[[str, str], object] = _get_json):
        self.cfg = cfg
        self.store = store
        self._now = now
        self._late_after = late_after
        self._fetch = fetch

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.leave_api.url.strip())

    def due(self) -> bool:
        if not self.enabled:
            return False
        now = self._now()
        status = read_status(self.store)
        if not status.get("at"):
            return True
        last = dt.datetime.fromisoformat(status["at"])
        if now - last >= (dt.timedelta(minutes=self.cfg.leave_api.refresh_minutes) if status.get("ok")
                          else RETRY_AFTER_FAILURE):
            return True
        deadline = self._late_after(now.date())
        return deadline - BEFORE_DEADLINE <= now < deadline and last < deadline - BEFORE_DEADLINE

    def sync(self) -> dict:
        now = self._now()
        api = self.cfg.leave_api
        start = now.date() - dt.timedelta(days=api.days_back)
        end = now.date() + dt.timedelta(days=api.days_ahead)
        status = read_status(self.store)
        sep = "&" if "?" in api.url else "?"
        url = f"{api.url.strip()}{sep}{urllib.parse.urlencode({'from': start.isoformat(), 'to': end.isoformat()})}"
        try:
            days, leaves, skipped = parse(self._fetch(url, api.token.strip()))
        except urllib.error.HTTPError as exc:
            message = f"HR system answered HTTP {exc.code}"
        except Exception as exc:  # network error, timeout, invalid JSON or format
            message = f"{type(exc).__name__}: {exc}"
        else:
            resolved = []
            for email, first, last, kind, note in leaves:
                person = self.store.employee_by_email(email)
                if person is None:
                    skipped += 1
                else:
                    resolved.append((person.id, first, last, kind, note))
            self.store.replace_external(start, end, days, resolved)
            message = f"{len(days)} calendar days, {len(resolved)} leave entries"
            if skipped:
                message += f", {skipped} invalid rows skipped"
            status = {"at": now.isoformat(), "ok": True, "message": message, "last_success": now.isoformat()}
            self.store.put(STATUS_KEY, json.dumps(status))
            log.info("Leave data updated: %s", message)
            return status
        log.warning("Leave data update failed: %s", message)
        status = {**status, "at": now.isoformat(), "ok": False, "message": message}
        self.store.put(STATUS_KEY, json.dumps(status))
        return status


def warning(cfg: Config, store: Store, now: dt.datetime) -> str | None:
    """A warning line when leave data is configured but not current."""
    if not cfg.leave_api.url.strip():
        return None
    status = read_status(store)
    last = status.get("last_success")
    if last is None:
        return "⚠️ Leave data could not be loaded from the HR system yet; people on leave may be listed as late."
    last_dt = dt.datetime.fromisoformat(last).astimezone(now.tzinfo)
    if not status.get("ok") or now - last_dt > STALE_AFTER:
        return (f"⚠️ Leave data from the HR system is from {last_dt:%Y-%m-%d %H:%M} (latest update failed); "
                f"recent leave may be missing.")
    return None
