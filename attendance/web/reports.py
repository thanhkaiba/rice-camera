"""Routes for attendance reports (/report), CSV exports, recognition accuracy (/accuracy), and audit log (/audit)."""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import TYPE_CHECKING

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from ..core.service import SOURCES, minutes_late
from . import _date, failed

if TYPE_CHECKING:
    from ..core.pipeline import App


def register(app: Flask, core: "App") -> None:
    store, service = core.store, core.service

    @app.post("/leave/sync")
    def leave_sync():
        if not core.leave_sync.enabled:
            return failed("Set the HR system API address in Settings first.", "settings_page")
        status = core.leave_sync.sync()
        back = request.form.get("back") or url_for("index")
        flash(("Leave data updated: " if status["ok"] else "Leave data update failed: ") + status["message"],
              "ok" if status["ok"] else "error")
        return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("index"))

    def _period():
        today = service.now().date()
        start = _date(request.args.get("start"), today.replace(day=1))
        end = _date(request.args.get("end"), today)
        if end < start or (end - start).days > 366:
            abort(400, "choose an end date on or after the start date, at most one year later")
        return start, end

    @app.get("/report")
    def report():
        start, end = _period()
        today = service.now().date()
        first = today.replace(day=1)
        last_month_end = first - dt.timedelta(days=1)
        ranges = [("This month", first, today), ("Last month", last_month_end.replace(day=1), last_month_end),
                  ("Last 7 days", today - dt.timedelta(days=6), today), ("Last 30 days", today - dt.timedelta(days=29), today)]
        return render_template("report.html", rep=service.period_report(start, end), start=start, end=end,
                               ranges=ranges)

    @app.get("/accuracy")
    def accuracy():
        from ..core.accuracy import accuracy_report

        today = service.now().date()
        start = _date(request.args.get("start"), today - dt.timedelta(days=29))
        end = _date(request.args.get("end"), today)
        if end < start:
            start, end = end, start
        ranges = [("Last 7 days", today - dt.timedelta(days=6), today),
                  ("Last 30 days", today - dt.timedelta(days=29), today),
                  ("Last 90 days", today - dt.timedelta(days=89), today)]
        return render_template("accuracy.html", acc=accuracy_report(store, core.gallery, start, end),
                               names=store.employees(), ranges=ranges)

    def _csv(name: str, header: list[str], rows) -> Response:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(rows)
        # The byte order mark makes Excel read accented names correctly.
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/report/summary.csv")
    def report_summary_csv():
        start, end = _period()
        rep = service.period_report(start, end)
        emails = store.employee_emails()
        return _csv(f"attendance-summary-{start}-{rep.end}.csv",
                    ["email", "name", "work_days", "on_time", "late", "late_minutes", "not_seen", "on_leave"],
                    [[emails.get(p.employee_id, ""), p.name, p.work_days, p.on_time, p.late, p.late_minutes, p.not_seen, p.on_leave]
                     for p in rep.people])

    @app.get("/report/details.csv")
    def report_details_csv():
        start, end = _period()
        rep = service.period_report(start, end)
        rows = []
        for d in rep.days:
            outage = "; ".join(f"camera down {a:%H:%M}-{b:%H:%M}" for a, b in d.outages)
            for a in d.on_time:
                rows.append([d.day, a.employee_id, a.name, "on_time", a.time, 0, SOURCES.get(a.source, a.source), outage])
            for a in d.late:
                rows.append([d.day, a.employee_id, a.name, "late", a.time, minutes_late(a, d.window),
                             SOURCES.get(a.source, a.source), outage])
            for emp_id, name in d.not_seen:
                rows.append([d.day, emp_id, name, "no_camera_data" if d.no_camera_data else "not_seen",
                             "", "", "", outage])
            for emp_id, name, kind in d.on_leave:
                rows.append([d.day, emp_id, name, "on_leave", "", "", kind, outage])
        emails = store.employee_emails()
        for row in rows:
            row[1] = emails.get(row[1], "")
        return _csv(f"attendance-details-{start}-{rep.end}.csv",
                    ["date", "email", "name", "status", "arrived", "minutes_late", "source_or_leave", "note"], rows)

    @app.get("/audit")
    def audit():
        return render_template("audit.html", rows=store.audit(), local=lambda ts: ts.astimezone(service.tz))
