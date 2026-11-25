"""Hearing the door speaker with the office computer's own microphone (macOS), to know whether it really made a
sound: a speaker on the headphone socket that is switched off, asleep or turned down by its own knob looks the
same to the computer as a working one (speaker.py).

While a greeting or the speaker test plays, a small helper app (mic_helper.swift, "FaceAttendance Mic") records
the microphone for a few seconds; the loudness over time of what was recorded is compared with that of what was
played. They rise and fall together when the speaker played it, even with people talking at the door. Nothing is
kept: the recording is deleted once compared.

The helper is built once on the office computer with the Command Line Tools' Swift compiler and kept in
<home>/mic, outside the versions folder: macOS asks once ("FaceAttendance Mic would like to access the
microphone") and the answer stays across app updates. It is built again only when its source changes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

APP = "FaceAttendance Mic.app"
EXE = "FaceAttendanceMic"
BUNDLE_ID = "com.faceattendance.mic"
SOURCE = Path(__file__).with_name("mic_helper.swift")
PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>
<key>CFBundleExecutable</key><string>{EXE}</string>
<key>CFBundleName</key><string>FaceAttendance Mic</string>
<key>CFBundleDisplayName</key><string>FaceAttendance Mic</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>12.0</string>
<key>NSMicrophoneUsageDescription</key><string>Face Attendance listens for a few seconds while the door speaker plays, to check that the speaker really makes a sound. Nothing is kept or sent.</string>
</dict></plist>
"""
RATE = 16000  # what the helper records
FRAME_S = 0.02  # loudness is compared in steps of 20 ms
LEAD_S = 1.5  # a greeting is not held back for the helper to start: it records this much longer instead
TAIL_S = 1.0  # the speaker and the room are a little late
LAG_S = (-0.25, 0.8)  # where the played sound may sit in the recording, around where the clocks put it
MIN_OVERLAP_S = 1.0  # less played sound than this in the recording: no answer
HEARD_AT = 0.5  # correlation of the loudness: at or above, the speaker played it
SILENT_BELOW = 0.25  # below: it did not; in between, no answer
MIC_DENIED = ("🎙️ The office computer may not use its microphone, so it cannot hear whether the door speaker "
              "plays. At the office computer: System Settings → Privacy & Security → Microphone → turn on FaceAttendance Mic.")


SILENT_ALERT = 3  # greetings in a row the microphone did not hear before the system space is told
ASKING_S = 600  # while macOS asks for the microphone, greetings do not start the helper again for this long


class MicError(RuntimeError):
    pass


def chime(rate: int = 24000):
    """(samples, rate): the speaker test, four short notes twice, about 2 seconds; easy to tell from voices."""
    import numpy as np

    parts = []
    for _ in range(2):
        for hz in (660, 880, 1100, 880):
            t = np.arange(int(0.18 * rate)) / rate
            note = 0.3 * np.sin(2 * np.pi * hz * t) * np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.01)
            parts += [note, np.zeros(int(0.07 * rate))]
        parts.append(np.zeros(int(0.25 * rate)))
    return np.concatenate(parts).astype(np.float32), rate


def envelope(samples, rate: int):
    """Loudness per FRAME_S of mono samples at `rate`, resampled to RATE, low rumble (and the 20 Hz keep-awake
    tone) taken out, compressed so a loud word does not drown the rest."""
    import numpy as np

    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    if rate != RATE and len(x):
        n = int(round(len(x) * RATE / rate))
        x = np.interp(np.arange(n) * (rate / RATE), np.arange(len(x)), x)
    x = np.diff(x, prepend=x[:1])  # a first difference: a gentle high-pass
    step = int(RATE * FRAME_S)
    m = len(x) // step
    if m == 0:
        return np.zeros(0)
    return np.sqrt(np.sqrt((x[: m * step].reshape(m, step) ** 2).mean(axis=1)) + 1e-9)


def compare(played, played_rate: int, recorded, offset_s: float) -> dict:
    """How well the recording follows what was played. `offset_s`: when the playing started, in seconds after the
    recording started (by the clocks; may be negative). {"corr", "lag_s", "overlap_s"}; corr None when too
    little of the played sound falls in the recording."""
    import numpy as np

    p, r = envelope(played, played_rate), envelope(recorded, RATE)
    best, best_lag, best_overlap = None, 0.0, 0.0
    want = int(round(offset_s / FRAME_S))
    for k in range(want + int(LAG_S[0] / FRAME_S), want + int(LAG_S[1] / FRAME_S) + 1):
        start_p, start_r = max(0, -k), max(0, k)
        n = min(len(p) - start_p, len(r) - start_r)
        if n * FRAME_S < MIN_OVERLAP_S:
            continue
        a, b = p[start_p:start_p + n], r[start_r:start_r + n]
        if a.std() < 1e-9 or b.std() < 1e-9:
            corr = 0.0
        else:
            corr = float(np.corrcoef(a, b)[0, 1])
        if best is None or corr > best:
            best, best_lag, best_overlap = corr, k * FRAME_S - offset_s, n * FRAME_S
    return {"corr": None if best is None else round(best, 3), "lag_s": round(best_lag, 2),
            "overlap_s": round(best_overlap, 2)}


def verdict(corr: float | None) -> bool | None:
    if corr is None:
        return None
    return True if corr >= HEARD_AT else False if corr < SILENT_BELOW else None


class Recording:
    """One run of the helper: started at once, read back with `samples`."""

    def __init__(self, app: Path, seconds: float, popen=subprocess.Popen):
        self.dir = Path(tempfile.mkdtemp(prefix="door-mic-"))
        self.out = self.dir / "heard.wav"
        self.seconds = seconds
        self.started = time.time()
        self.proc = popen(["open", "-W", "-n", "-g", "-j", "-a", str(app), "--args", str(self.out), f"{seconds:.2f}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def status(self) -> str:
        try:
            return (self.dir / "heard.wav.status").read_text().strip()
        except OSError:
            return ""

    def wait(self, timeout: float, until=("recording", "done", "denied", "failed")) -> str:
        end = time.time() + timeout
        while True:
            status = self.status()
            if status.startswith(until) or time.time() >= end:
                return status
            time.sleep(0.05)

    def samples(self, timeout: float):
        """(samples, unix time the recording started). Raises MicError."""
        import soundfile as sf

        try:
            status = self.wait(timeout, until=("done", "denied", "failed"))
            if status.startswith("denied"):
                raise MicError("denied")
            if not status.startswith("done"):
                raise MicError(status or "the microphone helper did not start")
            began = None
            try:
                with open(self.dir / "heard.wav.started", encoding="utf-8") as fh:
                    began = float(fh.read())
            except (OSError, ValueError):
                pass
            audio, rate = sf.read(str(self.out), dtype="float32")
            if rate != RATE:
                raise MicError(f"recorded at {rate} Hz")
            return audio, began
        finally:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            shutil.rmtree(self.dir, ignore_errors=True)


class Listener:
    """The microphone check. `allowed`: True once a recording worked, False when macOS said no, None unknown."""

    def __init__(self, home: Path, run=subprocess.run, popen=subprocess.Popen):
        self.home, self.run, self.popen = Path(home), run, popen
        self.allowed: bool | None = None
        self.last: dict | None = None  # the last answer: {"at", "kind", "heard", "corr", "message"}
        self.silent_in_a_row = 0
        self._asked_at = 0.0
        self._build_lock = threading.Lock()

    @property
    def app(self) -> Path:
        return self.home / "mic" / APP

    def _want(self) -> str:
        return hashlib.sha1(SOURCE.read_bytes() + PLIST.encode()).hexdigest()

    def ready(self) -> bool:
        stamp = self.home / "mic" / "built-from.sha1"
        try:
            return (self.app / "Contents" / "MacOS" / EXE).exists() and stamp.read_text().strip() == self._want()
        except OSError:
            return False

    def ensure(self) -> Path:
        """The helper app, built first when it is missing or its source changed. Raises MicError."""
        if sys.platform != "darwin":
            raise MicError("the microphone check works on macOS only")
        with self._build_lock:
            if self.ready():
                return self.app
            folder = self.home / "mic"
            new = folder / (APP + ".new")
            shutil.rmtree(new, ignore_errors=True)
            (new / "Contents" / "MacOS").mkdir(parents=True)
            (new / "Contents" / "Info.plist").write_text(PLIST, encoding="utf-8")
            try:
                built = self.run(["xcrun", "swiftc", "-O", "-o", str(new / "Contents" / "MacOS" / EXE), str(SOURCE)],
                                 capture_output=True, text=True, timeout=600)
                if built.returncode:
                    raise MicError("the microphone helper could not be built (are the Command Line Tools installed?): "
                                   + (built.stderr or built.stdout or "")[-300:])
                signed = self.run(["codesign", "--force", "--sign", "-", str(new)], capture_output=True, text=True, timeout=60)
                if signed.returncode:
                    raise MicError("the microphone helper could not be signed: " + (signed.stderr or "")[-300:])
            except (OSError, subprocess.SubprocessError) as exc:
                shutil.rmtree(new, ignore_errors=True)
                raise MicError(f"the microphone helper could not be built: {type(exc).__name__}: {exc}"[:400]) from None
            shutil.rmtree(self.app, ignore_errors=True)
            os.replace(new, self.app)
            (folder / "built-from.sha1").write_text(self._want(), encoding="utf-8")
            log.info("Microphone helper built: %s", self.app)
            return self.app

    def begin(self, seconds: float) -> Recording:
        return Recording(self.ensure(), seconds, self.popen)

    def judge(self, rec: Recording, played, rate: int, played_at: float, kind: str) -> dict:
        """Waits for the recording to end and compares it with what was played at `played_at` (unix time)."""
        result = {"at": time.time(), "kind": kind, "heard": None, "corr": None}
        try:
            audio, began = rec.samples(rec.seconds + 8.0)
        except MicError as exc:
            if str(exc) == "denied":
                self.allowed = False
                result["message"] = "the microphone is not allowed"
                result["denied"] = True
            elif str(exc).startswith("asking"):
                self._asked_at = time.time()
                result["message"] = ("macOS is asking at the office computer whether FaceAttendance Mic may use the "
                                     "microphone: click OK there")
            else:
                result["message"] = f"no recording: {exc}"[:300]
            self.last = result
            return result
        self.allowed = True
        began = began if began is not None else rec.started
        found = compare(played, rate, audio, played_at - began)
        heard = verdict(found["corr"])
        result.update(found, heard=heard)
        if found["corr"] is None:
            result["message"] = "the microphone started too late to hear it"
        else:
            result["message"] = (("heard by the microphone" if heard else "NOT heard by the microphone" if heard is False
                                  else "the microphone is not sure") + f" (match {found['corr']:.2f})")
        if heard is not None:
            self.silent_in_a_row = 0 if heard else self.silent_in_a_row + 1
        self.last = result
        log.log(logging.WARNING if heard is False else logging.INFO, "Speaker check (%s): %s", kind, result["message"])
        return result

    def may_listen(self) -> bool:
        """Whether a greeting listens now: the helper is built (building takes a while: see `build_soon`), macOS
        has not said no and is not asking right now."""
        return (sys.platform == "darwin" and self.allowed is not False and self.ready()
                and time.time() - self._asked_at >= ASKING_S)

    def build_soon(self) -> None:
        if sys.platform != "darwin" or self.ready():
            return

        def build():
            try:
                self.ensure()
            except Exception as exc:
                log.warning("Speaker check: %s", exc)

        threading.Thread(target=build, name="mic-build", daemon=True).start()

    def play_and_check(self, play, audio, rate: int, kind: str, wait_s: float = 0.0, background: bool = False):
        """Plays `audio` with `play(audio, rate)` while the microphone records. `wait_s`: how long the playing may
        wait for the microphone to start (the test waits; a greeting does not). `background`: compare in a thread
        and return None at once. A microphone that cannot be used never stops the sound from playing."""
        rec = None
        try:
            rec = self.begin(len(audio) / rate + (0.5 if wait_s else LEAD_S) + TAIL_S)
            if wait_s:
                rec.wait(wait_s)
        except MicError as exc:
            log.warning("Speaker check: %s", exc)
            self.last = {"at": time.time(), "kind": kind, "heard": None, "corr": None, "message": str(exc)[:300]}
        except Exception:
            log.exception("The microphone could not be started")
        played_at = time.time()
        play(audio, rate)
        if rec is None:
            return self.last if not background else None
        if background:
            threading.Thread(target=self.judge, args=(rec, audio, rate, played_at, kind), name="speaker-check",
                             daemon=True).start()
            return None
        return self.judge(rec, audio, rate, played_at, kind)

    def status(self) -> dict | None:
        if self.last is None and self.allowed is None:
            return None
        if self.allowed is True and (self.last or {}).get("denied"):
            self.last = None
        last = dict(self.last or {})
        at = last.get("at")
        return {"allowed": self.allowed, "at": at, "at_text": time.strftime("%H:%M", time.localtime(at)) if at else "",
                "kind": last.get("kind"), "heard": last.get("heard"),
                "corr": last.get("corr"), "message": last.get("message"), "silent_in_a_row": self.silent_in_a_row}
