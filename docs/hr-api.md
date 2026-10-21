# HR system API

Holidays, make-up work days and leave are read from the HR system; they are not entered on the web page. The program calls:

```
GET <leave_api.url>?from=2026-10-08&to=2026-12-14
Authorization: Bearer <leave_api.token>
```

and expects JSON like this:

```json
{
  "holidays": [{"date": "2026-11-20", "name": "Teachers' Day"}],
  "workdays": [{"date": "2026-10-24", "name": "Make-up day"}],
  "leaves": [
    {"email": "alice@example.com", "start": "2026-10-15", "end": "2026-10-16", "type": "leave", "note": "Family"},
    {"email": "bob@example.com", "start": "2026-10-15", "type": "sick", "period": "morning"}
  ]
}
```

- **`email`** must match the email on the People page. Surrounding spaces and letter case are ignored. Invalid or unknown employee emails are skipped and counted in the sync status.
- **`end`** defaults to `start`.
- **`type`** is free text. `leave`, `sick`, `business_trip` and `remote` get friendly labels; anything else is shown as sent.
- **`period`** is `full` (default), `morning` or `afternoon`. Attendance is checked in the morning, so afternoon-only leave is ignored.
- **Each refresh replaces** the data for the requested range, so cancelled leave disappears too.

**When refreshes happen:** every 15 minutes, once more 5 minutes before the late time, and when someone clicks *Refresh* on the Today page.

**If the HR system is unreachable**, the last copy keeps being used; the Today page and the chat messages then say when the data was last updated.

**If the HR system uses a different format**, only the `parse()` function in `attendance/integrations/leave_api.py` needs changing.
