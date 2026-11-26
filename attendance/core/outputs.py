"""The two speakers the office MacBook can greet through: the external one on the headphone socket (the
Edifier MR4 at the door) and the computer's own.

The Edifier switches itself off after about 15 minutes without sound and only its own button switches it on
again. So every morning at CHECK_AT the computer plays the speaker test on the external speaker and listens
with its microphone (listen.py): heard, the greetings play there; not heard, they play on the computer's own
speaker, louder (BUILTIN_LOUDNESS). Each speaker is set to its own volume at every switch (Settings), until someone switches back
on the Settings page after pressing the speaker's button (Greeter.use_output).

The macOS output is chosen through CoreAudio (ctypes): nothing to install. The volume is set with osascript.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import subprocess
import sys

EXTERNAL, BUILTIN = "external", "builtin"
NAMES = {EXTERNAL: "the external speaker", BUILTIN: "the computer's own speaker"}
VOLUME = {EXTERNAL: 95, BUILTIN: 100}  # unless Settings say otherwise
BUILTIN_LOUDNESS = 2.0  # the greetings on the computer's own speaker: as "Louder at the door" 2.0, or more if set
KEY = "speaker_output"  # store: {"output", "why", "at", "day", "heard"}, the speaker in use and why
CHECK_AT = dt.time(7, 0)  # the morning check; later that day when the app was not running at that time
CHECK_TRIES = 2  # the test is played again once when the microphone did not hear it for sure
SUPPORTED = sys.platform == "darwin"


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


SYSTEM_OBJECT = 1
GLOBAL, OUTPUT = _fourcc("glob"), _fourcc("outp")
UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_libs = None


def _coreaudio():
    global _libs
    if _libs is None:
        ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        address, size = ctypes.POINTER(_Address), ctypes.POINTER(ctypes.c_uint32)
        ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p, size]
        ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p, size,
                                                  ctypes.c_void_p]
        ca.AudioObjectSetPropertyData.argtypes = [ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
                                                  ctypes.c_uint32, ctypes.c_void_p]
        for call in (ca.AudioObjectGetPropertyDataSize, ca.AudioObjectGetPropertyData, ca.AudioObjectSetPropertyData):
            call.restype = ctypes.c_int32
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _libs = ca, cf
    return _libs


def _size(obj: int, selector: str, scope: int = GLOBAL) -> int:
    ca, _ = _coreaudio()
    where, size = _Address(_fourcc(selector), scope, 0), ctypes.c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(where), 0, None, ctypes.byref(size)):
        return 0
    return size.value


def _read(obj: int, selector: str, value, scope: int = GLOBAL) -> None:
    ca, _ = _coreaudio()
    where, size = _Address(_fourcc(selector), scope, 0), ctypes.c_uint32(ctypes.sizeof(value))
    status = ca.AudioObjectGetPropertyData(obj, ctypes.byref(where), 0, None, ctypes.byref(size), ctypes.byref(value))
    if status:
        raise OSError(f"CoreAudio could not read {selector} (status {status})")


def _name(device: int) -> str:
    _, cf = _coreaudio()
    ref = ctypes.c_void_p()
    _read(device, "lnam", ref)
    try:
        text = ctypes.create_string_buffer(256)
        return text.value.decode("utf-8", "replace") if cf.CFStringGetCString(ref, text, 256, UTF8) else ""
    finally:
        if ref.value:
            cf.CFRelease(ref)


def devices() -> list[dict]:
    """Every output device: {"id", "name", "builtin"} (built into the computer)."""
    count = _size(SYSTEM_OBJECT, "dev#") // 4
    ids = (ctypes.c_uint32 * count)()
    if count:
        _read(SYSTEM_OBJECT, "dev#", ids)
    found = []
    for device in ids:
        if not _size(device, "stm#", OUTPUT):
            continue  # a microphone
        transport = ctypes.c_uint32(0)
        try:
            _read(device, "tran", transport)
        except OSError:
            pass
        found.append({"id": int(device), "name": _name(device), "builtin": transport.value == _fourcc("bltn")})
    return found


def pick(found: list[dict], kind: str) -> dict | None:
    """The device of `kind` among `found` (devices()): the computer's own speaker, or the headphone socket."""
    if kind == BUILTIN:
        return next((d for d in found if d["builtin"] and "speaker" in d["name"].casefold()), None)
    return next((d for d in found if "headphone" in d["name"].casefold()), None)


def current() -> int | None:
    value = ctypes.c_uint32(0)
    _read(SYSTEM_OBJECT, "dOut", value)
    return value.value or None


def _choose(device: dict) -> None:
    if current() == device["id"]:
        return
    ca, _ = _coreaudio()
    where, value = _Address(_fourcc("dOut"), GLOBAL, 0), ctypes.c_uint32(device["id"])
    status = ca.AudioObjectSetPropertyData(SYSTEM_OBJECT, ctypes.byref(where), 0, None, 4, ctypes.byref(value))
    if status:
        raise OSError(f"CoreAudio could not choose {device['name']} (status {status})")


def use(kind: str, volume: int | None = None, run=subprocess.run) -> dict:
    """Makes `kind` the computer's output, at `volume` (else VOLUME[kind]) and not muted:
    {"ok", "device", "volume", "message"}."""
    if not SUPPORTED:
        return {"ok": False, "message": "choosing the speaker works on macOS only"}
    try:
        device = pick(devices(), kind)
        if device is None:
            return {"ok": False, "message": f"{NAMES[kind]} is not connected"}
        _choose(device)
    except OSError as exc:
        return {"ok": False, "message": str(exc)[:200]}
    volume = int(volume or VOLUME[kind])
    try:
        run(["osascript", "-e", f"set volume output volume {volume} without output muted"],
            capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "device": device["name"],
                "message": f"playing on {device['name']}, but its volume could not be set: {type(exc).__name__}"}
    return {"ok": True, "device": device["name"], "volume": volume,
            "message": f"playing on {device['name']} at volume {volume}%"}


def ensure(kind: str | None) -> None:
    """Before a greeting: `kind` is the output again if something else was chosen meanwhile (the volume stays)."""
    if not SUPPORTED or kind not in NAMES:
        return
    try:
        device = pick(devices(), kind)
        if device is not None:
            _choose(device)
    except OSError:
        pass
