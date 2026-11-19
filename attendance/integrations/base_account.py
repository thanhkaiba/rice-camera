"""Birthdays from Base Account, for the birthday greeting at the door.

Base's user list holds far more than a birthday (phone, address, devices, tokens): only the user name and
the day and month of birth are taken from the answer; nothing else is kept, logged or shown, and never the
year. The token is an Account token (V2), a different one from the Timeoff token, and stays on the server.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
URL = "https://account.base.vn/extapi/v1/users"
STATUS_KEY = "base_birthdays"  # {"at", "ok", "message", "given": {employee id: "MM-DD"}}
EVERY = dt.timedelta(hours=24)
RETRY = dt.timedelta(hours=1)


def _post(form: dict) -> dict:
    request = urllib.request.Request(URL, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"}, method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        return json.load(response)


def parse(data) -> dict[str, str]:
    """Base's answer -> user name -> "MM-DD"; people without a real day and month are left out."""
    if not isinstance(data, dict) or str(data.get("code")) != "1" or not isinstance(data.get("users"), list):
        raise ValueError("Base rejected the request; check that the token is an Account token (V2)")
    found = {}
    for user in data["users"]:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "").strip().lstrip("@").lower()
        dob = user.get("dob")
        if not username or not isinstance(dob, dict):
            continue
        try:
            day, month = int(dob.get("day") or 0), int(dob.get("month") or 0)
            dt.date(2024, month, day)  # a leap year: 29 February is a birthday too
        except (TypeError, ValueError):
            continue
        found[username] = f"{month:02d}-{day:02d}"
    return found


def fetch(token: str, post=_post) -> dict[str, str]:
    return parse(post({"access_token_v2": token}))


def status(store) -> dict:
    try:
        return json.loads(store.get(STATUS_KEY) or "{}")
    except ValueError:
        return {}


def due(cfg, store, now: dt.datetime) -> bool:
    if not cfg.base_account_key:
        return False
    last = status(store)
    if not last.get("at"):
        return True
    return now - dt.datetime.fromisoformat(last["at"]) >= (EVERY if last.get("ok") else RETRY)


def sync(cfg, store, now: dt.datetime, post=_post) -> dict:
    """Takes the birthdays from Base for the people with a Base username. A birthday typed on the People page
    is kept: Base fills an empty one, and replaces only what Base itself gave before."""
    last = status(store)
    given = dict(last.get("given") or {})
    try:
        found = fetch(cfg.base_account_key, post)
    except Exception as exc:
        # Never the exception text: an error from the network layer can carry the request, with the token.
        message = str(exc) if isinstance(exc, ValueError) else f"Base Account could not be reached ({type(exc).__name__})"
        result = {"at": now.isoformat(), "ok": False, "message": message, "given": given}
        log.warning("Birthdays from Base failed (%s)", type(exc).__name__)
        store.put(STATUS_KEY, json.dumps(result))
        return result
    matched = changed = 0
    for person in store.all_employees():
        birthday = found.get(person.base_username.lower()) if person.base_username else None
        if not birthday:
            continue
        matched += 1
        if person.birthday in ("", given.get(person.id)) and person.birthday != birthday:
            store.set_birthday(person.id, birthday)
            changed += 1
        if person.birthday in ("", given.get(person.id), birthday):
            given[person.id] = birthday
    result = {"at": now.isoformat(), "ok": True, "given": given,
              "message": f"{matched} people with a birthday in Base, {changed} updated"}
    store.put(STATUS_KEY, json.dumps(result))
    return result
