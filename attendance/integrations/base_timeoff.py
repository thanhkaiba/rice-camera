"""Base Timeoff V2: exact-day morning permissions and upcoming leave."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.parse
import urllib.request

from ..core.config import parse_clock

log = logging.getLogger(__name__)
VN = dt.timezone(dt.timedelta(hours=7))
URL = 'https://timeoff.base.vn/extapi/v1/timeoff/list'
KINDS = {'1967': 'Holiday', '2017': 'Unpaid leave', '1978': 'WFH',
         '1977': 'Coming late', '1975': 'Leave early'}


def _post(form):
    request = urllib.request.Request(URL, data=urllib.parse.urlencode(form).encode(),
                                    headers={'Content-Type': 'application/x-www-form-urlencoded',
                                             'Accept': 'application/json'}, method='POST')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        return json.load(response)


def parse(rows, day: dt.date, late_after: dt.time, *, approved: bool, morning_only: bool = True) -> dict:
    result = {}
    for row in rows:
        username = row['username'].strip().lstrip('@').lower()
        group = str(row['group_id'])
        if not username or not isinstance(row['shifts'], list):
            raise ValueError('invalid Base username or shifts')
        if group == '1975':
            continue
        for shift_day in row['shifts']:
            date = dt.datetime.fromtimestamp(int(shift_day['datetime']), VN).date()
            if date != day:
                continue
            if not isinstance(shift_day['shifts'], list) or not shift_day['shifts']:
                raise ValueError('missing Base shift values')
            for shift in shift_day['shifts']:
                value = str(shift['value']).strip()
                label = value.casefold()
                if label in ('full day', 'morning'):
                    morning = True
                elif label == 'afternoon':
                    morning = False
                else:
                    match = re.fullmatch(r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})', value)
                    if not match:
                        raise ValueError('unrecognized Base shift value; attendance deferred')
                    start, end = (parse_clock(v) for v in match.groups())
                    if end <= start:
                        raise ValueError('invalid Base shift interval')
                    morning = start < dt.time(12) if group == '1977' else start <= late_after < end
                if morning_only and not morning:
                    continue
                kind = KINDS.get(group)
                if kind is None:
                    kind = f'Unknown Base group {group}; HR review required'
                    log.warning('Unknown Base Timeoff group %s; HR review required', group)
                entry = {'kind': kind, 'absence': group != '1977', 'approved': approved, 'shift': value}
                if entry not in result.setdefault(username, []):
                    result[username].append(entry)
    return result


def _pages(start: dt.date, end: dt.date, token: str, post):
    for status in ('approved', 'pending'):
        page, seen, previous = 0, 0, set()
        while page < 1000:
            data = post({'access_token_v2': token, 'start_date_to': end.strftime('%d/%m/%Y'),
                         'end_date_from': start.strftime('%d/%m/%Y'), 's': status, 'page': page})
            if not isinstance(data, dict) or str(data.get('code')) != '1':
                raise ValueError('Base rejected the request; check token V2 validity and Timeoff scope')
            rows = data.get('timeoffs')
            if not isinstance(rows, list):
                raise ValueError('missing Base timeoffs list')
            total = int(data['total_items'])
            if int(data['page']) != page or total < 0:
                raise ValueError('invalid Base pagination')
            fingerprint = json.dumps(rows, sort_keys=True)
            if rows and fingerprint in previous:
                raise ValueError('Base repeated a page; attendance deferred')
            previous.add(fingerprint)
            yield rows, status == 'approved'
            seen += len(rows)
            if seen >= total:
                break
            if not rows:
                raise ValueError('incomplete Base pagination')
            page += 1
        else:
            raise ValueError('Base pagination exceeded limit')


def fetch(day: dt.date, late_after: dt.time, token: str, *, post=_post) -> dict:
    """Fetch only approved and pending requests; an incomplete response never replaces the cache."""
    result = {}
    for rows, approved in _pages(day, day, token, post):
        for username, entries in parse(rows, day, late_after, approved=approved).items():
            result.setdefault(username, []).extend(entries)
    return result


def fetch_upcoming(start: dt.date, end: dt.date, token: str, *, post=_post) -> dict:
    """Approved and pending leave, including afternoon shifts, without granting attendance permissions."""
    result = {}
    if end < start:
        return result
    for rows, approved in _pages(start, end, token, post):
        dates = {dt.datetime.fromtimestamp(int(shift['datetime']), VN).date()
                 for row in rows for shift in row['shifts']}
        for day in sorted(d for d in dates if start <= d <= end):
            parsed = parse(rows, day, dt.time(12), approved=approved, morning_only=False)
            for username, entries in parsed.items():
                for entry in entries:
                    if entry['absence']:
                        value = {'day': day.isoformat(), **entry}
                        if value not in result.setdefault(username, []):
                            result[username].append(value)
    for entries in result.values():
        entries.sort(key=lambda entry: entry['day'])
    return result


def permissions(store, day: dt.date) -> tuple[dict, dict, dict]:
    """Approved arrival permissions (label, deadline), approved absences, and all request labels."""
    snapshot = json.loads(store.get(f'base_timeoffs:{day}') or '{}')
    late, absent, requests = {}, {}, {}
    for person in store.all_employees():
        entries = snapshot.get(person.base_username, []) if person.base_username else []
        for entry in entries:
            label = entry['kind'] + ('' if entry['approved'] else ' (pending)') + ': ' + entry['shift']
            requests[person.id] = requests[person.id] + '; ' + label if person.id in requests else label
            if not entry['approved']:
                continue
            if entry['absence']:
                absent[person.id] = label
                late[person.id] = (label, None)
            else:
                interval = re.fullmatch(r'\d{1,2}:\d{2}\s*-\s*(\d{1,2}:\d{2})', entry['shift'])
                if interval:
                    until = parse_clock(interval[1])
                    if person.id not in late or (late[person.id][1] is not None and until > late[person.id][1]):
                        late[person.id] = (label, until)
    return late, absent, requests
