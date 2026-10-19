"""Settings edited on the web page, stored in the database on top of config.yaml.

Values are applied to the live Config object, so changes take effect without a restart.
Settings that need a restart (backend, web host/port, data_dir) are only in config.yaml.
"""
from __future__ import annotations

import copy
import json

from .config import Config
from ..store import Store

KEY = "settings"

# section ("" = top level) -> editable fields
EDITABLE: dict[str, list[str]] = {
    "schedule": ["window_start", "late_after", "window_end", "work_days"],
    "camera": ["url", "process_fps", "enhance_low_light", "area"],
    "tracking": ["mode", "person_conf", "full_frame_every"],
    "recognition": ["accept_score", "min_margin", "votes_required", "min_face_px", "min_det_score",
                    "min_sharpness", "max_yaw", "max_pitch", "auto_pose", "unknown_min_frames",
                    "learn_corrections", "learn_per_group", "group_days", "seen_again_score"],
    "google_chat": ["webhook_url", "mention_people", "notify_late_arrivals", "unknown_batch_minutes"],
    "leave_api": ["url", "token", "refresh_minutes"],
    "late_message": ["enabled", "intro", "item", "auto_send", "send_at"],
    "web": ["public_url", "password", "viewer_username", "viewer_password"],
    "updates": ["enabled"],
    "backup": ["folder", "time", "keep_days", "include_photos"],
    "": ["retention_days", "late_evidence_days", "healthcheck_url"],
}


def _section(cfg: Config, name: str):
    return cfg if name == "" else getattr(cfg, name)


def _coerce(current, value):
    if isinstance(current, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        items = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
        return sorted({int(v) for v in items})
    return str(value).strip()


def apply(cfg: Config, overrides: dict) -> None:
    """Validate overrides on a copy, then copy them into the live config. Raises ValueError."""
    trial = copy.deepcopy(cfg)
    for section, fields in overrides.items():
        if section not in EDITABLE:
            raise ValueError(f"unknown settings section {section!r}")
        target = _section(trial, section)
        for name, value in fields.items():
            if name not in EDITABLE[section]:
                raise ValueError(f"{section}.{name} cannot be changed here")
            try:
                setattr(target, name, _coerce(getattr(target, name), value))
            except (TypeError, ValueError):
                raise ValueError(f"invalid value for {name}: {value!r}") from None
    trial.validate()
    for section in EDITABLE:
        for name in EDITABLE[section]:
            setattr(_section(cfg, section), name, getattr(_section(trial, section), name))


def load(cfg: Config, store: Store) -> None:
    """Apply saved overrides at startup; a saved value that no longer validates is ignored."""
    saved = json.loads(store.get(KEY) or "{}")
    try:
        apply(cfg, saved)
    except ValueError:
        for section, fields in saved.items():
            for name, value in fields.items():
                try:
                    apply(cfg, {section: {name: value}})
                except ValueError:
                    pass


def save(cfg: Config, store: Store, changes: dict) -> None:
    """Validate and apply `changes`, then persist them merged with earlier overrides."""
    apply(cfg, changes)
    saved = json.loads(store.get(KEY) or "{}")
    for section, fields in changes.items():
        saved.setdefault(section, {}).update(fields)
    store.put(KEY, json.dumps(saved))


def current(cfg: Config) -> dict:
    return {s: {n: getattr(_section(cfg, s), n) for n in names} for s, names in EDITABLE.items()}
