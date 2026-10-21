"""Camera-based attendance: who arrived on time, who is late, who was not seen."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys

__version__ = "1.1.6"  # set from the release tag when the packages are built

_ALIASES: dict[str, str] = {
    # core
    "accuracy": "attendance.core.accuracy",
    "backup": "attendance.core.backup",
    "config": "attendance.core.config",
    "evidence": "attendance.core.evidence",
    "pipeline": "attendance.core.pipeline",
    "service": "attendance.core.service",
    "settings": "attendance.core.settings",
    # vision
    "camera": "attendance.vision.camera",
    "camera_check": "attendance.vision.camera_check",
    "faces": "attendance.vision.faces",
    "gallery": "attendance.vision.gallery",
    "live": "attendance.vision.live",
    "models": "attendance.vision.models",
    "person": "attendance.vision.person",
    "posecal": "attendance.vision.posecal",
    "processing": "attendance.vision.processing",
    "recognizer": "attendance.vision.recognizer",
    "tracker": "attendance.vision.tracker",
    # integrations
    "leave_api": "attendance.integrations.leave_api",
    "notifier": "attendance.integrations.notifier",
    # web
    "icons": "attendance.web.icons",
    "setup": "attendance.web.setup",
    # cli
    "benchmark": "attendance.cli.benchmark",
    "enroll": "attendance.cli.enroll",
    "evaluate": "attendance.cli.evaluate",
    "launcher": "attendance.cli.launcher",
    "updater": "attendance.cli.updater",
}


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.InspectLoader):
    """Map legacy `attendance.<module>` imports to the reorganised subpackage modules."""

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.startswith("attendance."):
            short = fullname[len("attendance."):]
            if short in _ALIASES:
                return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec):
        short = spec.name[len("attendance."):]
        mod = importlib.import_module(_ALIASES[short])
        setattr(sys.modules[__name__], short, mod)
        return mod

    def exec_module(self, module) -> None:
        if module.__spec__ is not None:
            module.__spec__.name = module.__name__

    def get_code(self, fullname: str):
        short = fullname[len("attendance."):]
        target = _ALIASES.get(short)
        if not target:
            return None
        spec = importlib.util.find_spec(target)
        return spec.loader.get_code(target) if spec and spec.loader else None

    def get_source(self, fullname: str):
        short = fullname[len("attendance."):]
        target = _ALIASES.get(short)
        if not target:
            return None
        spec = importlib.util.find_spec(target)
        return spec.loader.get_source(target) if spec and spec.loader else None


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name: str):
    if name in _ALIASES:
        mod = importlib.import_module(_ALIASES[name])
        sys.modules[f"{__name__}.{name}"] = mod
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
