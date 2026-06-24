"""
Hourly Plan Simulation — shared build pipeline and in-memory cache.

Refreshed every hour (scheduler :00 Warsaw), regardless of smart_mode_enabled,
with cached Load/PV forecast, Influx actuals for completed hours, and PSE RCE.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from . import forecast as forecast_mod
from . import influxdb as influxdb_mod
from . import rce as rce_mod
from . import sa_client
from .influxdb import now_warsaw
from .simulation import run_simulation
from .simulation_config import plan_min_soc_pct
from .timer_plan import build_hourly_schedule

log = logging.getLogger(__name__)

_cache: dict[str, Any] | None = None


def get_cached_plan() -> dict[str, Any] | None:
    return _cache


def invalidate_plan_cache() -> None:
    global _cache
    _cache = None


def extract_plan_soc_hourly(plan: dict[str, Any] | None) -> dict[str, list[float | None]]:
    """Hourly planned/actual SOC from Energy arbitrage cache (today + tomorrow)."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    out: dict[str, list[float | None]] = {
        "today": [None] * 24,
        "tomorrow": [None] * 24,
    }
    if not plan:
        return out

    def _date_key(row: dict) -> str | None:
        return row.get("plan_date") or row.get("date")

    for row in (plan.get("history_rows") or []) + (plan.get("rows") or []):
        date_key = _date_key(row)
        label = None
        if date_key == today_str:
            label = "today"
        elif date_key == tomorrow_str:
            label = "tomorrow"
        if label is None:
            continue
        h = row.get("hour")
        if h is None or not (0 <= int(h) < 24):
            continue
        soc = row.get("soc")
        if soc is not None:
            out[label][int(h)] = round(float(soc), 1)
    return out


def extract_plan_soc_q15(plan: dict[str, Any] | None) -> dict[str, list[float | None]]:
    direct = (plan or {}).get("plan_soc_q15")
    if isinstance(direct, dict) and any(k in direct for k in ("today", "tomorrow")):
        return {
            "today": list((direct.get("today") or [])[:96]) + [None] * max(0, 96 - len(direct.get("today") or [])),
            "tomorrow": list((direct.get("tomorrow") or [])[:96]) + [None] * max(0, 96 - len(direct.get("tomorrow") or [])),
        }
    hourly = extract_plan_soc_hourly(plan)
    q15_out: dict[str, list[float | None]] = {}
    for label, series in hourly.items():
        slots: list[float | None] = []
        for h in range(24):
            v = series[h]
            slots.extend([v] * 4)
        q15_out[label] = slots
    return q15_out


def _plan_cache_stale(cached: dict[str, Any]) -> bool:
    """True when the rolling window start (current hour) differs from cached plan."""
    now = now_warsaw()
    start = now.replace(minute=0, second=0, microsecond=0)
    return (
        cached.get("today_date") != start.strftime("%Y-%m-%d")
        or cached.get("plan_from_hour") != start.hour
    )


def get_cached_forecast() -> dict[str, Any] | None:
    if _cache is None:
        return None
    return _cache.get("forecast")


def get_cached_rce() -> dict[str, Any] | None:
    if _cache is None:
        return None
    return _cache.get("rce")


def get_cached_buy_tariff() -> dict[str, Any] | None:
    if _cache is None:
        return None
    return _cache.get("buy_tariff")


def _compute_buy_tariff_rows(cfg: dict) -> list[dict[str, Any]]:
    """Build hourly buy-tariff rows from config (G12 zones) over the simulation horizon."""
    from datetime import timedelta

    from .simulation import get_buy_price
    from .simulation_config import get_simulation_params

    steps = int(get_simulation_params(cfg)["horizon_hours"])
    start_dt = now_warsaw().replace(minute=0, second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    for step in range(steps):
        dt = start_dt + timedelta(hours=step)
        buy_price, g12_zone = get_buy_price(dt, cfg)
        rows.append({
            "hour": dt.hour,
            "start": dt.strftime("%d-%m-%Y %H:00"),
            "buy_price": round(buy_price, 4),
            "g12_zone": g12_zone,
        })
    return rows


def build_buy_tariff_payload(
    cfg: dict,
    sim_rows: list[dict] | None = None,
) -> dict[str, Any]:
    """Hourly buy-tariff rows aligned with the simulation horizon."""
    if sim_rows is None:
        rows = _compute_buy_tariff_rows(cfg)
    else:
        rows = [
            {
                "hour": r["hour"],
                "start": r["start"],
                "buy_price": r["buy_price"],
                "g12_zone": r["g12_zone"],
            }
            for r in sim_rows
        ]
    g12 = cfg["grid"]["g12"]
    return {
        "rows": rows,
        "tariff_name": g12.get("tariff_name", "Buy Tariff"),
        "peak_price_pln_kwh": g12.get("peak_price_pln_kwh"),
        "offpeak_price_pln_kwh": g12.get("offpeak_price_pln_kwh"),
    }


async def fetch_plan_inputs(
    cfg: dict, *, invalidate: bool = False,
) -> tuple[dict, dict, dict, dict]:
    """Return (forecast, metrics, rules, rce_prices)."""
    if invalidate:
        from .cache_registry import invalidate_all_caches
        invalidate_all_caches()

    today_str = now_warsaw().strftime("%Y-%m-%d")
    results = await asyncio.gather(
        sa_client.get_live_metrics(cfg),
        sa_client.get_rules(cfg, fresh=invalidate),
        rce_mod.get_rce_prices(),
        influxdb_mod.get_accruals_for_date(today_str),
        return_exceptions=True,
    )

    def _safe(val: Any, default: Any) -> Any:
        return default if isinstance(val, Exception) else val

    metrics = _safe(results[0], {"battery_soc": 50.0, "sa_online": False})
    rules = _safe(results[1], {})
    rce_prices = _safe(results[2], {"current_price_pln_kwh": None, "today": [], "tomorrow": []})
    today_day = _safe(results[3], {})
    today_pv_actual = None
    if isinstance(today_day, dict) and today_day.get("hourly"):
        metrics = dict(metrics)
        metrics["today_hourly"] = today_day["hourly"]
        today_pv_actual = today_day["hourly"].get("pv")
    if now_warsaw().hour == 0:
        yesterday_str = (now_warsaw() - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_day = await influxdb_mod.get_accruals_for_date(yesterday_str)
        if isinstance(prev_day, dict) and prev_day.get("hourly"):
            metrics = dict(metrics)
            metrics["prev_day_hourly"] = prev_day["hourly"]
    forecast = await forecast_mod.get_forecast(cfg, today_pv_actual=today_pv_actual)
    return forecast, metrics, rules, rce_prices


async def build_plan_simulation(
    cfg: dict,
    *,
    force_refresh: bool = False,
    invalidate_inputs: bool = False,
    store_cache: bool = True,
) -> dict[str, Any]:
    """Run Plan Simulation; optionally refresh forecast/RCE/Influx inputs first."""
    global _cache

    from .simulation_config import merge_simulation_defaults
    cfg = merge_simulation_defaults(cfg)

    if not force_refresh and _cache is not None and not _plan_cache_stale(_cache):
        return _cache

    forecast, metrics, rules, rce_prices = await fetch_plan_inputs(
        cfg, invalidate=invalidate_inputs or force_refresh,
    )

    forecast_bundle = dict(forecast)
    if metrics.get("today_hourly"):
        forecast_bundle["load_actual_hourly"] = metrics["today_hourly"].get("load")
        forecast_bundle["pv_actual_hourly"] = metrics["today_hourly"].get("pv")

    sim = run_simulation(
        forecast, metrics, rules, cfg,
        rce_prices=rce_prices,
    )

    next_hour = (now_warsaw().hour + 1) % 24
    next_hour_schedule = build_hourly_schedule(sim["rows"], next_hour, cfg, rules)
    computed_at = now_warsaw().strftime("%Y-%m-%d %H:%M:%S")

    result: dict[str, Any] = {
        **sim,
        "computed_at": computed_at,
        "simulation_min_soc_pct": plan_min_soc_pct(cfg),
        "rce_current": rce_prices.get("current_price_pln_kwh"),
        "next_hour": next_hour,
        "next_hour_schedule": next_hour_schedule,
        "forecast_meta": forecast.get("meta", {}),
        "forecast": forecast_bundle,
        "rce": rce_prices,
        "buy_tariff": build_buy_tariff_payload(cfg, sim["rows"]),
        "plan_soc_q15": extract_plan_soc_q15(sim),
    }

    if store_cache:
        _cache = result

    return result


async def hourly_plan_refresh(cfg: dict) -> dict[str, Any]:
    """Invalidate data caches, recompute plan, store result."""
    log.info("Hourly Plan Simulation refresh …")
    result = await build_plan_simulation(
        cfg,
        force_refresh=True,
        invalidate_inputs=True,
    )
    log.info(
        "Plan updated %s — Δ=%.2f kWh, export_hours=%s",
        result["computed_at"],
        result["delta_kwh"],
        result.get("plan_export_hours"),
    )
    return result
