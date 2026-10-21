"""Enrol a person at the door: their next walks past the camera become their reference photos.

A reference photo taken by the door camera itself matches what that camera will see later (its
angle, its light, the distance, a mask, a motion-blurred face) far better than an ID photo or a
phone selfie. An admin picks the employee on the Camera page and asks them to walk past two or
three times, with and without a mask. For a minute and a half, every clear face in view is
compared with what was kept so far, and the ones that add a new view are kept, up to a handful.

Safety: nothing is kept while more than one face is in view (so a colleague walking along is
never learned as this person), nor a face that clearly matches another employee. Arrivals are
not recorded while enrolment is on: it is a rehearsal, not a sighting.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from .recognizer import crop_face
from .tracker import clear_match

DURATION_S = 90.0
MAX_FACES = 8
DISTINCT = 0.9  # a face this similar to one kept in this session is the same view again


class DoorEnrolment:
    def __init__(self, library, store, recognition, now: Callable[[], float] = time.time):
        """`library`: FaceLibrary (keeps the faces); `recognition`: the RecognitionConfig (quality limits)."""
        self._library, self._store, self._cfg, self._now = library, store, recognition, now
        self._lock = threading.Lock()
        self._employee: str | None = None
        self._name = ""
        self._until = 0.0
        self._kept: list[np.ndarray] = []
        self._note = ""

    def start(self, employee_id: str) -> str:
        """Begin enrolling `employee_id`; returns their name. Raises ValueError for an unknown id."""
        name = self._store.employees().get(employee_id)
        if name is None:
            raise ValueError(f"unknown employee {employee_id}")
        with self._lock:
            self._employee, self._name, self._kept = employee_id, name, []
            self._until = self._now() + DURATION_S
            self._note = "Walk towards the camera, two or three times, with and without a mask."
        return name

    def stop(self) -> None:
        with self._lock:
            self._finish()

    def _finish(self) -> None:
        """Call with the lock held."""
        if self._employee is not None:
            self._note = f"Done: {len(self._kept)} face(s) of {self._name} kept as reference photos."
            if not self._kept:
                self._note = f"Done: no usable face of {self._name} was seen; try again, closer and in the light."
        self._employee, self._until = None, 0.0

    @property
    def active(self) -> bool:
        with self._lock:
            if self._employee is not None and self._now() >= self._until:
                self._finish()
            return self._employee is not None

    def status(self) -> dict:
        active = self.active
        with self._lock:
            return {"active": active, "employee": self._employee, "name": self._name if active else "",
                    "seconds_left": max(0, int(self._until - self._now())) if active else 0,
                    "kept": len(self._kept), "note": self._note}

    def observe(self, faces, frame) -> bool:
        """The faces of one analysed frame, as (DetectedFace, gallery candidates) pairs, with the
        frame they were found in. Keeps at most one face per frame; returns whether it did."""
        if not self.active:
            return False
        with self._lock:
            employee = self._employee
            if employee is None:
                return False
            if not faces:
                self._note = "No face in view."
                return False
            if len(faces) > 1:
                self._note = "More than one face in view; nothing is kept until only one person is in the picture."
                return False
            face, candidates = faces[0]
            if not face.size_ok:
                self._note = "The face is too small or unclear; come closer."
                return False
            if face.sharpness < self._cfg.min_sharpness:
                self._note = "The face is blurred; walk a little slower."
                return False
            if candidates and candidates[0][0] != employee and clear_match(candidates, self._cfg):
                other = self._store.employees().get(candidates[0][0], candidates[0][0])
                self._note = f"This face clearly matches {other}, not {self._name}; nothing kept."
                return False
            if any(float(np.dot(face.embedding, kept)) >= DISTINCT for kept in self._kept):
                self._note = f"{len(self._kept)}/{MAX_FACES} kept. Same view as before; turn a little, or put the mask on/off."
                return False
            if not self._library.keep_face(employee, face.embedding, crop_face(frame, face.bbox)):
                self._note = f"{len(self._kept)}/{MAX_FACES} kept. Already have this view of {self._name}."
                return False
            self._kept.append(face.embedding)
            self._note = f"{len(self._kept)}/{MAX_FACES} kept."
            if len(self._kept) >= MAX_FACES:
                self._finish()
            return True
