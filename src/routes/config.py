"""Configuration and cache management API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import forecast as forecast_mod
from ..cache_registry import invalidate_input_caches
from ..config import load_config, load_runtime_config, save_config
from ..config_templates import (
    extract_template_payload,
    merge_runtime_onto_template,
    resolve_active_template_name,
    template_meta_for_api,
    validate_template_name,
)
from ..simulation_config import normalize_battery_power_limits
from ..sqlite_store import (
    delete_config_template,
    delete_plan,
    get_installed_default_template,
    list_config_template_names,
    load_config_template,
    save_config_template,
    invalidate_month_history,
)
from ..plan_simulation import build_plan_simulation

router = APIRouter()
log = logging.getLogger(__name__)


def _template_meta_payload() -> dict[str, Any]:
    runtime = load_runtime_config()
    installed = get_installed_default_template()
    names = list_config_template_names()
    explicit_active = runtime.get("active_template")
    active = resolve_active_template_name(
        runtime,
        installed_default=installed,
        template_names=names,
    )
    return template_meta_for_api(
        installed_default=installed,
        active=active,
        names=names,
        explicit_active=str(explicit_active) if explicit_active else None,
    )


async def _refresh_after_config_change() -> None:
    forecast_mod.invalidate_cache()
    delete_plan()
    invalidate_month_history()
    await build_plan_simulation(load_config(), force_refresh=True, invalidate_inputs=False)


@router.post("/api/reload-caches")
async def api_reload_caches() -> dict[str, str]:
    """Drop live-input caches; keep SQLite plan_latest (history timers stay frozen)."""
    invalidate_input_caches()
    log.info("Live-input caches cleared; SQLite plan_latest preserved.")
    return {"status": "ok"}


@router.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    cfg = load_config()
    cfg.pop("_charge_rate_kw", None)
    cfg.pop("smart_mode_enabled", None)
    cfg["_template_meta"] = _template_meta_payload()
    return cfg


@router.get("/api/config/templates")
async def api_list_templates() -> dict[str, Any]:
    return _template_meta_payload()


@router.post("/api/config/templates")
async def api_create_template(body: dict[str, Any]) -> dict[str, Any]:
    name = validate_template_name(str(body.get("name") or ""))
    if name in list_config_template_names():
        raise HTTPException(status_code=409, detail="Template already exists")
    copy_from = (body.get("copy_from") or "").strip()
    if copy_from:
        payload = load_config_template(copy_from)
        if payload is None:
            raise HTTPException(status_code=404, detail="Source template not found")
    else:
        payload = extract_template_payload(load_config())
    save_config_template(name, payload)
    return {"ok": True, "name": name}


@router.delete("/api/config/templates/{name}")
async def api_delete_template(name: str) -> dict[str, Any]:
    name = validate_template_name(name)
    runtime = load_runtime_config()
    installed = get_installed_default_template()
    names = list_config_template_names()
    active = resolve_active_template_name(
        runtime,
        installed_default=installed,
        template_names=names,
    )
    if name == active:
        raise HTTPException(status_code=400, detail="Cannot delete the active template")
    if name == installed and len(names) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the only template")
    if not delete_config_template(name):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True}


@router.post("/api/config/templates/active")
async def api_set_active_template(body: dict[str, Any]) -> dict[str, Any]:
    name = validate_template_name(str(body.get("name") or ""))
    if load_config_template(name) is None:
        raise HTTPException(status_code=404, detail="Template not found")
    runtime = load_runtime_config()
    runtime["active_template"] = name
    template_payload = load_config_template(name) or {}
    merged = merge_runtime_onto_template(template_payload, runtime)
    normalize_battery_power_limits(merged)
    save_config(merged)
    await _refresh_after_config_change()
    return {"ok": True, "active_template": name}


@router.post("/api/config")
async def api_save_config(body: dict) -> dict[str, str]:
    existing = load_config()
    if "_charge_rate_kw" in existing:
        body["_charge_rate_kw"] = existing["_charge_rate_kw"]
    if "smart_mode_enabled" not in body and "smart_mode_enabled" in existing:
        body["smart_mode_enabled"] = existing["smart_mode_enabled"]
    runtime = load_runtime_config()
    if "active_template" not in body and runtime.get("active_template"):
        body["active_template"] = runtime["active_template"]
    if "plan_overrides" not in body and existing.get("plan_overrides"):
        body["plan_overrides"] = existing["plan_overrides"]
    normalize_battery_power_limits(body)
    save_config(body)
    await _refresh_after_config_change()
    return {"status": "saved"}


@router.get("/api/debug-tab")
async def api_get_debug_tab() -> dict[str, bool]:
    cfg = load_config()
    return {"enabled": cfg.get("debug_tab_enabled", True) is not False}


@router.post("/api/debug-tab")
async def api_set_debug_tab(body: dict) -> dict[str, bool]:
    cfg = load_config()
    enabled = bool(body.get("enabled", True))
    cfg["debug_tab_enabled"] = enabled
    save_config(cfg)
    return {"ok": True, "enabled": enabled}


@router.post("/api/overrides")
async def api_save_overrides(body: dict) -> dict[str, str]:
    cfg = load_config()
    overrides = cfg.setdefault("overrides", {})
    for key in ("today_pv_kwh", "today_load_kwh", "tomorrow_pv_kwh", "tomorrow_load_kwh"):
        val = body.get(key)
        overrides[key] = float(val) if val not in (None, "") else None
    save_config(cfg)
    await forecast_mod.apply_overrides_to_cache(cfg)
    delete_plan()
    await build_plan_simulation(cfg, force_refresh=True, invalidate_inputs=False)
    return {"status": "saved"}
