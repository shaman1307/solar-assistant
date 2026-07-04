"""Passive load-priority baseline costs for monthly history comparison."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .g12_pricing import get_buy_price
from .inverter_sim import _initial_soc_kwh, simulate_hour_load_priority
from .plan_cost import hour_meter_cash_pln


def _hourly_slot(
    hourly: dict[str, list[float | None]] | None,
    hour: int,
    key: str,
) -> float | None:
    if not hourly:
        return None
    arr = hourly.get(key) or [None] * 24
    if 0 <= hour < len(arr):
        return arr[hour]
    return None


def _hour_rce_price(
    quarters: list[float | None],
    hour: int,
) -> float | None:
    c0 = hour * 4
    hour_rce_q15 = list(quarters[c0:c0 + 4])
    hour_rce_vals = [float(v) for v in hour_rce_q15 if v is not None]
    if not hour_rce_vals:
        return None
    return round(sum(hour_rce_vals) / len(hour_rce_vals), 4)


def build_baseline_history_rows(
    plan_date: str,
    until_hour: int,
    hourly: dict[str, list[float | None]],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
    params: dict[str, float | int],
) -> list[dict[str, Any]]:
    """Replay IHDB PV/load through passive load-priority physics; price each hour."""
    if until_hour <= 0 or not hourly:
        return []

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = (float(params["min_soc_pct"]) / 100.0) * battery_cap
    ac_cap_kwh = float(cfg["inverter"]["ac_capacity_kw"])
    epsilon = float(params["epsilon_kwh"])

    base = datetime.strptime(plan_date, "%Y-%m-%d")
    quarters = quarters_by_date.get(plan_date) or []
    soc_kwh, _ = _initial_soc_kwh(hourly, battery_cap)
    rows: list[dict[str, Any]] = []

    for h in range(0, until_hour):
        pv_h = _hourly_slot(hourly, h, "pv")
        load_h = _hourly_slot(hourly, h, "load")
        if pv_h is None and load_h is None:
            continue

        pv = float(pv_h) if pv_h is not None else 0.0
        load = float(load_h) if load_h is not None else 0.0
        result = simulate_hour_load_priority(
            soc_kwh,
            pv,
            load,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kwh=ac_cap_kwh,
            epsilon=epsilon,
        )
        soc_kwh = result.soc_kwh

        hour_dt = base.replace(hour=h)
        buy_price, g12_zone = get_buy_price(hour_dt, cfg)
        rce_price = _hour_rce_price(quarters, h)
        cash = hour_meter_cash_pln(
            result.grid_import,
            result.grid_export,
            buy_price,
            rce_price,
            cfg,
            g12_zone=g12_zone,
        )
        rows.append(
            {
                "hour": h,
                "g12_zone": g12_zone,
                "grid_import": result.grid_import,
                "grid_export": result.grid_export,
                "energy_cost": cash["energy_cost"],
                "service_cost": cash["service_cost"],
                "cost": cash["cost"],
            }
        )
    return rows


def summarize_baseline_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    energy_cost = sum(float(r.get("energy_cost") or 0.0) for r in rows)
    service_cost = sum(float(r.get("service_cost") or 0.0) for r in rows)
    peak_import = sum(
        float(r.get("grid_import") or 0.0)
        for r in rows
        if r.get("g12_zone") == "peak"
    )
    offpeak_import = sum(
        float(r.get("grid_import") or 0.0)
        for r in rows
        if r.get("g12_zone") == "offpeak"
    )
    return {
        "baseline_energy_cost": round(energy_cost, 4),
        "baseline_service_cost": round(service_cost, 4),
        "baseline_cost": round(energy_cost + service_cost, 4),
        "baseline_grid_import_peak": round(peak_import, 4),
        "baseline_grid_import_offpeak": round(offpeak_import, 4),
        "baseline_grid_import": round(peak_import + offpeak_import, 4),
    }


def attach_baseline_savings(
    summary: dict[str, Any],
    baseline_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add baseline totals and savings vs actual day summary."""
    from .plan_cost import month_energy_cost_total, month_import_cost_total, month_savings_pln

    baseline = summarize_baseline_rows(baseline_rows)
    summary.update(baseline)
    export_revenue = float(summary.get("export_revenue") or 0.0)
    import_energy = float(summary.get("import_energy_cost") or 0.0)
    service_cost = float(summary.get("service_cost") or 0.0)
    energy_cost_total = month_energy_cost_total(export_revenue, import_energy)
    import_cost_total = month_import_cost_total(service_cost)
    summary["energy_cost_total"] = energy_cost_total
    summary["import_cost_total"] = import_cost_total
    summary["savings_pln"] = month_savings_pln(
        baseline["baseline_cost"],
        0.0,
        energy_cost_total,
        import_cost_total,
    )
    return summary
