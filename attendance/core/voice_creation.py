"""Create a draft voice locally, preview it, and activate it only after the admin chooses it."""
from __future__ import annotations

import io
import json
import logging
import math
import shutil
import threading
import uuid

import numpy as np

from .voice import check_voice_name, install_voice
from .voice_models import ensure as ensure_voice_models

log = logging.getLogger(__name__)
STATUS_KEY = "voice_creation"
MAX_REFERENCE_BYTES = 25 * 1024 * 1024


def prepare_reference(data: bytes) -> np.ndarray:
    import soundfile as sf

    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError("The WAV must be at most 25 MB.")
    try:
        with sf.SoundFile(io.BytesIO(data)) as source:
            if (source.format not in ("WAV", "WAVEX") or source.channels not in (1, 2)
                    or not 8000 <= source.samplerate <= 192000 or not 3 <= source.frames / source.samplerate <= 30):
                raise ValueError("Choose a 3–30 second WAV with one or two channels (8–192 kHz).")
            rate = source.samplerate
            audio = source.read(dtype="float32", always_2d=True)
    except (RuntimeError, OSError) as exc:
        raise ValueError("The recording could not be read as a WAV file.") from exc
    if (not 3 <= len(audio) / rate <= 30 or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 0.0001
            or np.max(np.abs(audio)) > 1.01):
        raise ValueError("The WAV must contain audible, finite audio within the normal volume range.")
    if rate != 48000:
        from scipy.signal import resample_poly

        factor = math.gcd(rate, 48000)
        audio = resample_poly(audio, 48000 // factor, rate // factor, axis=0)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    return np.ascontiguousarray(audio.T[None], dtype=np.float32)


def encode_reference(cfg, waveform: np.ndarray) -> np.ndarray:
    import onnxruntime as ort

    ensure_voice_models(cfg, creation=True)
    root = cfg.path("models/ZeroTTS/onnx")
    required = (root / "codec/moss_audio_tokenizer_encode.onnx", root / "codec/moss_audio_tokenizer_encode.data",
                root / "voice_encoder.onnx")
    if not all(path.is_file() for path in required):
        raise ValueError("Voice creation models are missing on this computer. See docs/voice-local.md for setup.")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    codec = ort.InferenceSession(str(required[0]), sess_options=options, providers=["CPUExecutionProvider"])
    codes, lengths = codec.run(None, {"waveform": waveform,
                                     "input_lengths": np.array([waveform.shape[2]], dtype=np.int32)})
    count = int(lengths[0])
    if (not 1 <= count <= 375 or codes.ndim != 3 or codes.shape[0] != 1
            or codes.shape[2] != 16 or codes.shape[1] < count):
        raise ValueError("The audio encoder returned incompatible code dimensions.")
    if np.min(codes[:, :count]) < 0 or np.max(codes[:, :count]) >= 1024:
        raise ValueError("The audio encoder returned invalid code values.")
    ref = np.zeros((1, 375, 16), dtype=np.int64)
    ref[:, :count] = codes[:, :count]
    encoder = ort.InferenceSession(str(required[2]), sess_options=options, providers=["CPUExecutionProvider"])
    return encoder.run(["voice_emb"], {"ref_codes": ref, "ref_lengths": np.array([count], dtype=np.int64)})[0]


class VoiceCreator:
    def __init__(self, cfg, store, greeter):
        self.cfg, self.store, self.greeter = cfg, store, greeter
        self.root = cfg.path(cfg.data_dir) / "voice-drafts"
        self._lock = threading.Lock()
        self._thread = None
        status = self.status()
        if status.get("state") == "working":
            self._save({**status, "state": "error", "message": "Voice creation was interrupted. Upload the recording again."})

    def status(self) -> dict:
        return json.loads(self.store.get(STATUS_KEY) or "{}")

    def _save(self, status: dict) -> None:
        self.store.put(STATUS_KEY, json.dumps(status))

    def start(self, name: str, data: bytes) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise BlockingIOError("Another voice is being created. Wait for it to finish.")
            check_voice_name(name)
            target = self.cfg.path("data/voices") / name
            if target.exists() or target.is_symlink():
                raise ValueError("That voice name is already installed. Choose a new name.")
            waveform = prepare_reference(data)
            status = {"id": uuid.uuid4().hex, "name": name, "state": "working", "message": "Preparing local models (first use downloads about 1 GB), then extracting the voice…"}
            self._save(status)
            self._thread = threading.Thread(target=self._run, args=(status, waveform), name="voice-create", daemon=True)
            self._thread.start()
            return status

    def _run(self, status: dict, waveform: np.ndarray) -> None:
        folder = self.root / status["id"]
        try:
            emb = encode_reference(self.cfg, waveform)
            pack = io.BytesIO()
            np.savez(pack, voice_emb=emb, n_voice_queries=np.array(10, dtype=np.int64))
            install_voice(folder, status["name"], pack.getvalue())
            self._save({**status, "message": "Generating a sample to listen to…"})
            self.greeter.render_voice(emb, folder / "preview.wav")
            self._save({**status, "state": "ready", "message": "Voice ready. Listen to the sample, then choose Use this voice."})
        except Exception as exc:
            log.exception("Local voice creation failed")
            shutil.rmtree(folder, ignore_errors=True)
            self._save({**status, "state": "error", "message": f"Could not create voice: {str(exc)[:300]}"})

    def use(self, job_id: str, activate) -> str:
        with self._lock:
            status = self.status()
            if status.get("id") != job_id or status.get("state") not in ("ready", "used"):
                raise ValueError("Create a voice and wait for its preview before selecting it.")
            if status["state"] == "used":
                return status["message"]
            pack = self.root / job_id / status["name"] / "voice.npz"
            message = activate(status["name"], pack.read_bytes())
            self._save({**status, "state": "used", "message": message})
            return message
