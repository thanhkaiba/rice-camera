"""Routes for the system status page (/status) and the diagnostics download."""
from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, Response, render_template

from ..core import diagnostics, health
from . import current_user

if TYPE_CHECKING:
    from ..core.pipeline import App


def register(app: Flask, core: "App") -> None:
    cfg, store, service = core.cfg, core.store, core.service

    @app.get("/status")
    def status_page():
        return render_template("status.html", st=health.status(core), cfg=cfg, few_photos=health.FEW_PHOTOS)

    @app.get("/status/diagnostics.zip")
    def diagnostics_zip():
        now = service.now()
        data = diagnostics.build(cfg, store, status=health.status(core), now=now)
        store.log(now, current_user(cfg), "download diagnostics", "")
        name = diagnostics.file_name(now)
        return Response(data, mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
