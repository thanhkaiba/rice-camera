"""Export and import the configuration, to set up another computer the same way.

The file holds config.yaml and every value saved on the Settings page (hours, camera and detection
area, groups, notifications, HR system, recognition, backup), including the password, the Google Chat
webhook and the HR token. People, photos and attendance are not in it: those move with a backup
(`restore`) or the seed import. Importing keeps this computer's data folder and page address.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import yaml

from .. import __version__
from . import config as config_module, settings
from .config import Config

FORMAT = "face-attendance-configuration"
HOURS_KEY = "setup_hours_done"  # web/setup.py: the working hours step counts as done
BACKUP_KEY = "config_before_import"  # kv: the configuration replaced by the last import
KEPT = {"web": ["public_url"]}  # saved settings that belong to this computer, kept on import


def export(cfg: Config, store, config_file: Path) -> dict:
    return {"format": FORMAT, "version": 1, "program_version": __version__,
            "exported_at": dt.datetime.now(cfg.tz).isoformat(timespec="seconds"),
            "config_yaml": config_file.read_text(encoding="utf-8") if config_file.exists() else "",
            "settings": json.loads(store.get(settings.KEY) or "{}")}


def _yaml(text: str) -> dict:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"config.yaml in the file cannot be read: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("config.yaml in the file is not a list of settings")
    return data


def prepare(cfg: Config, config_file: Path, data) -> tuple[str, dict]:
    """Check an exported file against this program; returns (config.yaml text, saved settings) as
    they will be written. Raises ValueError."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("this is not a configuration file exported from this program")
    if data.get("version") != 1:
        raise ValueError(f"configuration file version {data.get('version')!r} is not supported; update this program")
    text, saved = data.get("config_yaml") or "", data.get("settings") or {}
    if not isinstance(text, str) or not isinstance(saved, dict) or not all(isinstance(v, dict) for v in saved.values()):
        raise ValueError("the configuration file is damaged")
    current = _yaml(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    if text:
        wanted = _yaml(text)
        if wanted.get("data_dir") != current.get("data_dir"):  # keep using this computer's data
            wanted.pop("data_dir", None)
            if "data_dir" in current:
                wanted = {"data_dir": current["data_dir"], **wanted}
            text = yaml.safe_dump(wanted, sort_keys=False, allow_unicode=True)
    else:
        text, wanted = (config_file.read_text(encoding="utf-8") if config_file.exists() else ""), current
    try:
        trial = config_module._build(Config, wanted)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config.yaml in the file is not valid: {exc}") from None
    trial.base_dir = cfg.base_dir
    saved = copy.deepcopy(saved)
    mine = settings.current(cfg)
    for section, names in KEPT.items():
        for name in names:
            saved.setdefault(section, {})[name] = mine[section][name]
    try:
        trial.validate()
        settings.apply(trial, saved)
    except ValueError as exc:
        raise ValueError(f"the settings in the file are not valid here: {exc}") from None
    return text, saved


def import_(cfg: Config, store, config_file: Path, data) -> Path:
    """Replace config.yaml and the saved settings with an exported file, after saving the current ones
    to <data folder>/config-backups/ (and the database). Takes full effect after a restart; returns
    the backup file."""
    text, saved = prepare(cfg, config_file, data)
    before = export(cfg, store, config_file)
    stamp = dt.datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    backup = cfg.path(cfg.data_dir) / "config-backups" / f"before-import-{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    store.put(BACKUP_KEY, json.dumps(before))
    if text:
        config_file.write_text(text, encoding="utf-8")
    store.put(settings.KEY, json.dumps(saved))
    if "schedule" in saved:
        store.put(HOURS_KEY, "1")
    settings.load(cfg, store)  # what can change while running does now; the rest after the restart
    return backup
