"""The one-time copy of the office Mac's database and photos to the server (see attendance/migrate.py).

Open only in shadow mode and only with the admin login: the Mac's everyday token cannot replace the database.
The database is checked and stored beside the live one, then the worker restarts and swaps it in before
opening anything, so a request or the scheduler never sees it half replaced.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from flask import Flask, abort, jsonify, request

from ..core import config as config_module

if TYPE_CHECKING:
    from ..core.pipeline import App

log = logging.getLogger(__name__)

DATABASE = "attendance.db"
PENDING = "attendance.db.incoming"
FOLDERS = ("crops", "faces", "late", "review-seed")
PHOTO_PATH = re.compile(r"(?:crops|faces|late|review-seed)(?:/[A-Za-z0-9_\-]+)*/[A-Za-z0-9_\-]+\.jpe?g")
COUNTED = ("employees", "embeddings", "checkins", "sightings", "unknowns", "audit", "notices")
# Saved settings that only mean something on the Mac, or that the server takes from its environment.
MAC_ONLY = {"camera": ("url", "more"), "web": ("password",), "backup": ("folder",)}
MAC_ONLY_FILE = ("data_dir", "employees_file", "camera", "web", "updates", "edge")  # in config.yaml
ENDPOINTS = {"import_mac_config", "import_manifest", "import_photo", "import_database", "import_status"}


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in COUNTED}


def prepare(compressed: bytes, data_dir: Path) -> dict[str, int]:
    """Check an uploaded database and leave it ready to be swapped in; returns its row counts. Raises ValueError."""
    partial = data_dir / f".{uuid.uuid4().hex}.import"
    try:
        partial.write_bytes(gzip.decompress(compressed))
        db = sqlite3.connect(partial)
        try:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("the database is damaged")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {*COUNTED, "kv", "outbox"} <= tables:
                raise ValueError("this is not an attendance database")
            with db:
                db.execute("DELETE FROM outbox")  # messages the Mac still has to post stay the Mac's to post
                if "edge_outbox" in tables:
                    db.execute("DELETE FROM edge_outbox")
                row = db.execute("SELECT value FROM kv WHERE key = 'settings'").fetchone()
                saved = json.loads(row[0]) if row else {}
                for section, names in MAC_ONLY.items():
                    for name in names:
                        saved.get(section, {}).pop(name, None)
                db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('settings', ?)", (json.dumps(saved),))
            counts = _counts(db)
        finally:
            db.close()
        os.replace(partial, data_dir / PENDING)
        return counts
    except (OSError, EOFError, gzip.BadGzipFile, sqlite3.DatabaseError, json.JSONDecodeError) as exc:
        raise ValueError(f"the database cannot be used: {exc}") from None
    finally:
        for leftover in (partial, partial.with_name(partial.name + "-wal"), partial.with_name(partial.name + "-shm")):
            leftover.unlink(missing_ok=True)


def adopt(data_dir: Path) -> bool:
    """At startup, before the database is opened: put an uploaded database in place."""
    incoming = data_dir / PENDING
    if not incoming.exists():
        return False
    for sidecar in ("-wal", "-shm"):
        (data_dir / (DATABASE + sidecar)).unlink(missing_ok=True)
    os.replace(incoming, data_dir / DATABASE)
    log.info("Now using the database copied from the office Mac")
    return True


def restart_worker() -> None:
    """The process manager starts a new worker, which adopts the database. Delayed so the answer gets out."""
    threading.Timer(1.0, os._exit, [0]).start()


def register(app: Flask, core: "App") -> None:
    data_dir = core.data_dir

    @app.put("/api/edge/import/config")
    def import_mac_config():
        """Takes effect with the restart that follows the database."""
        try:
            wanted = yaml.safe_load(request.get_data()) or {}
            if not isinstance(wanted, dict):
                raise ValueError("not a list of settings")
            for key in MAC_ONLY_FILE:
                wanted.pop(key, None)
            config_module._build(config_module.Config, wanted).validate()
        except (yaml.YAMLError, TypeError, ValueError) as exc:
            abort(400, f"config.yaml cannot be used: {exc}")
        target = core.cfg.base_dir / "config.yaml"
        partial = target.with_name(f".{uuid.uuid4().hex}.part")
        partial.write_text(yaml.safe_dump(wanted, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.replace(partial, target)
        return jsonify(sorted(wanted))

    @app.get("/api/edge/import/manifest")
    def import_manifest():
        found = {}
        for folder in FOLDERS:
            for path in (data_dir / folder).rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    found[path.relative_to(data_dir).as_posix()] = path.stat().st_size
        return jsonify(found)

    @app.put("/api/edge/import/photo/<path:rel>")
    def import_photo(rel: str):
        if not PHOTO_PATH.fullmatch(rel):
            abort(400, "bad photo path")
        data = request.get_data()
        if not data.startswith(b"\xff\xd8"):
            abort(415, "not a JPEG")
        path = data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{uuid.uuid4().hex}.part")
        partial.write_bytes(data)
        os.replace(partial, path)
        return jsonify(ok=True)

    @app.put("/api/edge/import/database")
    def import_database():
        try:
            counts = prepare(request.get_data(), data_dir)
        except ValueError as exc:
            abort(400, str(exc))
        log.info("Database received from the office Mac: %s; restarting to use it", counts)
        restart_worker()
        return jsonify(counts)

    @app.get("/api/edge/import/status")
    def import_status():
        return jsonify(_counts(core.store._db))
