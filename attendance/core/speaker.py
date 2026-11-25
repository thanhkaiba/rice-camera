"""The speaker the door greets through (macOS): which output device the computer plays on, its volume
and whether it is muted, so a pulled plug or a muted computer is told to the system space instead of
going unnoticed (greetings were "said" for a morning that nobody heard).

Only what the computer itself knows is read: a speaker on the headphone socket that is switched off,
or turned down by its own knob, looks the same as a working one. `test` plays a short sound for a
person at the office to hear.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time

CHECK_EVERY_S = 600  # the state is read this often; reading makes no sound
TEST_SOUND = "/System/Library/Sounds/Glass.aiff"


def _run(args: list[str], timeout: float = 10.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_devices(text: str) -> tuple[str | None, list[str]]:
    """`system_profiler SPAudioDataType -json` -> (the default output device, every output device)."""
    try:
        groups = json.loads(text).get("SPAudioDataType", [])
    except (ValueError, AttributeError):
        return None, []
    current, names = None, []
    for group in groups if isinstance(groups, list) else []:
        for item in (group.get("_items") or []) if isinstance(group, dict) else []:
            if not isinstance(item, dict) or not item.get("coreaudio_device_output") or not item.get("_name"):
                continue
            names.append(str(item["_name"]))
            if item.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                current = str(item["_name"])
    return current, names


def parse_volume(text: str) -> tuple[int | None, bool | None]:
    """`osascript -e 'get volume settings'` -> (output volume 0-100, muted)."""
    volume = re.search(r"output volume:(\d+)", text)
    muted = re.search(r"output muted:(true|false)", text)
    return (int(volume.group(1)) if volume else None), (muted.group(1) == "true" if muted else None)


def read() -> dict | None:
    """The speaker state now; None where it cannot be read (not macOS)."""
    if sys.platform != "darwin":
        return None
    device, devices = parse_devices(_run(["system_profiler", "SPAudioDataType", "-json"]))
    volume, muted = parse_volume(_run(["osascript", "-e", "get volume settings"], timeout=5.0))
    if device is None and volume is None:
        return None
    return {"device": device, "devices": devices, "volume": volume, "muted": muted, "at": time.time()}


def problems(state: dict, want: str, min_volume: int, failed: str | None = None,
             heard: dict | None = None) -> dict[str, str]:
    """Problem id -> chat text. `want` is the device the greetings should play on ("" = any),
    `min_volume` the lowest volume that is fine (0 = not checked), `failed` the last greeting that
    could not be played, `heard` the microphone check (listen.Listener.status) when it is on."""
    from .listen import MIC_DENIED, SILENT_ALERT

    found = {}
    if heard:
        if heard.get("allowed") is False:
            found["speaker_mic"] = MIC_DENIED
        elif (heard.get("silent_in_a_row") or 0) >= SILENT_ALERT:
            found["speaker_silent"] = (f"🔇 *The door speaker seems silent*: the office computer's microphone did not hear "
                                       f"the last {heard['silent_in_a_row']} greetings. Check the speaker's power, its "
                                       "plug and its own volume knob.")
    device = state.get("device")
    if want and device and device.casefold() != want.casefold():
        gone = want.casefold() not in {d.casefold() for d in state.get("devices", [])}
        found["speaker_device"] = (f"🔇 *The door greetings are not playing on {want}*: the attendance computer plays on "
                                   f"{device} now" + (f" ({want} is not connected: check its plug)." if gone else
                                                      f". Choose {want} as the sound output again."))
    if state.get("muted"):
        found["speaker_muted"] = "🔇 *The attendance computer is muted*: nobody hears the door greetings. Turn its sound on."
    elif min_volume and state.get("volume") is not None and state["volume"] < min_volume:
        found["speaker_volume"] = (f"🔉 *The attendance computer's volume is at {state['volume']}%* (should be at least "
                                   f"{min_volume}%): the door greetings are hard to hear.")
    if failed:
        found["speaker_play"] = f"🔇 *A door greeting could not be played*: {failed}"
    return found


RECOVERED = {"speaker_device": "✅ The door greetings play on the right speaker again.",
             "speaker_muted": "✅ The attendance computer is no longer muted.",
             "speaker_volume": "✅ The attendance computer's volume is high enough again.",
             "speaker_play": "✅ The door greetings are playing again.",
             "speaker_silent": "✅ The office computer's microphone hears the door speaker again.",
             "speaker_mic": "✅ The office computer may use its microphone again."}


def describe(state: dict | None) -> str:
    if not state:
        return "the speaker state cannot be read on this computer"
    volume = "volume unknown" if state.get("volume") is None else f"volume {state['volume']}%"
    return f"{state.get('device') or 'unknown output'} · {volume} · {'MUTED' if state.get('muted') else 'not muted'}"


def test(want: str = "", min_volume: int = 0, play=None, listener=None) -> dict:
    """Plays a short sound on the computer's output and tells what it played on. With `listener`
    (listen.Listener) the computer's microphone listens too and tells whether the speaker made the sound;
    without, a person has to hear it."""
    state = read()
    if sys.platform != "darwin":
        return {"ok": False, "message": "the speaker test works on macOS only"}
    heard = None
    try:
        if listener is not None:
            from .greeting import play_audio
            from .listen import chime

            audio, rate = chime()
            heard = listener.play_and_check(play or play_audio, audio, rate, "test", wait_s=30.0)
        else:
            (play or (lambda: subprocess.run(["afplay", TEST_SOUND], timeout=15, check=True, capture_output=True)))()
    except Exception as exc:
        return {"ok": False, "message": f"could not play the test sound: {type(exc).__name__}: {exc}"[:300] + f" · {describe(state)}"}
    found = problems(state, want, min_volume) if state else {}
    message = f"played a test sound on {describe(state)}"
    if heard is not None:
        message += " · " + str(heard.get("message") or "")
        if heard.get("heard") is True:
            message += ": the speaker works"
        elif heard.get("heard") is False:
            message += ": the speaker is switched off, asleep, unplugged or turned down by its own knob"
    if found:
        return {"ok": False, "message": message + " · " + " ".join(re.sub(r"[*🔇🔉]", "", t).strip() for t in found.values()),
                "heard": (heard or {}).get("heard")}
    if heard is not None:
        return {"ok": heard.get("heard") is not False, "message": message, "heard": heard.get("heard")}
    return {"ok": True, "message": message + " · if nobody heard it, check the speaker's power and its own volume"}
