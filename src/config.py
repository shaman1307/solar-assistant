"""Shared sa-config.yaml load/save helpers."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .simulation_config import merge_simulation_defaults, normalize_battery_power_limits

BASE_DIR = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "sa-config.yaml"
LOCAL_CONFIG_PATH = BASE_DIR / "sa-config.local.yaml"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* onto a copy of *base* (overlay wins)."""
    merged = deepcopy(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def config_path() -> Path:
    """Primary config file (read/write). Optional SOLAR_CONFIG_PATH for tests."""
    override = os.environ.get("SOLAR_CONFIG_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else BASE_DIR / p
    return DEFAULT_CONFIG_PATH


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config() -> dict:
    """Load sa-config.yaml; merge sa-config.local.yaml overlay when present."""
    path = config_path()
    cfg = _load_yaml(path)
    if path == DEFAULT_CONFIG_PATH and LOCAL_CONFIG_PATH.is_file():
        cfg = _deep_merge(cfg, _load_yaml(LOCAL_CONFIG_PATH))
    if cfg.pop("plan_overrides", None) is not None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)
    cfg = merge_simulation_defaults(cfg)
    normalize_battery_power_limits(cfg)
    ev = cfg.setdefault("ev", {})
    ev.setdefault("max_power_kw", 11.0)
    return cfg


def save_config(cfg: dict) -> None:
    with open(config_path(), "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)
