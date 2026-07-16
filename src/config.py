"""Shared sa-config.yaml load/save helpers."""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .grid_config import merge_grid_defaults
from .simulation_config import (
    merge_simulation_defaults,
    merge_timer_schedule_defaults,
    normalize_battery_power_limits,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "sa-config.yaml"
LOCAL_CONFIG_PATH = BASE_DIR / "sa-config.local.yaml"
_LEGACY_STATE_PATH = BASE_DIR / "data" / "app_state.json"


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


def _recover_legacy_smart_mode(cfg: dict, path: Path) -> None:
    """One-time import of smart_mode_enabled left in data/app_state.json."""
    if path != DEFAULT_CONFIG_PATH or not _LEGACY_STATE_PATH.is_file():
        return
    if cfg.get("smart_mode_enabled") is not None:
        try:
            _LEGACY_STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        state = json.loads(_LEGACY_STATE_PATH.read_text(encoding="utf-8"))
        legacy = state.get("smart_mode_enabled")
        if legacy is None:
            return
        cfg["smart_mode_enabled"] = bool(legacy)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False)
        _LEGACY_STATE_PATH.unlink(missing_ok=True)
        log.info(
            "Recovered smart_mode_enabled=%s from app_state.json into sa-config.yaml",
            legacy,
        )
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("legacy app_state.json recovery failed: %s", exc)


def _read_runtime_yaml() -> dict[str, Any]:
    path = config_path()
    cfg = _load_yaml(path)
    _recover_legacy_smart_mode(cfg, path)
    if path == DEFAULT_CONFIG_PATH and LOCAL_CONFIG_PATH.is_file():
        cfg = _deep_merge(cfg, _load_yaml(LOCAL_CONFIG_PATH))
    return cfg


def load_config() -> dict:
    """Load active template merged with sa-config.yaml runtime state."""
    from .config_templates import (
        merge_runtime_onto_template,
        resolve_active_template_name,
        uses_template_mode,
    )
    from .sqlite_store import (
        get_installed_default_template,
        list_config_template_names,
        load_config_template,
    )

    runtime = _read_runtime_yaml()
    installed_default = get_installed_default_template()
    template_names = list_config_template_names()

    if uses_template_mode(runtime):
        template_name = resolve_active_template_name(
            runtime,
            installed_default=installed_default,
            template_names=template_names,
        )
        template_payload = load_config_template(template_name) or {}
        cfg = merge_runtime_onto_template(template_payload, runtime)
    else:
        cfg = deepcopy(runtime)

    cfg = merge_simulation_defaults(cfg)
    merge_timer_schedule_defaults(cfg)
    merge_grid_defaults(cfg)
    normalize_battery_power_limits(cfg)
    ev = cfg.setdefault("ev", {})
    ev.setdefault("max_power_kw", 11.0)
    return cfg


def save_config(cfg: dict) -> None:
    from .config_templates import (
        extract_runtime_payload,
        extract_template_payload,
        resolve_active_template_name,
    )
    from .sqlite_store import (
        get_installed_default_template,
        list_config_template_names,
        save_config_template,
    )

    runtime_existing = _read_runtime_yaml()
    installed_default = get_installed_default_template()
    template_names = list_config_template_names()
    explicit_active = cfg.get("active_template") or runtime_existing.get("active_template")
    active_name = str(
        explicit_active
        or resolve_active_template_name(
            runtime_existing,
            installed_default=installed_default,
            template_names=template_names,
        )
    )

    template_payload = extract_template_payload(cfg)
    save_config_template(active_name, template_payload)

    runtime = extract_runtime_payload(cfg)
    for key, val in extract_runtime_payload(runtime_existing).items():
        runtime.setdefault(key, val)
    if "plan_overrides" in runtime_existing and "plan_overrides" not in runtime:
        runtime["plan_overrides"] = runtime_existing["plan_overrides"]
    if explicit_active:
        runtime["active_template"] = str(explicit_active)
    else:
        runtime.pop("active_template", None)
    runtime.pop("default_template", None)

    with open(config_path(), "w", encoding="utf-8") as fh:
        yaml.dump(runtime, fh, allow_unicode=True, default_flow_style=False)


def load_runtime_config() -> dict[str, Any]:
    """Raw sa-config.yaml (+ local overlay) without template merge."""
    return _read_runtime_yaml()
