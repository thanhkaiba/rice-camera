"""Enrol a person at the door: their next walks past the camera become their reference photos.

A reference photo taken by the door camera itself matches what that camera will see later (its
angle, its light, the distance, a mask, a motion-blurred face) far better than an ID photo or a
phone selfie. An admin picks the employee on the Camera page; the page then walks the person
through five short steps, in big letters over the live picture (and read aloud if wanted):

    look at the camera · come close · turn to one side · the other side · mask on

Each step is ticked by what the camera sees (head pose, face size, a covered lower face), so
the person is told exactly what is still missing and when there is enough. Meanwhile every clear
face that adds a new view is kept as a reference, up to a handful. A step the person cannot do
(no mask at hand) can be skipped by the admin; when the time runs out the admin can continue.

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
EXTRA_S = 60.0  # 'Continue' after the time ran out
MAX_FACES = 8
DISTINCT = 0.9  # a face this similar to one kept in this session is the same view again
TURN = 15.0  # degrees of yaw that count as "turned to one side"
TURN_MAX = 20.0  # degrees beyond max_yaw: turned so far the face is no longer a useful reference
STRAIGHT = 12.0  # within this many degrees of yaw and pitch counts as looking at the camera
CLOSE = 2.0  # a face this many times min_face_px counts as close to the camera
MASK_COLOUR = 40.0  # Lab distance between forehead and mouth/chin that says the lower face is covered
MASK_TEXTURE = 28.0  # a mask is flat; a beard is not

STEPS = (
    ("straight", "Look at the camera"),
    ("close", "Come close to the camera"),
    ("side", "Turn your head to one side"),
    ("other", "Now turn to the other side"),
    ("mask", "Put a mask on and look at the camera"),
)
STEP_HINTS = {
    "straight": "Stand still a moment, face towards the lens.",
    "close": "Two or three steps closer, until your face fills more of the picture.",
    "side": "Turn your head about a quarter turn, slowly.",
    "other": "Same again, the other way.",
    "mask": "Any mask will do; this teaches the camera your masked face. Skip if you have none.",
}


def looks_masked(frame: np.ndarray, landmarks: np.ndarray | None) -> bool:
    """Whether the lower half of the face is covered: the mouth and chin are a flat patch of a
    colour unlike the forehead. A rough but cheap test; a beard may pass as a mask, which only
    ticks the step early."""
    if landmarks is None or frame is None or frame.ndim != 3:
        return False
    import cv2

    eyes, mouth = landmarks[:2], landmarks[3:5]
    eye_mid, mouth_mid = eyes.mean(axis=0), mouth.mean(axis=0)
    down = mouth_mid - eye_mid
    r = max(3, int(float(np.linalg.norm(eyes[1] - eyes[0])) * 0.18))
    h, w = frame.shape[:2]

    def patch(centre):
        x, y = int(centre[0]), int(centre[1])
        p = frame[max(0, y - r):min(h, y + r), max(0, x - r):min(w, x + r)]
        return p if p.size else None

    forehead = patch(eye_mid - down * 0.6)
    lower = patch(mouth_mid + down * 0.2)
    if forehead is None or lower is None:
        return False
    lab = [cv2.cvtColor(p, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32) for p in (forehead, lower)]
    colour = float(np.linalg.norm(lab[0].mean(axis=0) - lab[1].mean(axis=0)))
    texture = float(lab[1][:, 0].std())
    return colour >= MASK_COLOUR and texture <= MASK_TEXTURE


class DoorEnrolment:
    def __init__(self, library, store, recognition, now: Callable[[], float] = time.time):
        """`library`: FaceLibrary (keeps the faces); `recognition`: the RecognitionConfig (quality limits)."""
        self._library, self._store, self._cfg, self._now = library, store, recognition, now
        self._lock = threading.Lock()
        self._employee: str | None = None
        self._last: str | None = None  # the employee of the session that just ended (for Continue)
        self._name = ""
        self._until = 0.0
        self._ended = 0.0
        self._kept: list[np.ndarray] = []
        self._done: dict[str, str] = {}  # step -> "seen" | "skipped"
        self._note = ""
        self._instruction = ""

    def start(self, employee_id: str) -> str:
        """Begin enrolling `employee_id`; returns their name. Raises ValueError for an unknown id."""
        name = self._store.employees().get(employee_id)
        if name is None:
            raise ValueError(f"unknown employee {employee_id}")
        with self._lock:
            self._employee, self._last, self._name, self._kept, self._done = employee_id, employee_id, name, [], {}
            self._until = self._now() + DURATION_S
            self._note = "Only one person in the picture, please."
            self._instruction = self._next_instruction()
        return name

    def resume(self) -> bool:
        """Give the session that just ran out more time, keeping its progress."""
        with self._lock:
            if (self._employee is not None or self._last is None or not self._missing()
                    or self._store.employees().get(self._last) is None):
                return False
            self._employee = self._last
            self._until = self._now() + EXTRA_S
            self._note = ""
            self._instruction = self._next_instruction()
            return True

    def skip(self) -> str | None:
        """Mark the current step as skipped (the admin says it cannot be done); returns its key."""
        with self._lock:
            if self._employee is None:
                return None
            step = self._current()
            if step is None:
                return None
            self._done[step] = "skipped"
            self._after_step()
            return step

    def stop(self) -> None:
        with self._lock:
            self._finish("stopped")

    # -- state -----------------------------------------------------------------------------------

    def _current(self) -> str | None:
        return next((key for key, _ in STEPS if key not in self._done), None)

    def _next_instruction(self) -> str:
        step = self._current()
        return dict(STEPS)[step] if step else "Enough, thank you!"

    def _missing(self) -> list[str]:
        return [label for key, label in STEPS if key not in self._done]

    def _after_step(self) -> None:
        """Call with the lock held, after a step was ticked or skipped."""
        self._instruction = self._next_instruction()
        if self._current() is None and self._kept:
            self._finish()

    def _finish(self, how: str = "time is up") -> None:
        """Call with the lock held. `how`: why it ended, for the note ("time is up" or "stopped")."""
        if self._employee is not None:
            missing = self._missing()
            n = len(self._kept)
            if not n:
                self._note = f"Done: no usable face of {self._name} was seen; try again, closer and in the light."
                self._instruction = "No usable face was seen"
            elif missing:
                left = ", ".join(m.lower() for m in missing)
                self._note = (f"{how.capitalize()}: {n} face(s) of {self._name} kept as reference photos, "
                              f"but not yet: {left}. Continue for one more minute, or leave it here.")
                self._instruction = f"{how.capitalize()}; not yet: {left}"
            else:
                self._note = f"Done: {n} face(s) of {self._name} kept as reference photos, every view covered."
                self._instruction = "Enough, thank you!"
            self._ended = self._now()
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
            missing = self._missing()
            return {"active": active, "employee": self._employee, "name": self._name if (active or self._last) else "",
                    "seconds_left": max(0, int(self._until - self._now())) if active else 0,
                    "kept": len(self._kept), "max_faces": MAX_FACES, "note": self._note,
                    "instruction": self._instruction,
                    "hint": STEP_HINTS.get(self._current() or "", "") if active else "",
                    "steps": [{"key": k, "label": label, "done": self._done.get(k)} for k, label in STEPS],
                    "complete": not missing and bool(self._kept),
                    "can_continue": not active and self._last is not None and bool(missing) and self._ended > 0,
                    "ended_ago": round(self._now() - self._ended) if (not active and self._ended) else None}

    # -- the camera's view ------------------------------------------------------------------------

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
                self._note = "The face is blurred; stand still a moment."
                return False
            if candidates and candidates[0][0] != employee and clear_match(candidates, self._cfg):
                other = self._store.employees().get(candidates[0][0], candidates[0][0])
                self._note = f"This face clearly matches {other}, not {self._name}; nothing kept."
                return False

            kept = self._keep(employee, face, frame)
            ticked = self._tick(face, frame)
            if self._employee is None:  # finished on this frame: the last step, or enough faces
                return kept
            progress = f"{len(self._kept)}/{MAX_FACES} kept."
            current = self._current()
            if ticked:
                guidance = f"{dict(STEPS)[ticked]}: done."
            elif abs(face.yaw) > self._cfg.max_yaw + TURN_MAX:
                guidance = "Turned too far; a little less."
            elif current in ("side", "other") and abs(face.yaw) < TURN:
                guidance = "Turn your head further, slowly."
            elif current == "close":
                guidance = "Closer still."
            elif not kept:
                guidance = "Same view as before; follow the step shown."
            else:
                guidance = ""
            self._note = f"{progress} {guidance}".strip()
            return kept

    def _tick(self, face, frame) -> str | None:
        """Mark every step this face satisfies; returns the first newly ticked step."""
        seen = []
        if abs(face.yaw) <= STRAIGHT and abs(face.pitch) <= STRAIGHT + 3:
            seen.append("straight")
        if face.size >= CLOSE * self._cfg.min_face_px:
            seen.append("close")
        if TURN <= -face.yaw <= self._cfg.max_yaw + TURN_MAX:
            seen.append("side")
        if TURN <= face.yaw <= self._cfg.max_yaw + TURN_MAX:
            seen.append("other")
        if looks_masked(frame, face.landmarks):
            seen.append("mask")
        new = [s for s in seen if s not in self._done]
        for s in new:
            self._done[s] = "seen"
        if new:
            self._after_step()
        return new[0] if new else None

    def _keep(self, employee: str, face, frame) -> bool:
        if any(float(np.dot(face.embedding, kept)) >= DISTINCT for kept in self._kept):
            return False
        if not self._library.keep_face(employee, face.embedding, crop_face(frame, face.bbox)):
            return False
        self._kept.append(face.embedding)
        if len(self._kept) >= MAX_FACES and self._employee is not None:
            self._done.update({k: self._done.get(k, "seen") for k, _ in STEPS})  # enough views either way
            self._finish()
        return True
