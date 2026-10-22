"""Enrol a person at the door: their next walks past the camera become their reference photos.

A reference photo taken by the door camera itself matches what that camera will see later (its
angle, its light, the distance, a mask, a motion-blurred face) far better than an ID photo or a
phone selfie. An admin picks the employee on the Camera page; the page then walks the person
through five short steps, in big letters over the live picture (and read aloud if wanted):

    look at the camera · come close · turn to one side · the other side · mask on

The steps come one at a time. Each is ticked by what the camera sees (head pose, face size, a
covered lower face), but only once the person has held it for a moment and had time to read it:
a single frame with a noisy head-pose estimate, or someone who happens to stand close already,
must not tick the step before they did anything. While a step's pose is held, a couple of clear
faces in that pose are kept as references, so each view ends up in the gallery. A step the
person cannot do (no mask at hand) can be skipped by the admin; when the time runs out the
admin can continue.

Safety: nothing is kept while more than one face is in view (so a colleague walking along is
never learned as this person), nor a face that clearly matches another employee. Arrivals are
not recorded while enrolment is on: it is a rehearsal, not a sighting.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from ..vision.recognizer import crop_face
from ..vision.tracker import clear_match

DURATION_S = 90.0
EXTRA_S = 60.0  # 'Continue' after the time ran out
PER_STEP = 2  # faces kept in each step's pose
HOLD_S = 1.0  # a step is done once its pose is held this long,
STEP_MIN_S = 2.5  # and no sooner than this after it was shown, so the person can read (or hear) it
GAP_S = 0.4  # a missed frame or two this short does not break a held pose
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
MAX_FACES = PER_STEP * len(STEPS)
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
    def __init__(self, library, store, recognition, now: Callable[[], float] = time.time,
                 name_of: Callable[[str], str] | None = None):
        """`library`: FaceLibrary (keeps the faces); `recognition`: the RecognitionConfig (quality limits);
        `name_of`: how to name an employee by id (the email name, if Settings say so)."""
        self._library, self._store, self._cfg, self._now = library, store, recognition, now
        self._name_of = name_of or (lambda employee_id: self._store.employees().get(employee_id, employee_id))
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
        self._step_since = 0.0  # when the current step was shown
        self._holding: float | None = None  # since when its pose is held
        self._last_met = 0.0  # the last frame that showed it
        self._step_kept = 0
        self._side = 0.0  # the way the head turned for "side": "other" is the opposite way

    def start(self, employee_id: str) -> str:
        """Begin enrolling `employee_id`; returns their name. Raises ValueError for an unknown id."""
        if employee_id not in self._store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        name = self._name_of(employee_id)
        with self._lock:
            self._employee, self._last, self._name, self._kept, self._done = employee_id, employee_id, name, [], {}
            self._until = self._now() + DURATION_S
            self._note = "Only one person in the picture, please."
            self._side = 0.0
            self._new_step()
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
            self._new_step()
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

    def _new_step(self) -> None:
        """Call with the lock held: the current step is shown from now on."""
        self._instruction = self._next_instruction()
        self._step_since, self._holding, self._step_kept = self._now(), None, 0

    def _after_step(self) -> None:
        """Call with the lock held, after a step was ticked or skipped."""
        self._new_step()
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
                other = self._name_of(candidates[0][0])
                self._note = f"This face clearly matches {other}, not {self._name}; nothing kept."
                return False

            step = self._current()
            met = step is not None and self._meets(step, face, frame)
            kept = met and self._step_kept < PER_STEP and self._keep(employee, face, frame)
            self._step_kept += kept
            ticked = self._hold(step, met, face.yaw)
            if self._employee is None:  # finished on this frame: the last step
                return kept
            progress = f"{len(self._kept)}/{MAX_FACES} kept."
            if ticked:
                guidance = f"{dict(STEPS)[ticked]}: done."
            elif met:
                guidance = "Hold it…"
            elif abs(face.yaw) > self._cfg.max_yaw + TURN_MAX:
                guidance = "Turned too far; a little less."
            elif step in ("side", "other") and abs(face.yaw) < TURN:
                guidance = "Turn your head further, slowly."
            elif step == "other":
                guidance = "Now the other way."
            elif step == "close":
                guidance = "Closer still."
            elif step == "straight":
                guidance = "Look straight at the camera."
            elif step == "mask":
                guidance = "No mask seen yet: cover your nose and mouth."
            else:
                guidance = ""
            self._note = f"{progress} {guidance}".strip()
            return kept

    def _meets(self, step: str, face, frame) -> bool:
        """Whether this face shows what `step` asks for."""
        if step == "straight":
            return abs(face.yaw) <= STRAIGHT and abs(face.pitch) <= STRAIGHT + 3
        if step == "close":
            return face.size >= CLOSE * self._cfg.min_face_px
        if step in ("side", "other"):
            turned = TURN <= abs(face.yaw) <= self._cfg.max_yaw + TURN_MAX
            # either way for the first turn, then the opposite way
            return turned and (step == "side" or self._side == 0.0 or face.yaw * self._side < 0)
        if step == "mask":
            return looks_masked(frame, face.landmarks)
        return False

    def _hold(self, step: str | None, met: bool, yaw: float) -> str | None:
        """Tick `step` once its pose was held for HOLD_S and it was shown for STEP_MIN_S; returns it then."""
        now = self._now()
        if not met:
            if self._holding is not None and now - self._last_met > GAP_S:
                self._holding = None
            return None
        if self._holding is None or now - self._last_met > GAP_S:
            self._holding = now
        self._last_met = now
        if step is None or now - self._holding < HOLD_S or now - self._step_since < STEP_MIN_S:
            return None
        if step == "side":
            self._side = yaw
        self._done[step] = "seen"
        self._after_step()
        return step

    def _keep(self, employee: str, face, frame) -> bool:
        if any(float(np.dot(face.embedding, kept)) >= DISTINCT for kept in self._kept):
            return False
        if not self._library.keep_face(employee, face.embedding, crop_face(frame, face.bbox)):
            return False
        self._kept.append(face.embedding)
        return True
