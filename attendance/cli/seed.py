"""Import a portable seed: employees (seed/employees.csv), their reference photos (seed/faces/<email>/)
and unlabelled camera faces for manual review (seed/review.csv), without recording attendance.

Every file is checked against seed/manifest.json (SHA-256) first, and the database, faces/ and
review-seed/ are copied to <data>/backups/before-seed-<time>/ before anything is written. Runs from
the command line (`import-seed`, program stopped) or from the People page (camera paused meanwhile).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
import shutil
import threading
import uuid
import zipfile
from typing import Callable

import numpy as np

from .enroll import images_in, read_image
from ..store import body_photo, scene_photo, normalize_email

log = logging.getLogger(__name__)

STATUS_FILE = "seed-import.json"  # in the data folder: progress and result of the last import from the page


def _print(text: str) -> None:
    print(text, flush=True)


def _review_vector(app, path: Path):
    image = read_image(path)
    if image is None:
        raise ValueError(f"Review sample cannot be decoded: {path}")
    with app.recognizer_lock:
        faces = app.recognizer().analyze_photo(image)
    if len(faces) == 1 and faces[0].det_score >= .5 and faces[0].size >= 40:
        return faces[0].embedding
    return None


def _review_metadata(app, row, seed, uid, report):
    sample = app.store.unknown(uid)
    if sample.source != "seed":
        return
    app.store.put(f"seed_review_email:{uid}", row.get("expected_email", ""))
    email = row.get("auto_resolve_email")
    if not email or sample.status != "pending":
        return
    report["auto_pending"] += 1
    if (app.store.get(f"seed_review_manual:{uid}") or sample.employee_id is not None
            or sample.model != app.service.model_id or sample.vector is None
            or row["auto_resolve_model"] != app.service.model_id):
        return
    person = app.store.employee_by_email(email)
    if person is None or not person.active:
        return
    # Recheck the actual photo; portable metadata alone never establishes identity.
    vector = _review_vector(app, seed / row["file"])
    if vector is None or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-12:
        return
    matches = app.gallery.match(vector)
    params = app.cfg.recognition
    if (len(matches) < 2 or matches[0][0] != person.id
            or matches[0][1] < max(.60, params.accept_score)
            or matches[0][1] - matches[1][1] < max(.12, params.min_margin)):
        return
    evidence = {"email": email, "model": app.service.model_id, "score": matches[0][1],
                "margin": matches[0][1] - matches[1][1], "sha256": row["sha256"]}
    if app.store.label_seed(uid, person.id, app.service.model_id, json.dumps(evidence), app.service.now()):
        report["resolved"] += 1
        report["auto_pending"] -= 1


def import_review_samples(app, seed: Path, progress: Callable[[str], None] = _print) -> dict:
    if not (seed / "review.csv").exists():
        return {"imported": 0, "skipped": 0, "refreshed": 0, "resolved": 0, "auto_pending": 0}
    seed = seed.resolve()
    manifest = json.loads((seed / "manifest.json").read_text(encoding="utf-8"))
    with (seed / "review.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    # Validate the entire batch before adding rows or copying sample files.
    for relative in ["review.csv"] + [r[key] for r in rows for key in ("file", "body_file", "scene_file") if r.get(key)]:
        path = (seed / relative).resolve()
        if (not path.is_relative_to(seed) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get(relative)):
            raise ValueError(f"Review seed file missing or changed: {relative}")
    for row in rows:
        if row["sha256"] != manifest[row["file"]]:
            raise ValueError("Review sample hash differs from manifest")
        stamp = dt.datetime.fromisoformat(row["captured_at"])
        if stamp.tzinfo is None:
            raise ValueError("Review timestamps must include a timezone")
        for key in ("expected_email", "auto_resolve_email"):
            row[key] = normalize_email(row[key]) if row.get(key) else ""
        if row["auto_resolve_email"] and (row["auto_resolve_email"] != row["expected_email"]
                                          or not row.get("auto_resolve_model")):
            raise ValueError("Seed auto-resolution email must match the source email and include its model")
    existing = {}
    for face in app.store.unknowns(status=None):
        path = app.data_dir / face.image if face.image else None
        if path and path.is_file():
            existing[hashlib.sha256(path.read_bytes()).hexdigest()] = face.id
    report = {"imported": 0, "skipped": 0, "refreshed": 0, "resolved": 0, "auto_pending": 0}
    for row in rows:
        digest = row["sha256"]
        key = "seed_review:" + digest
        if app.store.get(key) or digest in existing:
            if digest in existing:
                sample = app.store.unknown(existing[digest])
                if sample.source == "seed" and sample.status == "pending" and sample.vector is None:
                    vector = _review_vector(app, seed / row["file"])
                    if vector is not None:
                        report["refreshed"] += app.store.refresh_seed_vector(sample.id, app.service.model_id, vector)
                app.store.put(key, str(existing[digest]))
                _review_metadata(app, row, seed, existing[digest], report)
            report["skipped"] += 1
            continue
        source = seed / row["file"]
        vector = _review_vector(app, source)
        relative = "review-seed/" + digest + source.suffix.lower()
        target = app.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        for column, sidecar in (("body_file", body_photo), ("scene_file", scene_photo)):
            if row.get(column):
                shutil.copyfile(seed / row[column], app.data_dir / sidecar(relative))
        stamp = dt.datetime.fromisoformat(row["captured_at"]).astimezone(app.cfg.tz)
        uid = app.store.add_unknown(stamp.date(), stamp, relative, app.service.model_id,
                                    vector, None, None, source="seed")
        app.store.put(key, str(uid))
        existing[digest] = uid
        _review_metadata(app, row, seed, uid, report)
        report["imported"] += 1
        if report["imported"] % 25 == 0:
            progress(f'Review samples imported: {report["imported"]}')
    return report


def find_seed(folder: Path) -> Path:
    """The seed folder (the one holding manifest.json): `folder` itself or a folder in it, e.g. seed/ in a zip."""
    for candidate in [folder, *sorted(p for p in folder.iterdir() if p.is_dir())]:
        if (candidate / "manifest.json").is_file():
            return candidate
    raise ValueError("this is not a seed: no manifest.json in it")


def unpack(archive: Path, into: Path) -> Path:
    """Unpack a seed zip into `into` (a new folder) and return the seed folder in it."""
    try:
        with zipfile.ZipFile(archive) as z:
            into.mkdir(parents=True, exist_ok=True)
            for info in z.infolist():
                if not info.flag_bits & 0x800:  # zipped without the UTF-8 flag (macOS Finder, Windows): names
                    try:                        # like "Ảnh 3x4.jpg" are UTF-8 all the same
                        info.filename = info.filename.encode("cp437").decode("utf-8")
                    except UnicodeError:
                        pass
                z.extract(info, into)  # zipfile drops ".." and absolute paths; the manifest check covers the rest
    except zipfile.BadZipFile:
        raise ValueError("this is not a zip file") from None
    return find_seed(into)


def check(seed: Path) -> tuple[list[dict], dict[str, list[Path]]]:
    """Verify the manifest, and that the employee list and the photo folders agree, before anything
    is written. Returns the employees (rows of employees.csv) and their photos by email."""
    seed = seed.resolve()
    manifest = json.loads((seed / "manifest.json").read_text(encoding="utf-8"))
    for relative, digest in manifest.items():
        path = (seed / relative).resolve()
        if not path.is_relative_to(seed) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Seed file missing or changed: {relative}")
    with (seed / "employees.csv").open(encoding="utf-8-sig", newline="") as file:
        people = list(csv.DictReader(file))
    emails = [p["email"] for p in people]
    folders = {p.name for p in (seed / "faces").iterdir() if p.is_dir()} if (seed / "faces").is_dir() else set()
    if len(emails) != len(set(emails)) or set(emails) != folders:
        raise ValueError("Employee CSV and photo folders must have the same unique emails")
    photos = {email: images_in(seed / "faces" / email, subfolders=True) for email in emails}
    planned = {p.relative_to(seed).as_posix() for paths in photos.values() for p in paths}
    if planned != {p for p in manifest if p.startswith("faces/")}:
        raise ValueError("Photo files differ from the seed manifest")
    return people, photos


def backup(app) -> tuple[Path, str]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    folder = app.data_dir / "backups" / ("before-seed-" + stamp)
    folder.mkdir(parents=True)
    app.store.backup_to(folder / "attendance.db")
    for name in ("faces", "review-seed"):
        if (app.data_dir / name).exists():
            shutil.copytree(app.data_dir / name, folder / name)
    return folder, stamp


def import_seed(app, seed: Path, review_only: bool = False, progress: Callable[[str], None] = _print) -> dict:
    """Import a seed folder into `app`; returns the report, which is also saved in the data folder.
    ValueError: the seed does not check out (nothing written). RuntimeError: some photos were rejected."""
    seed = seed.resolve()
    if review_only:  # employees and reference photos are already right: only add the review samples
        folder, stamp = backup(app)  # import_review_samples checks its files against the manifest itself
        progress(f"Backup: {folder}")
        report = import_review_samples(app, seed, progress)
        report["backup"] = str(folder)
        (app.data_dir / f"seed-review-import-{stamp}.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    people, photos = check(seed)
    folder, stamp = backup(app)
    progress(f"Backup: {folder}")
    planned = sum(len(paths) for paths in photos.values())
    report = dict(model=app.cfg.recognition.model_id(), employees=len(people), expected_photos=planned,
                  imported=0, failed=[], warnings=[], backup=str(folder))
    try:
        app.recognizer()
        app.store.import_employees((seed / "employees.csv").read_text(encoding="utf-8-sig"))
        app.store.put("employee_email_csv_imported", "1")
        for row in people:
            person = app.store.employee_by_email(row["email"])
            for employee_id, image, source, _ in app.store.reference_images():
                if employee_id == person.id and source == "enroll":
                    app.library.delete_photo(image)
            for path in photos[row["email"]]:
                relative = path.relative_to(seed).as_posix()
                try:
                    image = read_image(path)
                    if image is None:
                        raise ValueError("Image cannot be decoded")
                    result = app.library.add_photo(person.id, image, source="enroll")
                    if not result.ok:
                        raise ValueError(result.message)
                except ValueError as exc:
                    report["failed"].append(dict(file=relative, error=str(exc)))
                    continue
                report["imported"] += 1
                if result.warnings:
                    report["warnings"].append(dict(file=relative, messages=result.warnings))
                if report["imported"] % 25 == 0:
                    progress(f'Imported {report["imported"]}/{planned} photos')
        report["review"] = import_review_samples(app, seed, progress)
        app.store.log(app.service.now(), "seed", "import seed", f'{len(people)} employees, {report["imported"]} photos')
    finally:
        app.library.reload()
        (app.data_dir / f"seed-import-{stamp}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    progress(f'Imported {report["imported"]}/{planned} photos; failures: {len(report["failed"])}')
    if report["failed"]:
        raise PhotosRejected(f'{len(report["failed"])} photos were rejected; see seed-import-{stamp}.json '
                             "in the data folder", report)
    return report


class PhotosRejected(RuntimeError):
    """The import ran to the end (employees and the other photos are in), but some photos were refused."""

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = report


def script_main(seed: Path) -> None:
    """seed/import_seed.py: `python seed/import_seed.py --config config.yaml [--review-only]`, program stopped."""
    import argparse

    from ..core.config import load
    from ..core.pipeline import App

    parser = argparse.ArgumentParser(description="Import this seed into the attendance program (stop it first).")
    parser.add_argument("--config", default=str(seed.parent / "config.yaml"))
    parser.add_argument("--review-only", action="store_true", help="only add review samples; keep employees and photos")
    args = parser.parse_args()
    app = App(load(args.config))
    try:
        report = import_seed(app, seed, args.review_only)
    finally:
        app.store.close()
    if args.review_only:
        print(json.dumps(report, indent=2))


_running = threading.Lock()


def read_status(app) -> dict:
    try:
        status = json.loads((app.data_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if status.get("state") == "running" and not _running.locked():  # the program stopped mid-import
        status.update(state="failed", message="The import was interrupted (the program stopped). Upload the seed again.")
    return status


def _write_status(app, **fields) -> None:
    status = {} if fields.get("state") == "running" else read_status(app)
    status.update({k: v for k, v in fields.items() if v is not None})
    path = app.data_dir / STATUS_FILE
    partial = path.with_suffix(".part")
    partial.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


def start(app, archive: Path, review_only: bool = False, user: str = "admin") -> None:
    """Import an uploaded seed zip in a thread of the running program, with the camera paused
    meanwhile (app.maintenance), so nothing else writes attendance while photos are replaced.
    Progress and the result go to STATUS_FILE. The zip is deleted afterwards."""
    if not zipfile.is_zipfile(archive):
        raise ValueError("this is not a zip file")
    if not _running.acquire(blocking=False):
        raise ValueError("a seed import is already running")
    _write_status(app, state="running", started=dt.datetime.now().isoformat(timespec="seconds"),
                  review_only=review_only, progress="Checking the seed")
    threading.Thread(target=_run, args=(app, archive, review_only, user), name="seed-import", daemon=True).start()


def _run(app, archive: Path, review_only: bool, user: str) -> None:
    work = archive.with_suffix("")
    app.maintenance = "importing a seed"
    try:
        try:
            seed = unpack(archive, work)
            report = import_seed(app, seed, review_only, progress=lambda text: _write_status(app, progress=text))
        except PhotosRejected as exc:  # done, with photos to look at: uploading again would redo all of it
            message = f"{summary(exc.report)} · {exc}"
            app.store.log(app.service.now(), user, "import seed", message)
            result = dict(state="done", message=message, backup=exc.report.get("backup"))
        except Exception as exc:  # shown on the People page
            log.exception("Seed import failed")
            result = dict(state="failed", message=str(exc))
        else:
            app.store.log(app.service.now(), user, "import seed", summary(report))
            result = dict(state="done", message=summary(report), backup=report.get("backup"))
        finally:
            app.maintenance = None
            shutil.rmtree(work, ignore_errors=True)
            try:
                archive.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove seed upload %s: %s", archive, exc)
                result["message"] += f" · Could not remove temporary upload: {exc}"
        _write_status(app, **result, finished=dt.datetime.now().isoformat(timespec="seconds"))
    finally:
        _running.release()


def summary(report: dict) -> str:
    """The result in one line, for the People page."""
    review = report.get("review", {} if "expected_photos" in report else report)
    parts = []
    if "expected_photos" in report:
        parts.append(f'{report["employees"]} employees, {report["imported"]}/{report["expected_photos"]} photos')
        if report["warnings"]:
            parts.append(f'{len(report["warnings"])} photos with warnings (blurred or look-alike)')
    if review:
        parts.append(f'{review.get("imported", 0)} review samples added, '
                     f'{review.get("resolved", 0)} labelled automatically')
    return "; ".join(parts)
