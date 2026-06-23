"""Daily history totals for a calendar month (Influx actuals only)."""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timedelta
from typing import Any

from . import influxdb as influxdb_mod
from . import rce as rce_mod
from .influxdb import now_warsaw
from .plan_hourly_actuals import build_completed_history_rows
from .simulation_config import get_simulation_params


def _empty_month_totals() -> dict[str, Any]:
    return {
        "start": "TOTAL",
        "production": 0.0,
        "consumption": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "energy_cost": 0.0,
        "service_cost": 0.0,
    }


def _summarize_day_rows(day_rows: list[dict[str, Any]], date_str: str) -> dict[str, Any]:
    """Sum hourly history rows; costs already use per-hour G12 zone + RCE."""
    production = sum(float(r["production"]) for r in day_rows)
    consumption = sum(float(r["consumption"]) for r in day_rows)
    grid_import = sum(float(r["grid_import"]) for r in day_rows)
    grid_export = sum(float(r["grid_export"]) for r in day_rows)
    energy_cost = sum(float(r.get("energy_cost") or 0.0) for r in day_rows)
    service_cost = sum(float(r.get("service_cost") or 0.0) for r in day_rows)
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return {
        "date": date_str,
        "start": dt.strftime("%d-%m-%Y"),
        "production": round(production, 2),
        "consumption": round(consumption, 2),
        "grid_import": round(grid_import, 2),
        "grid_export": round(grid_export, 2),
        "energy_cost": round(energy_cost, 2),
        "service_cost": round(service_cost, 2),
    }


def _summarize_period(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return _empty_month_totals()
    return {
        "start": "TOTAL",
        "production": round(sum(float(r["production"]) for r in rows), 2),
        "consumption": round(sum(float(r["consumption"]) for r in rows), 2),
        "grid_import": round(sum(float(r["grid_import"]) for r in rows), 2),
        "grid_export": round(sum(float(r["grid_export"]) for r in rows), 2),
        "energy_cost": round(sum(float(r.get("energy_cost") or 0.0) for r in rows), 2),
        "service_cost": round(sum(float(r.get("service_cost") or 0.0) for r in rows), 2),
    }


def _month_date_range(month: str) -> tuple[list[str], str | None]:
    """Return Warsaw calendar dates in month up to today, or error message."""
    try:
        year_s, mon_s = month.split("-", 1)
        year, mon = int(year_s), int(mon_s)
        if mon < 1 or mon > 12:
            return [], "invalid month"
    except (ValueError, AttributeError):
        return [], "invalid month format (use YYYY-MM)"

    last_dom = calendar.monthrange(year, mon)[1]
    first = datetime(year, mon, 1).date()
    last = datetime(year, mon, last_dom).date()
    today = now_warsaw().date()

    if first > today:
        return [], None

    end = min(last, today)
    dates: list[str] = []
    d = first
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates, None


async def build_month_history(month: str, cfg: dict) -> dict[str, Any]:
    """Daily totals from Influx history for each day in the selected month.

    Costs: for each hour h, import kWh × G12 peak/offpeak (by clock) + export × RCE(h),
    then summed to the day — not daily kWh × one tariff.
    """
    dates, err = _month_date_range(month)
    if err:
        return {"month": month, "error": err, "rows": [], "totals": _empty_month_totals()}
    if not dates:
        return {
            "month": month,
            "rows": [],
            "totals": _empty_month_totals(),
            "g12_tariff_name": cfg.get("grid", {}).get("g12", {}).get("tariff_name", "G12"),
        }

    params = get_simulation_params(cfg)
    accruals, quarters_by_date = await asyncio.gather(
        asyncio.gather(*[influxdb_mod.get_accruals_for_date(d) for d in dates]),
        rce_mod.get_quarter_rce_for_dates(*dates),
    )

    now = now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []

    for date_str, acc in zip(dates, accruals):
        if not isinstance(acc, dict) or acc.get("error"):
            continue
        hourly = acc.get("hourly")
        if not hourly:
            continue
        until_hour = now.hour if date_str == today_str else 24
        if until_hour <= 0:
            continue
        day_rows = build_completed_history_rows(
            date_str,
            until_hour,
            hourly,
            quarters_by_date,
            cfg,
            params,
        )
        if not day_rows:
            continue
        rows.append(_summarize_day_rows(day_rows, date_str))

    return {
        "month": month,
        "rows": rows,
        "totals": _summarize_period(rows),
        "g12_tariff_name": cfg.get("grid", {}).get("g12", {}).get("tariff_name", "G12"),
    }
