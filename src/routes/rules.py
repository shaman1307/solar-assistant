"""SA rules, smart mode, plan apply, hourly sync."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import sa_client
from ..config import load_config, save_config
from ..plan_simulation import build_plan_simulation, get_cached_plan
from ..scheduler import get_last_hourly_sync, run_hourly_plan_sync

router = APIRouter()


@router.get("/api/rules")
async def api_rules(fresh: bool = False) -> dict[str, Any]:
    cfg = load_config()
    return await sa_client.get_rules(cfg, fresh=fresh)


@router.post("/api/rules/grid-charge")
async def api_set_grid_charge(body: dict) -> dict[str, Any]:
    cfg = load_config()
    enabled: bool = bool(body.get("enabled", False))
    power_kw: float = float(body.get("power_kw", 2.0))
    ok = await sa_client.set_grid_charging(cfg, enabled=enabled, power_kw=power_kw)
    return {"ok": ok}


@router.post("/api/rules/grid-export")
async def api_set_grid_export(body: dict) -> dict[str, Any]:
    cfg = load_config()
    enabled: bool = bool(body.get("enabled", False))
    ok = await sa_client.set_grid_export(cfg, enabled=enabled)
    return {"ok": ok}


@router.post("/api/rules/timer-schedule")
async def api_set_timer_schedule(body: dict) -> dict[str, Any]:
    cfg = load_config()
    ok = await sa_client.set_timer_schedule(cfg, body)
    return {"ok": ok}


@router.post("/api/rules/work-mode")
async def api_set_work_mode(body: dict) -> dict[str, Any]:
    cfg = load_config()
    mode = str(body.get("work_mode", "")).strip()
    if not mode:
        return {"ok": False, "error": "work_mode required"}
    ok = await sa_client.set_work_mode(cfg, mode)
    return {"ok": ok, "work_mode": mode if ok else None}


@router.post("/api/rules/timed-power")
async def api_set_timed_power(body: dict) -> dict[str, Any]:
    """SA Power tab: timed charge / timed discharge toggles."""
    cfg = load_config()
    charge = body.get("timed_charge_enabled")
    discharge = body.get("timed_discharge_enabled")
    if charge is None and discharge is None:
        return {"ok": False, "error": "timed_charge_enabled or timed_discharge_enabled required"}
    ok = await sa_client.set_timed_power_flags(
        cfg,
        timed_charge_enabled=bool(charge) if charge is not None else None,
        timed_discharge_enabled=bool(discharge) if discharge is not None else None,
    )
    return {
        "ok": ok,
        "timed_charge_enabled": bool(charge) if charge is not None else None,
        "timed_discharge_enabled": bool(discharge) if discharge is not None else None,
    }


@router.post("/api/smart-mode")
async def api_set_smart_mode(body: dict) -> dict[str, Any]:
    cfg = load_config()
    was_enabled = bool(cfg.get("smart_mode_enabled", False))
    enabled = bool(body.get("enabled", False))
    cfg["smart_mode_enabled"] = enabled
    save_config(cfg)
    result: dict[str, Any] = {"ok": True, "enabled": enabled}
    if enabled and not was_enabled:
        result["initial_sync"] = await run_hourly_plan_sync()
    return result


@router.get("/api/smart-mode")
async def api_get_smart_mode() -> dict[str, Any]:
    cfg = load_config()
    return {"enabled": bool(cfg.get("smart_mode_enabled", False))}


@router.post("/api/rules/apply-plan")
async def api_apply_plan() -> dict[str, Any]:
    cfg = load_config()
    cached = get_cached_plan()
    if cached:
        sim_result = cached
    else:
        sim_result = await build_plan_simulation(
            cfg,
            force_refresh=False,
            invalidate_inputs=False,
        )
    schedule = sim_result["next_hour_schedule"]
    ok = await sa_client.apply_hourly_schedule_to_sa(cfg, schedule)
    return {
        "ok": ok,
        "error": None if ok else (
            "SolarAssistant rejected the timer write. "
            "Check SA → Configuration → Timer schedule manually, or reboot SolarAssistant if writes fail."
        ),
        "next_hour": sim_result["next_hour"],
        "next_hour_schedule": schedule,
        "planned_action": schedule.get("planned_action"),
    }


@router.get("/api/hourly-sync/status")
async def api_hourly_sync_status() -> dict[str, Any]:
    """Last backend hourly job result (plan refresh + optional SA timer write)."""
    cfg = load_config()
    status = get_last_hourly_sync()
    return {
        **status,
        "smart_mode_enabled_now": bool(cfg.get("smart_mode_enabled", False)),
        "scheduler_active": True,
    }


@router.post("/api/rules/sync-hour")
async def api_sync_hour() -> dict[str, Any]:
    """Run hourly plan→SA sync immediately (same as the :00 cron job)."""
    return await run_hourly_plan_sync()
