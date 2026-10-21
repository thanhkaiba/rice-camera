"""One person's day, for when a late arrival is disputed.

What is kept for every sighting: the whole camera picture with the time and name burned in and
the face boxed (`scene`), the face itself, the person below it, and a SHA-256 of those files
recorded when they were taken. This module puts that together with the rule that was applied,
what the camera said, every correction a person made, and whether the files still match their
hash, as a page and as a ZIP that can be handed over.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import socket
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from .service import SOURCES, minutes_late
from ..store import Checkin, Sighting, body_photo, scene_photo

FILE_KINDS = ("scene", "face", "body")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_digest(data_dir: Path, image: str | None) -> str | None:
    """SHA-256 over the snapshot files of a sighting (face, then scene), recorded with the sighting
    so that later anyone can check the files were not altered."""
    if not image:
        return None
    h, found = hashlib.sha256(), False
    for rel in (image, scene_photo(image)):
        path = Path(data_dir) / rel
        if path.exists():
            h.update(path.read_bytes())
            found = True
    return h.hexdigest() if found else None


@dataclass
class Item:
    sighting: Sighting
    time: str  # local HH:MM:SS
    files: dict[str, str] = field(default_factory=dict)  # kind -> path relative to the data folder
    intact: bool | None = None  # files match the hash recorded at capture; None if none was recorded

    @property
    def source(self) -> str:
        return SOURCES.get(self.sighting.source, self.sighting.source)

    @property
    def camera_said(self) -> str | None:
        """Who the camera identified, when that is not who the sighting belongs to now."""
        s = self.sighting
        return s.camera_id if s.camera_id and s.camera_id != s.employee_id else None

    @property
    def integrity(self) -> str:
        if self.intact is None:
            return "no hash recorded"
        return "files unchanged since capture" if self.intact else "FILES DIFFER from the hash recorded at capture"


@dataclass
class Evidence:
    day: dt.date
    employee_id: str
    name: str
    status: str  # Late, On time, Not seen, On leave (...), Never counted as late, Not a work day, ...
    minutes_late: int | None
    start: dt.datetime
    late_after: dt.datetime
    end: dt.datetime
    arrival: Checkin | None
    arrival_time: str | None  # local HH:MM:SS
    items: list[Item]
    corrections: list[tuple[dt.datetime, str, str, str]]  # (when, who, action, details), oldest first
    generated_at: dt.datetime
    timezone: str
    computer: str = field(default_factory=socket.gethostname)
    version: str = __version__
    names: dict[str, str] = field(default_factory=dict)
    email: str = ""

    @property
    def arrival_source(self) -> str | None:
        return SOURCES.get(self.arrival.source, self.arrival.source) if self.arrival else None

    def summary(self, data_dir: Path | None = None) -> dict:
        """Everything on the page as data, for the ZIP (with each file's hash as of the export)."""
        sightings = []
        for it in self.items:
            s = it.sighting
            entry = {
                "time": s.ts.isoformat(), "source": it.source, "status": "removed" if s.status == "rejected" else "ok",
                "camera_said": self.names.get(s.camera_id, s.camera_id) if s.camera_id else None,
                "match_score": round(s.score, 3) if s.score is not None else None,
                "files": dict(it.files), "sha256_at_capture": s.sha256, "integrity": it.integrity,
            }
            if data_dir is not None:
                entry["sha256_now"] = {k: file_sha256(Path(data_dir) / rel) for k, rel in it.files.items()}
            sightings.append(entry)
        return {
            "generated_at": self.generated_at.isoformat(), "software": f"Camera attendance {self.version}",
            "computer": self.computer, "timezone": self.timezone,
            "employee": {"email": self.email, "name": self.name}, "day": self.day.isoformat(),
            "status": self.status, "minutes_late": self.minutes_late,
            "rule": {"camera_hours": f"{self.start:%H:%M}-{self.end:%H:%M}", "late_from": f"{self.late_after:%H:%M}",
                     "arrival": "the earliest sighting of the day that was not removed; a time typed in by an "
                                "admin takes precedence; seen at or after late_from counts as late"},
            "arrival": None if self.arrival is None else {
                "time": self.arrival.first_seen.isoformat(), "source": self.arrival_source,
                "match_score": round(self.arrival.score, 3) if self.arrival.score is not None else None},
            "sightings": sightings,
            "corrections": [{"at": when.isoformat(), "by": who, "action": action, "details": details}
                            for when, who, action, details in self.corrections],
            "notes": ["Times come from the office computer's clock.",
                      "Each sighting's files were hashed (SHA-256) when taken; 'files unchanged since capture' "
                      "means they still match that hash."],
        }

    def text(self) -> str:
        lines = [f"Attendance evidence: {self.name} ({self.email}), {self.day:%A %d %B %Y}",
                 f"Result: {self.status}" + (f", {self.minutes_late} min late" if self.minutes_late is not None else ""),
                 f"Rule: camera hours {self.start:%H:%M}-{self.end:%H:%M}, late from {self.late_after:%H:%M}; "
                 "the arrival is the earliest sighting not removed (a time typed in by an admin takes precedence).",
                 ""]
        if self.arrival:
            score = f", match {self.arrival.score:.0%}" if self.arrival.score else ""
            lines.append(f"Arrival: {self.arrival_time} ({self.arrival_source}{score})")
        lines.append(f"Seen by the camera: {len(self.items)} time(s)")
        for it in self.items:
            s = it.sighting
            said = f", camera said {self.names.get(it.camera_said, it.camera_said)}" if it.camera_said else ""
            score = f", match {s.score:.0%}" if s.score else ""
            removed = " [removed by an admin]" if s.status == "rejected" else ""
            files = ", ".join(f"{k}: photos/{Path(rel).name}" for k, rel in it.files.items()) or "no files"
            lines.append(f"  {it.time}  {it.source}{score}{said}{removed}  ({it.integrity})  {files}")
        lines.append("")
        lines.append(f"Changes made by people: {len(self.corrections)}")
        for when, who, action, details in self.corrections:
            lines.append(f"  {when:%Y-%m-%d %H:%M:%S}  {who}: {action}  {details}")
        lines += ["", f"Generated {self.generated_at:%Y-%m-%d %H:%M:%S} ({self.timezone}) by Camera attendance "
                      f"{self.version} on {self.computer}. Times come from this computer's clock."]
        return "\n".join(lines)


def build(service, day: dt.date, employee_id: str) -> Evidence:
    store, cfg = service.store, service.cfg
    data_dir = cfg.path(cfg.data_dir)
    rep = service.report(day)
    names = store.employees()
    name = names.get(employee_id) or service._name(employee_id)

    minutes = None
    late = next((a for a in rep.late if a.employee_id == employee_id), None)
    if not rep.workday:
        status = "Not a work day" + (f" ({rep.day_note})" if rep.day_note else "")
    elif late is not None:
        status, minutes = "Late", minutes_late(late, rep.window)
    elif any(a.employee_id == employee_id for a in rep.on_time):
        status = "On time"
    elif any(e == employee_id for e, _, _ in rep.on_leave):
        status = "On leave (" + next(k for e, _, k in rep.on_leave if e == employee_id) + ")"
    elif any(e == employee_id for e, _, _ in rep.exempt):
        status = "Never counted as late"
    elif any(e == employee_id for e, _ in rep.not_seen):
        status = "Not seen" + (" (no camera data that day)" if rep.no_camera_data else "")
    else:
        status = "Not on the list that day"

    items = []
    for s in store.sightings(day, employee_id):
        files = {}
        if s.image:
            for kind, rel in (("scene", scene_photo(s.image)), ("face", s.image), ("body", body_photo(s.image))):
                if (data_dir / rel).exists():
                    files[kind] = rel
        intact = None if not s.sha256 else evidence_digest(data_dir, s.image) == s.sha256
        items.append(Item(s, f"{service._local(s.ts):%H:%M:%S}", files, intact))

    arrival = store.checkins(day).get(employee_id)
    corrections = list(reversed(store.audit_about(day, name, employee_id)))
    corrections = [(service._local(when), who, action, details) for when, who, action, details in corrections]
    return Evidence(day, employee_id, name, status, minutes, rep.window.start, rep.window.late_from, rep.window.end,
                    arrival, f"{service._local(arrival.first_seen):%H:%M:%S}" if arrival else None, items,
                    corrections, service.now(), cfg.timezone, names=names,
                    email=store.employee_emails().get(employee_id, ""))


def zip_bytes(ev: Evidence, data_dir: Path) -> bytes:
    """The evidence as one file to hand over: summary.txt, summary.json and the pictures."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", ev.text())
        z.writestr("summary.json", json.dumps(ev.summary(data_dir), indent=2, ensure_ascii=False))
        for it in ev.items:
            for rel in it.files.values():
                z.write(Path(data_dir) / rel, f"photos/{Path(rel).name}")
    return out.getvalue()
