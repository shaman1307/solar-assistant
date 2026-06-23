"""Data API: metrics, forecast, RCE, buy tariff, simulation."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from .. import forecast as forecast_mod
from .. import influxdb as influxdb_mod
from .. import rce as rce_mod
from .. import sa_client
from ..config import load_config
from ..influxdb import now_warsaw
from ..plan_monthly_history import build_month_history
from ..plan_simulation import (
    build_buy_tariff_payload,
    build_plan_simulation,
    get_cached_buy_tariff,
    get_cached_rce,
)

router = APIRouter()

_SA_DAILY_ENERGY_KEYS = (
    "pv_energy_today",
    "load_energy_today",
    "grid_buy_energy",
    "grid_sell_energy",
    "battery_charged_today",
    "battery_discharged_today",
)


@router.get("/api/metrics")
async def api_metrics() -> dict[str, Any]:
    cfg = load_config()
    live, accruals = await asyncio.gather(
        sa_client.get_live_metrics(cfg),
        influxdb_mod.get_accruals(),
    )
    if live.get("sa_online"):
        for key in _SA_DAILY_ENERGY_KEYS:
            if key in live:
                accruals[key] = live[key]
    live.update(accruals)
    return live


@router.get("/api/forecast")
async def api_forecast() -> dict[str, Any]:
    """Forecast today/tomorrow from day cache; actuals for charts from Influx."""
    cfg = load_config()
    today_str = now_warsaw().strftime("%Y-%m-%d")
    day_accruals = await influxdb_mod.get_accruals_for_date(today_str)
    hourly = (day_accruals.get("hourly") or {}) if isinstance(day_accruals, dict) else {}
    forecast = await forecast_mod.get_forecast(
        cfg, today_pv_actual=hourly.get("pv"),
    )
    if isinstance(day_accruals, dict):
        forecast["series_10min"] = day_accruals.get("series_10min")
    forecast["load_actual_hourly"] = hourly.get("load")
    forecast["pv_actual_hourly"] = hourly.get("pv")
    return forecast


@router.get("/api/rce")
async def api_rce() -> dict[str, Any]:
    """Return hourly RCE prices for today and tomorrow from PSE."""
    cached = get_cached_rce()
    if cached is not None:
        return cached
    return await rce_mod.get_rce_prices()


@router.get("/api/buy-tariff")
async def api_buy_tariff() -> dict[str, Any]:
    """Return rolling hourly buy-tariff prices from config (G12 zones)."""
    cached = get_cached_buy_tariff()
    if cached is not None:
        return cached
    return build_buy_tariff_payload(load_config())


@router.get("/api/history-month")
async def api_history_month(month: str) -> dict[str, Any]:
    """Daily Influx history totals for a calendar month (YYYY-MM). Loaded on demand."""
    cfg = load_config()
    return await build_month_history(month, cfg)


@router.get("/api/simulation")
async def api_simulation(refresh: bool = False) -> dict[str, Any]:
    cfg = load_config()
    return await build_plan_simulation(
        cfg,
        force_refresh=refresh,
        invalidate_inputs=refresh,
    )


@router.get("/api/accruals")
async def api_accruals(date: str | None = None) -> dict[str, Any]:
    """Return daily totals + hourly breakdown for any Warsaw day (default: today)."""
    if not date:
        date = now_warsaw().strftime("%Y-%m-%d")
    return await influxdb_mod.get_accruals_for_date(date)
