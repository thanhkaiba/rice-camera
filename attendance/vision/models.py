"""Download the model files for a recognition backend into models/."""
from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

INSIGHTFACE_PACK = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
OPENCV_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
YOLOX = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/{}.onnx"
PERSON_MODELS = ("yolox_tiny", "yolox_s")
# U²-Net (small): cuts people out of their photo for the picture in the late message. Optional:
# without it OpenCV's GrabCut is used, which is rougher.
CUTOUT = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
CUTOUT_MODEL = "u2netp.onnx"

LICENCES = {
    "insightface": "InsightFace pretrained models: non-commercial research use only. "
                   "Commercial use requires a licence from InsightFace (contact@insightface.ai).",
    "opencv": "YuNet: MIT licence. SFace: Apache 2.0 licence. Free for commercial use.",
    "person": "YOLOX: Apache 2.0 licence. Free for commercial use.",
    "cutout": "U²-Net: Apache 2.0 licence. Free for commercial use.",
}

# Called with (bytes done, total bytes or 0 if unknown) while a file downloads.
Progress = Callable[[int, int], None]


def _print_progress(done: int, total: int) -> None:
    if total:
        print(f"\r  {done / total:4.0%}  {done >> 20} of {total >> 20} MB", end="", flush=True)


def _fetch_to(url: str, dest: Path, progress: Progress | None = None) -> None:
    print(f"Downloading {url}")
    progress = progress or _print_progress
    partial = dest.with_name(dest.name + ".part")  # a broken download never looks like a model
    with urllib.request.urlopen(url, timeout=120) as resp, open(partial, "wb") as out:
        total, done = int(resp.headers.get("Content-Length") or 0), 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            progress(done, total)
    if total and done < total:
        partial.unlink()
        raise RuntimeError(f"Download of {url} stopped at {done >> 20} of {total >> 20} MB")
    partial.replace(dest)
    print()


def _fetch(url: str) -> bytes:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def missing(cfg) -> list[Path]:
    """Model files the current settings need that are not on disk."""
    files = [cfg.path(f) for f in cfg.recognition.model_files() if f]
    if cfg.tracking.mode == "person":
        files.append(cfg.path(cfg.tracking.person_model))
    return [f for f in files if not f.exists()]


def cutout_file(cfg) -> Path | None:
    """The cutout model when it is on disk, else None (GrabCut is used then)."""
    path = cfg.path("models") / CUTOUT_MODEL
    return path if path.exists() else None


def cutout_wanted(cfg) -> bool:
    """The picture in the late message is on and its cutout model is not here yet."""
    return cfg.late_message.photo and cutout_file(cfg) is None


def download_cutout(models_dir: Path, progress: Progress | None = None) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = models_dir / CUTOUT_MODEL
    _fetch_to(CUTOUT, dest, progress)
    print(f"Saved to {dest}")
    print(f"Licence: {LICENCES['cutout']}")


def ensure(cfg, progress: Progress | None = None) -> bool:
    """Download what `missing` lists, if it is a default model this program knows how to get.
    Returns True when something was downloaded."""
    need = missing(cfg)
    if not need:
        return False
    models_dir = cfg.path("models")
    rec = {cfg.path(f) for f in cfg.recognition.model_files() if f}
    if rec & set(need):
        download(cfg.recognition.backend, models_dir, progress)
    person = cfg.path(cfg.tracking.person_model)
    if person in need and person.stem in PERSON_MODELS:
        download_person(person.stem, models_dir, progress)
    still = missing(cfg)
    if still:
        raise RuntimeError("Model file not found: " + ", ".join(str(f) for f in still))
    return True


def download(backend: str, models_dir: Path, progress: Progress | None = None) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    if backend == "insightface":
        archive = models_dir / "buffalo_l.zip"  # ~290 MB; only two of its models are needed
        _fetch_to(INSIGHTFACE_PACK, archive, progress)
        with zipfile.ZipFile(archive) as pack:
            for name in ("det_10g.onnx", "w600k_r50.onnx"):
                member = next(m for m in pack.namelist() if m.endswith(name))
                partial = models_dir / (name + ".part")
                with pack.open(member) as src, open(partial, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                partial.replace(models_dir / name)
        archive.unlink()
    elif backend == "opencv":
        for path in ("face_detection_yunet/face_detection_yunet_2023mar.onnx",
                     "face_recognition_sface/face_recognition_sface_2021dec.onnx"):
            data = _fetch(OPENCV_ZOO + path)
            if len(data) < 10_000:  # a Git LFS pointer instead of the model
                raise RuntimeError(f"Download of {path} failed (got {len(data)} bytes)")
            (models_dir / Path(path).name).write_bytes(data)
    else:
        raise ValueError(f"unknown backend {backend}")
    print(f"Saved to {models_dir}")
    print(f"Licence: {LICENCES[backend]}")


def download_person(name: str, models_dir: Path, progress: Progress | None = None) -> None:
    """YOLOX person detector for tracking.mode: person."""
    if name not in PERSON_MODELS:
        raise ValueError(f"unknown person model {name}")
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = models_dir / f"{name}.onnx"
    _fetch_to(YOLOX.format(name), dest, progress)
    print(f"Saved to {dest}")
    print(f"Licence: {LICENCES['person']}")
