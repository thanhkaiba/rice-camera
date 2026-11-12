"""Install a bounded, compatible ZeroTTS voice pack without trusting upload paths or pickle."""
from __future__ import annotations

import io
import re
import tempfile
import zipfile
import zlib
from pathlib import Path

import numpy as np

MAX_VOICE_BYTES = 128 * 1024


def preset_names() -> set[str]:
    from .voice_models import FILES

    return {name.split("/")[1] for name in FILES if name.startswith("voices/") and name.endswith("/voice.npz")}


def voice_names(cfg, *, presets: bool = False) -> list[str]:
    """Discover packs on this computer; include downloadable presets for the picker."""
    folders = [cfg.path("models/ZeroTTS/voices")]
    if cfg.greeting.zerotts_voices_dir:
        folders.append(cfg.path(cfg.greeting.zerotts_voices_dir))
    names = {path.parent.name for folder in folders for path in folder.glob("*/voice.npz") if path.is_file()}
    if presets:
        names.update(preset_names())
    return sorted(name for name in names if name != "auto" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name))


def check_voice_name(name: str) -> None:
    if name == "auto":
        raise ValueError("The name auto is reserved for daily voice rotation. Choose another voice name.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name):
        raise ValueError("Use a folder name with letters, numbers, _, - or . (at most 100 characters).")


def install_voice(root: Path, name: str, data: bytes) -> Path:
    check_voice_name(name)
    try:
        if len(data) > MAX_VOICE_BYTES:
            raise ValueError("voice.npz is too large (maximum 128 KB).")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(f.file_size for f in archive.infolist()) > MAX_VOICE_BYTES:
                raise ValueError("The unpacked voice is too large (maximum 128 KB).")
            if set(archive.namelist()) != {"voice_emb.npy", "n_voice_queries.npy"}:
                raise ValueError("Expected voice_emb and n_voice_queries arrays in voice.npz.")
            # Check headers before np.load can allocate from an untrusted array shape.
            for filename, expected in (("voice_emb.npy", (1, 10, 768)), ("n_voice_queries.npy", ())):
                with archive.open(filename) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version not in ((1, 0), (2, 0)):
                        raise ValueError("Unsupported voice array format.")
                    reader = (np.lib.format.read_array_header_1_0 if version == (1, 0)
                              else np.lib.format.read_array_header_2_0)
                    shape, _, dtype = reader(stream)
                    valid_dtype = dtype == np.float32 if expected else dtype.kind in "iu" and dtype.itemsize <= 8
                    if shape != expected or not valid_dtype:
                        raise ValueError("This voice's array shape or type is incompatible with ZeroTTS.")
        with np.load(io.BytesIO(data), allow_pickle=False) as pack:
            emb, queries = pack["voice_emb"], pack["n_voice_queries"]
            if (emb.shape != (1, 10, 768) or emb.dtype != np.float32 or not np.isfinite(emb).all()
                    or queries.shape != () or queries.dtype.kind not in "iu" or queries.item() != 10):
                raise ValueError("Expected finite float32 voice_emb with shape (1, 10, 768) and n_voice_queries=10.")
    except (OSError, EOFError, KeyError, TypeError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error) as exc:
        raise ValueError("This is not a valid ZeroTTS voice.npz file.") from exc

    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.exists() or target.is_symlink():
        raise ValueError(f"Voice {name} already exists. Rename the uploaded folder to keep both voices.")
    with tempfile.TemporaryDirectory(prefix=".voice-", dir=root) as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        np.savez(stage / "voice.npz", voice_emb=emb, n_voice_queries=np.array(10, dtype=np.int64))
        stage.rename(target)
    return target / "voice.npz"
