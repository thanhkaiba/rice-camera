"""Enroll reference photos from folders: faces/<email>/*.jpg (the web page can do the same)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..core.config import Config

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MIN_PHOTOS = 5


def read_image(path: Path):
    # cv2.imread cannot open non-ASCII paths on Windows.
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def images_in(folder: Path, subfolders: bool = False) -> list[Path]:
    """Images in `folder`; with `subfolders`, also those one level down (one folder per visit)."""
    found = [p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    if subfolders:
        found += [p for d in folder.iterdir() if d.is_dir() for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    return sorted(found)


def enroll(cfg: Config, faces_dir: Path) -> None:
    from ..core.pipeline import App

    app = App(cfg)
    employees = app.store.employees()
    print(f"Enrolling with model {cfg.recognition.model_id()}")
    seen = set()
    for person_dir in sorted(p for p in faces_dir.iterdir() if p.is_dir()):
        try:
            person = app.store.employee_by_email(person_dir.name)
        except ValueError:
            person = None
        if person is None or not person.active:
            print(f"! {person_dir.name}: not an active employee email (add them on the People page first), skipped")
            continue
        emp = person.id
        seen.add(emp)
        # Re-running replaces photos enrolled from folders; uploads and admin-confirmed faces are kept.
        for employee_id, image, source in app.store.reference_images():
            if employee_id == emp and source == "enroll":
                app.library.delete_photo(image)
        added = 0
        for path in images_in(person_dir, subfolders=True):
            image = read_image(path)
            if image is None:
                print(f"  - {path.name}: not a readable image, skipped")
                continue
            result = app.library.add_photo(emp, image, source="enroll")
            if not result.ok:
                print(f"  - {path.name}: {result.message}")
                continue
            added += 1
            for w in result.warnings:
                print(f"  ! {path.name}: {w}")
        warn = f"  (warning: use at least {MIN_PHOTOS} photos)" if added < MIN_PHOTOS else ""
        print(f"{emp} ({employees[emp]}): {added} photos{warn}")

    for emp in sorted(set(employees) - seen):
        if emp not in app.gallery.counts():
            print(f"! {emp} ({employees[emp]}): no photos, this person cannot be recognised")
