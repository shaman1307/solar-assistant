"""
Balance automation scheduler.

  - Nightly at 23:59 Europe/Warsaw: build Load+PV day cache for tomorrow and day-after,
    then refresh charge-rate estimate in config (Δ).
  - Hourly at :00: refresh Plan Simulation (Energy arbitrage + RCE + buy tariff).
  - Same :00 tick: SA Timer Schedule when smart_mode_enabled.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import forecast as forecast_mod
from . import rce as rce_mod
from . import sa_client
from .config import load_config, save_config
from .plan_simulation import fetch_plan_inputs, hourly_plan_refresh
from .simulation import compute_balance_delta

log = logging.getLogger(__name__)

CHARGE_SPREAD_HOURS = 8

# Last hourly job outcome — readable without frontend (GET /api/hourly-sync/status).
_last_hourly_sync: dict[str, Any] = {
    "ran_at": None,
    "plan_cache_refreshed": False,
    "plan_computed_at": None,
    "smart_mode_enabled": False,
    "sa_sync_attempted": False,
    "sa_sync_ok": None,
    "next_hour": None,
    "planned_action": None,
    "error": None,
}


def get_last_hourly_sync() -> dict[str, Any]:
    return dict(_last_hourly_sync)


def _smart_mode_enabled(cfg: dict) -> bool:
    return bool(cfg.get("smart_mode_enabled", False))


async def _refresh_hourly_plan_cache(cfg: dict) -> dict[str, Any]:
    """Recompute Energy arbitrage plan and store backend cache (always runs)."""
    log.info("Hourly plan refresh — recompute simulation, RCE, forecast, buy tariff …")
    return await hourly_plan_refresh(cfg)


async def _sync_sa_timer_if_smart(
    cfg: dict,
    sim_result: dict[str, Any],
    *,
    next_hour: int,
) -> bool | None:
    """Write next-hour Timer Schedule slot 1 to SA. Returns None when smart mode is off."""
    if not _smart_mode_enabled(cfg):
        return None

    schedule = sim_result["next_hour_schedule"]
    log.info("Hourly SA sync — arm slot 1 for %02d:00 …", next_hour)
    ok = await sa_client.apply_hourly_schedule_to_sa(cfg, schedule)
    if ok:
        log.info(
            "Hourly SA sync OK — %02d:00 action=%s charge=%s discharge=%s power=%.2f kW",
            next_hour,
            schedule.get("planned_action"),
            schedule.get("timed_charge_enabled"),
            schedule.get("timed_discharge_enabled"),
            schedule.get("discharge_slots", [{}])[0].get("power_kw", 0)
            or schedule.get("charge_slots", [{}])[0].get("power_kw", 0),
        )
    else:
        log.error("Hourly plan sync failed for hour %02d", next_hour)
    return ok


async def run_nightly_forecast_cache() -> dict[str, Any]:
    """23:59 — weekday Load + Open-Meteo PV for D+1/D+2; then balance Δ."""
    cfg = load_config()
    try:
        result = await forecast_mod.run_nightly_forecast_cache(cfg)
        log.info("Nightly forecast cache OK at %s", result.get("computed_at"))
        await _balance_job()
        return {"ok": True, "computed_at": result.get("computed_at")}
    except Exception as exc:
        log.exception("Nightly job failed (forecast cache or balance Δ)")
        return {"ok": False, "error": str(exc)}


async def run_hourly_plan_sync() -> dict[str, Any]:
    """Hourly :00 job — refresh plan cache; SA timer write only if smart mode on."""
    global _last_hourly_sync

    from .influxdb import now_warsaw

    ran_at = now_warsaw().strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "plan_cache_refreshed": False,
        "plan_computed_at": None,
        "smart_mode_enabled": False,
        "sa_sync_attempted": False,
        "sa_sync_ok": None,
        "next_hour": None,
        "planned_action": None,
        "next_hour_schedule": None,
        "error": None,
    }

    try:
        cfg = load_config()
        now = now_warsaw()
        next_hour = (now.hour + 1) % 24
        smart_on = _smart_mode_enabled(cfg)
        status["smart_mode_enabled"] = smart_on
        status["next_hour"] = next_hour

        sim_result = await _refresh_hourly_plan_cache(cfg)
        status["plan_cache_refreshed"] = True
        status["plan_computed_at"] = sim_result.get("computed_at")
        schedule = sim_result["next_hour_schedule"]
        status["planned_action"] = schedule.get("planned_action")
        status["next_hour_schedule"] = schedule

        if smart_on:
            status["sa_sync_attempted"] = True
            sa_ok = await _sync_sa_timer_if_smart(cfg, sim_result, next_hour=next_hour)
            status["sa_sync_ok"] = sa_ok
            if sa_ok is False:
                status["error"] = f"SA timer write failed for hour {next_hour:02d}"
        else:
            log.info(
                "Hourly plan cache updated %s — SA timer sync skipped (smart mode off)",
                sim_result.get("computed_at"),
            )

        _last_hourly_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        _last_hourly_sync = status
        log.exception("Hourly plan sync job failed")
        return status


async def _balance_job() -> None:
    """After nightly forecast cache: update grid-charge rate from energy balance Δ."""
    log.info("Balance job starting …")
    cfg = load_config()

    forecast_mod.invalidate_cache()
    rce_mod.invalidate_cache()

    forecast, metrics, _, _ = await fetch_plan_inputs(cfg)
    delta = compute_balance_delta(forecast, metrics, cfg)
    log.info(
        "Balance Δ = %.2f kWh  (PV_tomorrow=%.2f  SOC_now=%.2f  Load_tomorrow=%.2f)",
        delta,
        forecast["tomorrow"]["pv_total"],
        float(metrics.get("battery_soc", 50)) / 100 * cfg["battery"]["capacity_kwh"],
        forecast["tomorrow"]["load_total"],
    )

    if delta < 0:
        cfg["_charge_rate_kw"] = round(abs(delta) / max(CHARGE_SPREAD_HOURS, 1), 2)
        save_config(cfg)
        log.info("Charge rate set to %.2f kW", cfg["_charge_rate_kw"])
    else:
        cfg.pop("_charge_rate_kw", None)
        save_config(cfg)
        log.info("Energy balance positive — charge rate cleared")

    log.info("Balance job complete (hourly sync handles SA timer slots).")


def create_scheduler(cfg: dict) -> AsyncIOScheduler:
    del cfg
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_nightly_forecast_cache,
        trigger=CronTrigger(hour=23, minute=59, timezone="Europe/Warsaw"),
        id="nightly_forecast_cache",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_hourly_plan_sync,
        trigger=CronTrigger(minute=0, timezone="Europe/Warsaw"),
        id="hourly_plan_sync",
        replace_existing=True,
        misfire_grace_time=120,
    )
    log.info(
        "Scheduler: forecast cache + balance Δ at 23:59; plan refresh at :00 (always); "
        "SA timer at :00 when smart mode on — Europe/Warsaw.",
    )
    return scheduler
