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
import re
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
# The server: the seed whose review samples wait for the office computer's face vectors (a seed_vectors job),
# {"job_id", "image" (a copy of the zip under uploads/), "report", "user"}.
REMOTE_KEY = "seed_import_remote"


def _print(text: str) -> None:
    print(text, flush=True)


def _review_vector(app, path: Path, row: dict | None = None, analyze: bool = True):
    """The face embedding of a one-face review sample, or None. A row may bring it ("vector": base64 float32,
    "vector_model"): the server has no face models, so a seed built on the camera computer carries them.
    `analyze` False (the server): never read the photo here, None without a vector."""
    if row and row.get("vector") and row.get("vector_model") == app.service.model_id:
        import base64

        vector = np.frombuffer(base64.b64decode(row["vector"]), dtype=np.float32).copy()
        norm = float(np.linalg.norm(vector))
        if vector.size >= 64 and np.all(np.isfinite(vector)) and norm > 1e-6:
            return vector / norm
    if not analyze:
        return None
    image = read_image(path)
    if image is None:
        raise ValueError(f"Review sample cannot be decoded: {path}")
    with app.recognizer_lock:
        faces = app.recognizer().analyze_photo(image)
    if len(faces) == 1 and faces[0].det_score >= .5 and faces[0].size >= 40:
        return faces[0].embedding
    return None


def _review_metadata(app, row, seed, uid, report, analyze: bool = True):
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
    vector = _review_vector(app, seed / row["file"], row, analyze)
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


def import_review_samples(app, seed: Path, progress: Callable[[str], None] = _print,
                          vectors: dict | None = None, model: str = "") -> dict:
    """`vectors` (the server): {sha256: base64 float32} read by the office computer with `model`; no photo is
    read here then."""
    analyze = vectors is None
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
        if vectors and model and row["sha256"] in vectors and not row.get("vector"):
            row["vector"], row["vector_model"] = vectors[row["sha256"]], model
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
                    vector = _review_vector(app, seed / row["file"], row, analyze)
                    if vector is not None:
                        report["refreshed"] += app.store.refresh_seed_vector(sample.id, app.service.model_id, vector)
                app.store.put(key, str(existing[digest]))
                _review_metadata(app, row, seed, existing[digest], report, analyze)
            report["skipped"] += 1
            continue
        source = seed / row["file"]
        vector = _review_vector(app, source, row, analyze)
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
        _review_metadata(app, row, seed, uid, report, analyze)
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
    remote = getattr(app, "photos_read_elsewhere", False)  # the server: the face models are on the office computer
    threading.Thread(target=_run_remote if remote else _run, args=(app, archive, review_only, user),
                     name="seed-import", daemon=True).start()


def _forget_remote(app) -> None:
    """The review samples of an earlier seed still waiting: no longer (a new seed was uploaded)."""
    waiting = json.loads(app.store.get(REMOTE_KEY) or "{}")
    if waiting.get("job_id"):
        app.store.edge_job_take(waiting["job_id"])
    if re.fullmatch(r"uploads/[0-9a-f]{32}", waiting.get("image", "")):
        (app.data_dir / waiting["image"]).unlink(missing_ok=True)
    app.store.put(REMOTE_KEY, "")


def _run_remote(app, archive: Path, review_only: bool, user: str) -> None:
    """The server, which has no face models: the people of the seed now, their photos sent to the office computer
    as if uploaded on each person's page, and the review samples once the office computer read their faces."""
    work = archive.with_suffix("")
    try:
        try:
            seed = unpack(archive, work).resolve()
            people, photos = check(seed) if not review_only else ([], {})
            folder, _ = backup(app)
            report = {"employees": len(people), "sent_photos": 0, "backup": str(folder)}
            if not review_only:
                app.store.import_employees((seed / "employees.csv").read_text(encoding="utf-8-sig"))
                app.store.put("employee_email_csv_imported", "1")
                (app.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
                for row in people:
                    person = app.store.employee_by_email(row["email"])
                    for employee_id, image, source, _ in app.store.reference_images():
                        if employee_id == person.id and source == "enroll":
                            app.library.delete_photo(image)
                    for path in photos[row["email"]]:
                        rel = f"uploads/{uuid.uuid4().hex}"
                        shutil.copyfile(path, app.data_dir / rel)
                        app.store.edge_job_add("add_photo", {"employee_id": person.id, "image": rel, "filename": path.name,
                                                             "source": "enroll"}, app.service.now())
                        report["sent_photos"] += 1
                app.library.reload()
            _forget_remote(app)
            if (seed / "review.csv").exists():
                rel = f"uploads/{uuid.uuid4().hex}"
                (app.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive, app.data_dir / rel)
                job = app.store.edge_job_add("seed_vectors", {"kind": "seed_vectors", "image": rel}, app.service.now())
                app.store.put(REMOTE_KEY, json.dumps({"job_id": job, "image": rel, "report": report, "user": user}))
                done = summary(report)
                result = dict(state="waiting", backup=report["backup"], message=(done + "; " if done else "")
                              + "the review samples wait for the office computer to read their faces (outside the camera hours)")
            else:
                app.store.log(app.service.now(), user, "import seed", summary(report))
                result = dict(state="done", message=summary(report), backup=report["backup"])
        except Exception as exc:  # shown on the People page
            log.exception("Seed import failed")
            result = dict(state="failed", message=str(exc))
        finally:
            shutil.rmtree(work, ignore_errors=True)
            archive.unlink(missing_ok=True)
        _write_status(app, **result, finished=dt.datetime.now().isoformat(timespec="seconds"))
    finally:
        _running.release()


def finish_remote(app, job_id: int, payload: dict, result: dict) -> threading.Thread:
    """The server: the office computer's answer to a seed_vectors job; the review samples are imported with
    its face vectors, in a thread."""
    def run():
        with _running:
            waiting = json.loads(app.store.get(REMOTE_KEY) or "{}")
            if waiting.get("job_id") != job_id:  # an older seed, replaced by a newer one
                return
            image = waiting.get("image", "")
            work = app.data_dir / "seed-upload" / f"review-{job_id}"
            try:
                if not result.get("ok"):
                    raise ValueError(str(result.get("message") or "the office computer could not read the faces")[:400])
                if not re.fullmatch(r"uploads/[0-9a-f]{32}", image):
                    raise ValueError("the seed is no longer on the server")
                _write_status(app, state="running", started=dt.datetime.now().isoformat(timespec="seconds"),
                              progress="Importing the review samples")
                seed = unpack(app.data_dir / image, work).resolve()
                vectors = result.get("vectors") if isinstance(result.get("vectors"), dict) else {}
                review = import_review_samples(app, seed, lambda text: _write_status(app, progress=text),
                                               vectors=vectors, model=str(result.get("model") or ""))
                report = {**waiting.get("report", {}), "review": review}
                app.store.log(app.service.now(), waiting.get("user", "admin"), "import seed", summary(report))
                status = dict(state="done", message=summary(report), backup=report.get("backup"))
            except Exception as exc:
                log.exception("Seed import failed")
                status = dict(state="failed", message=str(exc))
            finally:
                shutil.rmtree(work, ignore_errors=True)
                _forget_remote(app)
            _write_status(app, **status, finished=dt.datetime.now().isoformat(timespec="seconds"))

    thread = threading.Thread(target=run, name="seed-review-import", daemon=True)
    thread.start()
    return thread


def review_vectors(app, archive: Path, work: Path) -> dict:
    """The office computer: the face vector of each review sample of a seed uploaded on the server, which has
    no face models. {"ok", "model", "vectors": {sha256: base64 float32}, "message"}."""
    import base64

    seed = unpack(archive, work).resolve()
    rows = []
    if (seed / "review.csv").exists():
        with (seed / "review.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
    vectors = {}
    for row in rows:
        path = (seed / row.get("file", "")).resolve()
        if not row.get("sha256") or not path.is_relative_to(seed) or not path.is_file():
            continue
        try:
            vector = _review_vector(app, path)
        except ValueError:
            continue
        if vector is not None:
            vectors[row["sha256"]] = base64.b64encode(np.asarray(vector, np.float32).tobytes()).decode()
    return {"ok": True, "model": app.service.model_id, "vectors": vectors,
            "message": f"{len(vectors)} of {len(rows)} review faces read"}


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
    if report.get("sent_photos") or report.get("employees") and "expected_photos" not in report:
        parts.append(f'{report["employees"]} employees, {report.get("sent_photos", 0)} photos sent to the office '
                     "computer (each person has them in a few minutes)")
    if "expected_photos" in report:
        parts.append(f'{report["employees"]} employees, {report["imported"]}/{report["expected_photos"]} photos')
        if report["warnings"]:
            parts.append(f'{len(report["warnings"])} photos with warnings (blurred or look-alike)')
    if review:
        parts.append(f'{review.get("imported", 0)} review samples added, '
                     f'{review.get("resolved", 0)} labelled automatically')
    return "; ".join(parts)
