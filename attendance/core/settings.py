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
BASELINE = "_file_settings"  # attribute on the Config: the values from config.yaml, before overrides
PINNED_PASSWORD = "_pinned_admin_password"  # attribute on the Config: the server's CHECKIN_ADMIN_PASSWORD, never replaced
SECRET = {"password", "viewer_password", "token", "webhook_url", "system_webhook_url", "approver_webhook_url", "base_api_key", "base_account_key", "key", "elevenlabs_key", "elevenlabs_key2", "elevenlabs_key3"}  # never shown, only "set"/"empty"

# section ("" = top level) -> editable fields
EDITABLE: dict[str, list[str]] = {
    "schedule": ["window_start", "late_after", "window_end", "work_days", "maintenance"],
    "camera": ["url", "more", "process_fps", "decode_threads", "enhance_low_light", "area"],
    "tracking": ["mode", "person_conf", "full_frame_every"],
    "recognition": ["accept_score", "min_margin", "votes_required", "min_face_px", "small_face_px", "min_det_score",
                    "min_sharpness", "max_yaw", "max_pitch", "auto_pose", "unknown_min_frames",
                    "learn_corrections", "learn_per_group", "group_days", "seen_again_score", "suggest_score",
                    "same_face_score", "blurry_score", "auto_delete_blurry",
                    "auto_delete_photos_below"],
    "google_chat": ["webhook_url", "system_webhook_url", "mention_people", "notify_late_arrivals", "late_time_list", "daily_summary",
                    "unknown_batch_minutes", "names"],
    "camera_api": ["url", "key", "send_at", "include_late", "include_not_seen", "mode", "days_back",
                   "interval_seconds", "interval_start", "interval_end"],
    "leave_api": ["url", "token", "refresh_minutes", "pending_counts"],
    "late_message": ["enabled", "include_late", "include_not_seen", "intro", "item", "auto_send", "send_at", "photo", "occasions", "background", "space"],
    "pending_reminder": ["enabled", "auto_send", "time", "kinds"],
    "fine_notice": ["enabled", "send_at", "list_until", "appeal_until", "review_until", "approvers", "approver_webhook_url"],
    "web": ["public_url", "password", "viewer_username", "viewer_password"],
    "updates": ["enabled"],
    "backup": ["folder", "time", "keep_days", "include_photos"],
    "collect": ["enabled", "start", "end", "min_score", "per_day", "enough", "fps", "review_per_day", "skip_people", "angles"],
    "greeting": ["enabled", "key", "model", "engine", "engine_en", "zerotts_voice", "zerotts_voices_dir",
                 "voice", "voice_en", "rate", "until", "until_people", "zerotts_speed", "zerotts_liveliness",
                 "elevenlabs_key", "elevenlabs_voice", "elevenlabs_voice_en", "elevenlabs_key2", "elevenlabs_voice2",
                 "elevenlabs_key3", "elevenlabs_voice3", "elevenlabs_model", "elevenlabs_prefix", "elevenlabs_stability", "elevenlabs_speed", "loudness", "birthdays", "speaker", "speaker_min_volume", "speaker_awake", "days_off", "speaker_awake_from", "speaker_awake_every", "speaker_awake_level", "elevenlabs_emotions", "elevenlabs_tags", "style", "trends", "trends_research", "office", "location",
                 "unknown", "tease"],
    "": ["retention_days", "late_evidence_days", "healthcheck_url", "base_api_key", "base_account_key"],
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
    if pinned := getattr(cfg, PINNED_PASSWORD, ""):
        cfg.web.password = pinned


def load(cfg: Config, store: Store) -> None:
    """Apply saved overrides at startup; a saved value that no longer validates is ignored."""
    if not hasattr(cfg, BASELINE):  # remembered so the Settings page can say what config.yaml has
        setattr(cfg, BASELINE, copy.deepcopy(current(cfg)))
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


def require_recovery_password(cfg: Config, store: Store, password: str) -> None:
    """`password` is the admin password a change would leave; the server's pinned one always stays."""
    if store.get("google_login_enabled") == "1" and not (getattr(cfg, PINNED_PASSWORD, "") or password).strip():
        raise ValueError("Google login requires an admin recovery password.")


def save(cfg: Config, store: Store, changes: dict) -> None:
    """Validate and apply `changes`, then persist them merged with earlier overrides."""
    if getattr(cfg, PINNED_PASSWORD, "") and changes.get("web", {}).get("password"):
        raise ValueError("on the server the admin password comes from CHECKIN_ADMIN_PASSWORD; change it there and restart")
    require_recovery_password(cfg, store, str(changes.get("web", {}).get("password", cfg.web.password)))
    apply(cfg, changes)
    saved = json.loads(store.get(KEY) or "{}")
    for section, fields in changes.items():
        saved.setdefault(section, {}).update(fields)
    # A value equal to config.yaml is not stored: the Settings form posts every field, and storing
    # them all would make the database silently win over a later edit of config.yaml.
    file_values = getattr(cfg, BASELINE, None)
    if file_values is not None:
        for section, fields in changes.items():
            for name in fields:
                if getattr(_section(cfg, section), name) == file_values.get(section, {}).get(name, object()):
                    saved.get(section, {}).pop(name, None)
        saved = {s: f for s, f in saved.items() if f}
    store.put(KEY, json.dumps(saved))


def overrides(cfg: Config, store: Store) -> dict[str, str]:
    """Saved values that differ from config.yaml: {"section.name": what config.yaml has}, shown next
    to the field. Secrets are not shown."""
    file_values = getattr(cfg, BASELINE, None)
    if file_values is None:
        return {}
    found = {}
    for section, fields in json.loads(store.get(KEY) or "{}").items():
        for name in fields:
            if section not in EDITABLE or name not in EDITABLE[section]:
                continue
            original = file_values[section][name]
            if getattr(_section(cfg, section), name) != original:
                shown = ("set" if original else "empty") if name in SECRET else _display(original)
                found[f"{section}.{name}" if section else name] = shown
    return found


def _display(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "none"
    return str(value) if value != "" else "empty"


def reset(cfg: Config, store: Store, key: str) -> None:
    """Forget the saved value of one field ("section.name"), so config.yaml applies again."""
    section, _, name = key.rpartition(".")
    file_values = getattr(cfg, BASELINE, None)
    if section not in EDITABLE or name not in EDITABLE[section] or file_values is None:
        raise ValueError(f"unknown setting {key!r}")
    if key == "web.password":
        require_recovery_password(cfg, store, file_values[section][name])
    saved = json.loads(store.get(KEY) or "{}")
    saved.get(section, {}).pop(name, None)
    apply(cfg, {section: {name: file_values[section][name]}})
    store.put(KEY, json.dumps({s: f for s, f in saved.items() if f}))


def current(cfg: Config) -> dict:
    return {s: {n: getattr(_section(cfg, s), n) for n in names} for s, names in EDITABLE.items()}
