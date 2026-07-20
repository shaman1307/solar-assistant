"""Daily history totals for a calendar month (Influx actuals only)."""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timedelta
from typing import Any

from . import influxdb as influxdb_mod
from . import rce as rce_mod
from .grid_config import compute_service_fee_pln
from .influxdb import now_warsaw
from .plan_baseline import attach_baseline_savings, build_baseline_history_rows
from .plan_hourly_actuals import build_completed_history_rows
from .simulation_config import get_simulation_params


from .grid_config import BILLING_MODEL_VERSION
from .plan_cost import (
    month_energy_cost_total,
    month_import_cost_total,
    month_savings_pln,
)


def _empty_month_totals() -> dict[str, Any]:
    return {
        "start": "TOTAL",
        "production": 0.0,
        "consumption": 0.0,
        "grid_import": 0.0,
        "grid_import_peak": 0.0,
        "grid_import_offpeak": 0.0,
        "grid_export": 0.0,
        "export_revenue": 0.0,
        "import_energy_cost": 0.0,
        "energy_cost": 0.0,
        "service_cost": 0.0,
        "service_fee": 0.0,
        "energy_cost_total": 0.0,
        "import_cost_total": 0.0,
        "baseline_energy_cost": 0.0,
        "baseline_service_cost": 0.0,
        "baseline_service_fee": 0.0,
        "baseline_export_revenue": 0.0,
        "baseline_import_energy_cost": 0.0,
        "baseline_cost": 0.0,
        "savings_pln": 0.0,
    }


def _import_by_zone(day_rows: list[dict[str, Any]]) -> tuple[float, float, float]:
    peak = sum(
        float(r.get("grid_import") or 0.0)
        for r in day_rows
        if r.get("g12_zone") == "peak"
    )
    offpeak = sum(
        float(r.get("grid_import") or 0.0)
        for r in day_rows
        if r.get("g12_zone") == "offpeak"
    )
    total = sum(float(r.get("grid_import") or 0.0) for r in day_rows)
    return peak, offpeak, total


def _summarize_day_rows(day_rows: list[dict[str, Any]], date_str: str) -> dict[str, Any]:
    """Sum hourly history rows; costs already use per-hour G12 zone + RCE."""
    production = sum(float(r["production"]) for r in day_rows)
    consumption = sum(float(r["consumption"]) for r in day_rows)
    grid_import = sum(float(r["grid_import"]) for r in day_rows)
    grid_export = sum(float(r["grid_export"]) for r in day_rows)
    energy_cost = sum(float(r.get("energy_cost") or 0.0) for r in day_rows)
    export_revenue = sum(float(r.get("export_revenue") or 0.0) for r in day_rows)
    import_energy_cost = sum(
        float(r.get("energy_cost") or 0.0) + float(r.get("export_revenue") or 0.0)
        for r in day_rows
    )
    service_cost = sum(float(r.get("service_cost") or 0.0) for r in day_rows)
    energy_cost_total = month_energy_cost_total(export_revenue, import_energy_cost)
    import_cost_total = month_import_cost_total(service_cost)
    peak_import, offpeak_import, _ = _import_by_zone(day_rows)
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return {
        "date": date_str,
        "start": dt.strftime("%d-%m-%Y"),
        "production": round(production, 2),
        "consumption": round(consumption, 2),
        "grid_import": round(grid_import, 2),
        "grid_import_peak": round(peak_import, 4),
        "grid_import_offpeak": round(offpeak_import, 4),
        "grid_export": round(grid_export, 2),
        "export_revenue": round(export_revenue, 4),
        "import_energy_cost": round(import_energy_cost, 4),
        "energy_cost": round(energy_cost, 4),
        "service_cost": round(service_cost, 4),
        "energy_cost_total": energy_cost_total,
        "import_cost_total": import_cost_total,
    }


def _attach_month_service_fees(
    totals: dict[str, Any],
    rows: list[dict[str, Any]],
    cfg: dict,
) -> dict[str, Any]:
    """Add month-level service fees and savings including fixed distribution charges."""
    total_import = sum(float(r.get("grid_import") or 0.0) for r in rows)
    baseline_import = sum(float(r.get("baseline_grid_import") or 0.0) for r in rows)

    service_fee = compute_service_fee_pln(total_import, cfg)
    baseline_service_fee = compute_service_fee_pln(baseline_import, cfg)

    totals["service_fee"] = service_fee
    totals["baseline_service_fee"] = baseline_service_fee

    export_revenue = float(totals.get("export_revenue") or 0.0)
    import_energy = float(totals.get("import_energy_cost") or 0.0)
    service_cost = float(totals.get("service_cost") or 0.0)
    # Keep Baseline net aligned with summed sim export/tariff.
    base_exp = float(totals.get("baseline_export_revenue") or 0.0)
    base_tariff = float(totals.get("baseline_import_energy_cost") or 0.0)
    totals["baseline_cost"] = round(base_exp - base_tariff, 4)
    energy_cost_total = month_energy_cost_total(export_revenue, import_energy)
    import_cost_total = month_import_cost_total(service_cost, service_fee)
    totals["energy_cost_total"] = energy_cost_total
    totals["import_cost_total"] = import_cost_total
    totals["savings_pln"] = month_savings_pln(
        export_revenue,
        import_energy,
        base_exp,
        base_tariff,
    )
    return totals


def _summarize_period(rows: list[dict[str, Any]], cfg: dict) -> dict[str, Any]:
    if not rows:
        return _empty_month_totals()
    totals = {
        "start": "TOTAL",
        "production": round(sum(float(r["production"]) for r in rows), 2),
        "consumption": round(sum(float(r["consumption"]) for r in rows), 2),
        "grid_import": round(sum(float(r["grid_import"]) for r in rows), 2),
        "grid_import_peak": round(
            sum(float(r.get("grid_import_peak") or 0.0) for r in rows), 4,
        ),
        "grid_import_offpeak": round(
            sum(float(r.get("grid_import_offpeak") or 0.0) for r in rows), 4,
        ),
        "grid_export": round(sum(float(r["grid_export"]) for r in rows), 2),
        "export_revenue": round(
            sum(float(r.get("export_revenue") or 0.0) for r in rows), 4,
        ),
        "import_energy_cost": round(
            sum(float(r.get("import_energy_cost") or 0.0) for r in rows), 4,
        ),
        "energy_cost": round(sum(float(r.get("energy_cost") or 0.0) for r in rows), 4),
        "service_cost": round(sum(float(r.get("service_cost") or 0.0) for r in rows), 4),
        "baseline_energy_cost": round(
            sum(float(r.get("baseline_energy_cost") or 0.0) for r in rows), 4,
        ),
        "baseline_service_cost": round(
            sum(float(r.get("baseline_service_cost") or 0.0) for r in rows), 4,
        ),
        "baseline_export_revenue": round(
            sum(float(r.get("baseline_export_revenue") or 0.0) for r in rows), 4,
        ),
        "baseline_import_energy_cost": round(
            sum(float(r.get("baseline_import_energy_cost") or 0.0) for r in rows), 4,
        ),
        "baseline_cost": round(sum(float(r.get("baseline_cost") or 0.0) for r in rows), 4),
        "savings_pln": round(sum(float(r.get("savings_pln") or 0.0) for r in rows), 4),
    }
    return _attach_month_service_fees(totals, rows, cfg)


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
        baseline_rows = build_baseline_history_rows(
            date_str,
            until_hour,
            hourly,
            quarters_by_date,
            cfg,
            params,
        )
        summary = _summarize_day_rows(day_rows, date_str)
        baseline_summary = attach_baseline_savings(summary, baseline_rows)
        peak_import, offpeak_import, baseline_total = _import_by_zone(baseline_rows)
        baseline_summary["baseline_grid_import_peak"] = round(peak_import, 4)
        baseline_summary["baseline_grid_import_offpeak"] = round(offpeak_import, 4)
        baseline_summary["baseline_grid_import"] = round(baseline_total, 4)
        rows.append(baseline_summary)

    totals = _summarize_period(rows, cfg)

    return {
        "month": month,
        "billing_model_version": BILLING_MODEL_VERSION,
        "rows": rows,
        "totals": totals,
        "g12_tariff_name": cfg.get("grid", {}).get("g12", {}).get("tariff_name", "G12"),
    }
