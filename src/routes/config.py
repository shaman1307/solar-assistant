"""Configuration and cache management API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from .. import forecast as forecast_mod
from ..cache_registry import invalidate_all_caches
from ..config import load_config, save_config
from ..simulation_config import normalize_battery_power_limits
from ..plan_simulation import build_plan_simulation, invalidate_plan_cache

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/api/reload-caches")
async def api_reload_caches() -> dict[str, str]:
    """Drop in-memory caches so the next request picks up fresh data."""
    invalidate_all_caches()
    log.info("In-memory caches cleared.")
    return {"status": "ok"}


@router.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    cfg = load_config()
    cfg.pop("_charge_rate_kw", None)
    cfg.pop("smart_mode_enabled", None)
    return cfg


@router.post("/api/config")
async def api_save_config(body: dict) -> dict[str, str]:
    existing = load_config()
    if "_charge_rate_kw" in existing:
        body["_charge_rate_kw"] = existing["_charge_rate_kw"]
    if "debug_tab_enabled" not in body and "debug_tab_enabled" in existing:
        body["debug_tab_enabled"] = existing["debug_tab_enabled"]
    if "smart_mode_enabled" not in body and "smart_mode_enabled" in existing:
        body["smart_mode_enabled"] = existing["smart_mode_enabled"]
    normalize_battery_power_limits(body)
    save_config(body)
    forecast_mod.invalidate_cache()
    invalidate_plan_cache()
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
    invalidate_plan_cache()
    await build_plan_simulation(cfg, force_refresh=True, invalidate_inputs=False)
    return {"status": "saved"}
