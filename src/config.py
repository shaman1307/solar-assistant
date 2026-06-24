"""Shared sa-config.yaml load/save helpers."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .simulation_config import merge_simulation_defaults

BASE_DIR = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "sa-config.yaml"
LOCAL_CONFIG_PATH = BASE_DIR / "sa-config.local.yaml"


def config_path() -> Path:
    """Active config file. Pi default: sa-config.yaml. Local dev: set SOLAR_CONFIG_PATH."""
    override = os.environ.get("SOLAR_CONFIG_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else BASE_DIR / p
    return DEFAULT_CONFIG_PATH


def load_config() -> dict:
    path = config_path()
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if cfg.pop("plan_overrides", None) is not None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)
    cfg = merge_simulation_defaults(cfg)
    ev = cfg.setdefault("ev", {})
    ev.setdefault("max_power_kw", 11.0)
    return cfg


def save_config(cfg: dict) -> None:
    with open(config_path(), "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)
