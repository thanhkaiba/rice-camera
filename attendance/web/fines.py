"""The fine notice's two pages (core/fines.py), both opened without the admin login: the token in
the address is the key, a different one every day, and each page only answers during its hours.

/fines/<token>          who is fined today; pick your name and write an appeal
/fines/review/<token>   the approvers: the appeals, to accept or reject
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, abort, flash, make_response, redirect, render_template, request, send_file, url_for

from ..core import fines

if TYPE_CHECKING:
    from ..core.pipeline import App

CLOSED = "Trang này không còn mở.\n"


def register(app: Flask, core: "App") -> None:
    service = core.service

    def day_of(page: str, token: str):
        day = fines.open_day(service, page, token)
        if day is None:
            abort(make_response((CLOSED, 404, {"Content-Type": "text/plain; charset=utf-8"})))
        return day

    def private(response):
        """Never kept by a browser or a proxy, never indexed: the page shows who came late."""
        response = make_response(response)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def photo(page: str, token: str, employee_id: str):
        day = day_of(page, token)
        # The approver still sees the photo of someone whose appeal was accepted a moment ago.
        people = fines.fined(service, day, accepted=page == fines.REVIEW)
        person = next((f for f in people if f.employee_id == employee_id), None)
        if person is None or person.photo is None:
            abort(404)
        return private(send_file(person.photo, mimetype="image/jpeg", max_age=0))

    @app.get("/fines/<token>")
    def fines_page(token: str):
        day = day_of(fines.APPEAL, token)
        t = fines.times(service, day)
        cfg = service.cfg.fine_notice
        return private(render_template("fines.html", day=day, token=token, people=fines.fined(service, day),
                                       intro=fines.intro(service.store, day)[0], cfg=cfg,
                                       list_open=service.now() < t.list_close, max_message=fines.MAX_MESSAGE))

    @app.post("/fines/<token>")
    def fines_appeal(token: str):
        day = day_of(fines.APPEAL, token)
        error = fines.submit(service, day, request.form.get("employee_id", ""), request.form.get("message", ""))
        flash(error or "Đơn đã dâng lên công đường. Quan sẽ phán xử trước "
              f"{service.cfg.fine_notice.review_until}.", "error" if error else "ok")
        return redirect(url_for("fines_page", token=token))

    @app.get("/fines/<token>/photo/<employee_id>.jpg")
    def fines_photo(token: str, employee_id: str):
        return photo(fines.APPEAL, token, employee_id)

    @app.get("/fines/review/<token>")
    def fines_review(token: str):
        day = day_of(fines.REVIEW, token)
        people = {f.employee_id: f for f in fines.fined(service, day, accepted=True)}
        appeals = [{**a, "person": people.get(a["employee_id"]), "name": service.chat_name(a["employee_id"])}
                   for a in service.store.appeals(day)]
        return private(render_template("fines_review.html", day=day, token=token, appeals=appeals,
                                       approvers=fines.approvers(service), cfg=service.cfg.fine_notice,
                                       max_note=fines.MAX_NOTE))

    @app.post("/fines/review/<token>")
    def fines_decide(token: str):
        day = day_of(fines.REVIEW, token)
        accept, reject = request.form.get("accept"), request.form.get("reject")
        employee_id = accept or reject or ""
        error = fines.decide(service, day, employee_id, bool(accept), request.form.get("approver", ""),
                             request.form.get(f"note:{employee_id}", ""))
        if error:
            flash(error, "error")
        else:
            flash(f"{'Đã chấp nhận lời kêu oan của' if accept else 'Oan Thị Mầu:'} {service.chat_name(employee_id)}"
                  f"{'' if accept else ' vẫn bị phạt'}.", "ok")
        response = redirect(url_for("fines_review", token=token))
        if not error:  # the next decision is by the same person: their name stays chosen
            response.set_cookie("fine_approver", request.form.get("approver", ""), max_age=86400, httponly=True,
                                samesite="Lax", secure=request.is_secure)
        return response

    @app.get("/fines/review/<token>/photo/<employee_id>.jpg")
    def fines_review_photo(token: str, employee_id: str):
        return photo(fines.REVIEW, token, employee_id)
