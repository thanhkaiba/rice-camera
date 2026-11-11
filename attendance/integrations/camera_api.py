"""Report confirmed late arrivals; local attendance and delivery notices survive restarts."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import urllib.request
from zoneinfo import ZoneInfo

from . import leave_api
from ..core.config import parse_clock
from ..core.service import minutes_late

log = logging.getLogger(__name__)
STATUS_KEY = 'camera_api_status'
VN = ZoneInfo('Asia/Ho_Chi_Minh')


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'text/plain;charset=utf-8'}, method='POST')
    # urllib follows Apps Script's 302 with a GET, without submitting the event a second time.
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError('Invalid Camera API response')
    return data


def ping(url: str, key: str) -> None:
    if not url or not key:
        raise ValueError('Enter the Camera API URL and key first.')
    from ..core.config import CameraApiConfig, Config
    cfg = Config(camera_api=CameraApiConfig(url=url, key=key))
    cfg.validate()
    try:
        response = post(cfg.camera_api.url, {'action': 'camera.ping', 'secret': cfg.camera_api.key})
        if response.get('ok') is not True:
            raise ValueError()
    except Exception:
        raise ValueError('Camera API connection failed; check the URL, key and server deployment.') from None


class CameraSync:
    def __init__(self, cfg, store, service):
        self.cfg, self.store, self.service = cfg, store, service
        self._syncing = threading.Lock()

    def status(self) -> dict:
        return json.loads(self.store.get(STATUS_KEY) or '{}')

    def _status(self, now, ok: bool, message: str) -> dict:
        status = {'at': now.isoformat(), 'ok': ok, 'message': message}
        self.store.put(STATUS_KEY, json.dumps(status))
        if not ok:
            log.warning('Camera API: %s', message)
        return status

    def sync(self) -> dict:
        if not self._syncing.acquire(blocking=False):
            return {'ok': False, 'message': 'A Camera API delivery is already running. Try again shortly.'}
        try:
            return self._sync()
        finally:
            self._syncing.release()

    def _sync(self) -> dict:
        api = self.cfg.camera_api
        if not api.url or not api.key:
            return {'ok': False, 'message': 'Save the Camera API URL and key in Settings first.'}
        now = self.service.now()
        today = now.astimezone(VN).date()
        send_at = api.send_at or self.cfg.schedule.window_end
        send_time = parse_clock(send_at)
        emails = self.store.employee_emails()
        pending = []
        issues = []
        for offset in range(7, -1, -1):
            day = today - dt.timedelta(days=offset)
            if (day.weekday() >= 5 or now < dt.datetime.combine(day, send_time, self.service.tz)
                    or not self.service.is_workday(day)):
                continue
            # Check freshness before building a report; a concurrent refresh must not bless provisional fines.
            if self.cfg.base_api_key:
                ready = (leave_api.base_ready(self.cfg, self.store, now, day) if day == today else
                         self.store.get(f'base_timeoffs:{day}') is not None and leave_api.all_tracked_have_username(self.store))
            else:
                ready = not leave_api.warning(self.cfg, self.store, now)
            report = self.service.report(day)
            day_pending = []
            for arrival in report.late:
                email = emails.get(arrival.employee_id, '').strip().lower()
                if not email:
                    message = f'{day}: A late employee has no email; update People to send their report.'
                    issues.append(message)
                    self._status(now, False, message)
                    continue
                ts = arrival.ts.astimezone(VN)
                event = {'email': email, 'date': ts.date().isoformat(), 'arrivedAt': ts.strftime('%H:%M')}
                if not today - dt.timedelta(days=7) <= ts.date() <= today or ts.weekday() >= 5:
                    continue
                minutes = minutes_late(arrival, report.window)
                if 0 <= minutes <= 720:
                    event['lateMinutes'] = minutes
                done = f'camera_api:{email}'
                rejected = done + ':' + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                if not self.store.notice_sent(day, done) and not self.store.notice_sent(day, rejected):
                    day_pending.append((day, done, rejected, event))
            if not day_pending:
                continue
            if not ready:
                message = f'{day}: Waiting for HR leave data; no provisional fines sent.'
                issues.append(message)
                self._status(now, False, message)
                continue
            pending.extend(day_pending)
        if not pending:
            if issues:
                return {'ok': False, 'message': ' '.join(dict.fromkeys(issues))}
            if self.service.is_workday(today) and now < dt.datetime.combine(today, send_time, self.service.tz):
                return {'ok': False, 'message': f'No past reports to send. Today is available after scheduled send time {send_at}.'}
            return {'ok': True, 'message': 'No unsent late arrivals eligible for reporting in the last 7 days and today.'}
        rejected_count = 0
        processed_count = 0
        for start in range(0, len(pending), 200):
            batch = pending[start:start + 200]
            try:
                response = post(api.url, {'action': 'camera.late', 'secret': api.key,
                                          'events': [row[3] for row in batch]})
                if response.get('ok') is not True:
                    if response.get('permanent') is True:
                        for day, _, rejected, _ in batch:
                            self.store.mark_notice(day, rejected, now)
                        return self._status(now, False, 'Server rejected the request permanently; check attendance data and API format.')
                    raise ValueError()
                results = response.get('results')
                if not isinstance(results, list) or len(results) != len(batch):
                    raise ValueError()
                for row, result in zip(batch, results):
                    event = row[3]
                    if (not isinstance(result, dict) or result.get('email', '').lower() != event['email']
                            or result.get('date') != event['date']
                            or result.get('status') not in ('created', 'duplicate', 'skipped', 'rejected')):
                        raise ValueError()
            except Exception:
                # Do not log exception/response text: the server or URL may echo the secret.
                return self._status(now, False, 'Delivery failed; check URL, key and server. Will retry in 30 seconds.')
            for (day, done, rejected, _), result in zip(batch, results):
                failed = result['status'] == 'rejected'
                rejected_count += failed
                processed_count += not failed
                self.store.mark_notice(day, rejected if failed else done, now)
                if failed:
                    self.store.log(now, 'system', 'camera API rejected', f'{day}: {result["email"]}; correct employee/attendance data')
            message = f'{processed_count} processed, {rejected_count} rejected. Check Audit for rejected employees.'
            if issues:
                message += ' ' + ' '.join(dict.fromkeys(issues))
            self._status(now, not rejected_count and not issues, message)
        return self.status()
