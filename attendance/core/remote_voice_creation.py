"""Create voice drafts on the camera; the server only validates and stores files."""
from __future__ import annotations

import base64
import datetime as dt
import io
import shutil
import threading
import time
import urllib.parse
import uuid

import numpy as np

from .voice import MAX_VOICE_BYTES, check_voice_name, install_voice
from .voice_creation import MAX_REFERENCE_BYTES, VoiceCreator

CREATION_TIMEOUT_S = 600


def validate_reference(data: bytes) -> None:
    import soundfile as sf

    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError("The WAV must be at most 25 MB.")
    try:
        with sf.SoundFile(io.BytesIO(data)) as source:
            if (source.format not in ("WAV", "WAVEX") or source.channels not in (1, 2)
                    or not 8000 <= source.samplerate <= 192000 or not 3 <= source.frames / source.samplerate <= 30):
                raise ValueError("Choose a 3–30 second WAV with one or two channels (8–192 kHz).")
            audio = source.read(dtype="float32", always_2d=True)
            if (not 3 <= len(audio) / source.samplerate <= 30 or not np.isfinite(audio).all()
                    or np.max(np.abs(audio)) < .0001 or np.max(np.abs(audio)) > 1.01):
                raise ValueError("The WAV must contain audible, finite audio within the normal volume range.")
    except (RuntimeError, OSError) as exc:
        raise ValueError("The recording could not be read as a WAV file.") from exc


class RemoteVoiceCreator(VoiceCreator):
    def __init__(self, cfg, store, previews: dict, preview_lock):
        self.previews, self.preview_lock = previews, preview_lock
        super().__init__(cfg, store, None)
        self._lock = threading.RLock()
        if self.status().get("state") == "error":
            self._cleanup(self.status())

    def _cleanup(self, status: dict) -> None:
        if status.get("image"):
            (self.cfg.path(self.cfg.data_dir) / status["image"]).unlink(missing_ok=True)
        if status.get("job_id"):
            self.store.edge_job_take(status["job_id"])
        with self.preview_lock:
            self.previews.pop(status.get("id"), None)

    def status(self) -> dict:
        with self._lock:
            status = super().status()
            if status.get("state") == "working" and time.time() >= status.get("expires", 0):
                self._cleanup(status)
                status = {**status, "state": "error", "message": "Voice creation timed out. Check that the camera is online and updated, then upload again."}
                self._save(status)
            return status

    def start(self, name: str, data: bytes) -> dict:
        with self._lock:
            if self.status().get("state") == "working":
                raise BlockingIOError("Another voice is being created. Wait for it to finish.")
            check_voice_name(name)
            target = self.cfg.path("data/voices") / name
            if target.exists() or target.is_symlink():
                raise ValueError("That voice name is already installed. Choose a new name.")
            validate_reference(data)
            token = uuid.uuid4().hex
            image = "uploads/" + uuid.uuid4().hex
            source = self.cfg.path(self.cfg.data_dir) / image
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            payload = {"kind": "create_voice", "preview_id": token, "name": name, "image": image,
                       "expires": time.time() + CREATION_TIMEOUT_S}
            try:
                with self.preview_lock:
                    job_id = self.store.edge_job_add("create_voice", payload, dt.datetime.now(dt.timezone.utc))
                    self.previews[token] = {"kind": "create_voice", "expires": payload["expires"], "job_id": job_id}
                    status = {**payload, "id": token, "job_id": job_id, "state": "working",
                              "message": "Creating the voice on the office Mac. First use may download models there…"}
                    self._save(status)
                return status
            except Exception:
                source.unlink(missing_ok=True)
                raise

    def complete(self, payload: dict, result: dict) -> None:
        with self._lock:
            status = self.status()
            if payload.get("preview_id") != status.get("id") or status.get("state") != "working":
                return
            folder = self.root / status["id"]
            try:
                if not result.get("ok"):
                    raise ValueError(str(result.get("message", "Camera could not create the voice."))[:400])
                with self.preview_lock:
                    preview = self.previews.get(status["id"], {}).get("result", {})
                if not preview.get("ok"):
                    raise ValueError("The camera did not upload the voice preview. Try again.")
                audio = preview["audio"]
                if len(audio) > 4 * 1024 * 1024 or not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
                    raise ValueError("Invalid voice preview WAV from camera.")
                encoded = result.get("voice_pack")
                if not isinstance(encoded, str) or len(encoded) > (MAX_VOICE_BYTES + 2) // 3 * 4:
                    raise ValueError("Invalid voice pack size from camera.")
                install_voice(folder, status["name"], base64.b64decode(encoded, validate=True))
                (folder / "preview.wav").write_bytes(audio)
                self._save({**status, "state": "ready", "message": "Voice ready. Listen to the sample, then choose Use this voice."})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                shutil.rmtree(folder, ignore_errors=True)
                self._save({**status, "state": "error", "message": f"Could not create voice: {str(exc)[:300]}"})
            finally:
                self._cleanup(status)


def run_on_camera(core, payload: dict) -> dict:
    """EdgeClient.on_create_voice callback: create a draft, never activate it."""
    if time.time() >= payload["expires"]:
        raise ValueError("Voice creation expired. Upload the recording again.")
    status, data = core.edge._send("GET", "/api/edge/files/" + urllib.parse.quote(payload["image"]),
                                  b"", "application/octet-stream")
    if status != 200:
        raise ConnectionError(f"Reference recording answered HTTP {status}")
    draft = core.voice_creator.start(payload["name"], data)
    del data
    core.voice_creator._thread.join(timeout=max(0, payload["expires"] - time.time()))
    if core.voice_creator._thread.is_alive():
        raise ValueError("Voice creation timed out on the camera. Try again after it finishes preparing models.")
    result = core.voice_creator.status()
    if result.get("id") != draft["id"] or result.get("state") != "ready":
        raise ValueError(result.get("message", "Camera could not create the voice."))
    folder = core.voice_creator.root / draft["id"]
    core.edge._upload_preview(payload["preview_id"], (folder / "preview.wav").read_bytes())
    return {"ok": True, "voice_pack": base64.b64encode((folder / draft["name"] / "voice.npz").read_bytes()).decode()}
