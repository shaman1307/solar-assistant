"""Actual Influx data for the last completed hour in plan simulation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .g12_pricing import get_buy_price
from .plan_cost import (
    derive_grid_flows_from_balance,
    hour_meter_cash_pln,
)
from .timer_plan import classify_action


def hour_in_progress(now: datetime, hour_start: datetime) -> bool:
    return now > hour_start


def interval_end_label(hour_dt: datetime) -> str:
    """Table Start column = end of hourly bucket (15:00 row = energy over 14:00–15:00)."""
    return (hour_dt + timedelta(hours=1)).strftime("%d-%m-%Y %H:00")


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


def _completed_hourly(
    plan_hour_start: datetime,
    today_hourly: dict[str, list[float | None]] | None,
    prev_day_hourly: dict[str, list[float | None]] | None,
) -> tuple[datetime, dict[str, list[float | None]] | None]:
    """Calendar hour and Influx bucket for the last full hour before plan_hour_start."""
    completed_dt = plan_hour_start - timedelta(hours=1)
    if completed_dt.date() == plan_hour_start.date():
        return completed_dt, today_hourly
    return completed_dt, prev_day_hourly


def _row_from_hourly_actual(
    hour_dt: datetime,
    hourly: dict[str, list[float | None]],
    *,
    cfg: dict,
    params: dict[str, float | int],
    rce_price: float | None,
    plan_date: str,
) -> dict[str, Any] | None:
    """One completed hour from Influx hourly accruals."""
    h = hour_dt.hour
    pv_h = _hourly_slot(hourly, h, "pv")
    load_h = _hourly_slot(hourly, h, "load")
    if pv_h is None and load_h is None:
        return None

    bat_in = _hourly_slot(hourly, h, "bat_charge")
    bat_out = _hourly_slot(hourly, h, "bat_discharge")
    grid_buy_h = _hourly_slot(hourly, h, "grid_buy")
    grid_sell_h = _hourly_slot(hourly, h, "grid_sell")
    soc_h = _hourly_slot(hourly, h, "soc")

    pv = float(pv_h) if pv_h is not None else 0.0
    load = float(load_h) if load_h is not None else 0.0
    min_soc_pct = float(params["min_soc_pct"])

    if soc_h is not None:
        soc_pct = max(min_soc_pct, min(100.0, float(soc_h)))
    else:
        soc_pct = min_soc_pct

    if bat_in is not None or bat_out is not None:
        battery_delta = float(bat_in or 0.0) - float(bat_out or 0.0)
    else:
        battery_delta = 0.0

    epsilon = float(params["epsilon_kwh"])
    grid_import = abs(float(grid_buy_h)) if grid_buy_h is not None and grid_buy_h < 0 else 0.0
    grid_export = float(grid_sell_h) if grid_sell_h is not None and grid_sell_h > 0 else 0.0
    if grid_import <= 0.0 and grid_export <= 0.0 and (bat_in is not None or bat_out is not None):
        grid_import, grid_export = derive_grid_flows_from_balance(
            pv, load, battery_delta, epsilon=epsilon,
        )

    buy_price, g12_zone = get_buy_price(hour_dt, cfg)
    bat_in_kwh = float(bat_in or 0.0)
    bat_out_kwh = float(bat_out or 0.0)
    action = classify_action(
        bat_charge=bat_in_kwh,
        bat_discharge=bat_out_kwh,
        grid_import=grid_import,
        grid_export=grid_export,
        production=pv,
    )
    cash = hour_meter_cash_pln(
        grid_import, grid_export, buy_price, rce_price, cfg, g12_zone=g12_zone,
    )

    return {
        "hour": h,
        "plan_date": plan_date,
        "start": interval_end_label(hour_dt),
        "production": round(pv, 3),
        "consumption": round(load, 3),
        "battery": round(battery_delta, 3),
        "bat_charge": round(bat_in_kwh, 3),
        "bat_discharge": round(bat_out_kwh, 3),
        "grid_import": round(grid_import, 3),
        "grid_export": round(grid_export, 3),
        "soc": round(soc_pct, 1),
        "import_cost": cash["import_cost"],
        "export_revenue": cash["export_revenue"],
        "energy_cost": cash["energy_cost"],
        "service_cost": cash["service_cost"],
        "cost": cash["cost"],
        "action": action,
        "timer_schedule": "",
        "rce_price": round(rce_price, 4) if rce_price is not None else None,
        "export_credit": cash["export_credit"],
        "g12_zone": g12_zone,
        "buy_price": round(buy_price, 4),
        "export_planned": False,
        "history_hour": True,
    }


def _hourly_slot_empty(hourly: dict[str, list[float | None]], h: int) -> bool:
    """True when all energy slots for this hour are zero or missing."""
    for key in ("pv", "load", "bat_charge", "bat_discharge", "grid_buy", "grid_sell"):
        arr = hourly.get(key) or []
        if h >= len(arr) or arr[h] is None:
            continue
        if abs(float(arr[h])) > 0:
            return False
    return True


def first_history_hour(hourly: dict[str, list[float | None]] | None) -> int:
    """First hour row to show: 00:00 only when hour-0 is all zeros (SA buckets from 01:00)."""
    if not hourly:
        return 0
    return 0 if _hourly_slot_empty(hourly, 0) else 1


def build_h0_carryover_row(
    plan_date: str,
    prev_day_hourly: dict[str, list[float | None]],
    *,
    forecast_pv: float,
    forecast_load: float,
    cfg: dict,
    params: dict[str, float | int],
    rce_price: float | None,
    timer_schedule: str = "",
) -> dict[str, Any] | None:
    """First row at 00:00–01:00 when today's Influx is empty.

    Bat/grid/SOC from yesterday 23:00–00:00 (hour 23); PV/Load from forecast.
    Replaced by today's hour-0 actuals on the 01:00 plan refresh.
    """
    prev_h = 23
    bat_in = _hourly_slot(prev_day_hourly, prev_h, "bat_charge")
    bat_out = _hourly_slot(prev_day_hourly, prev_h, "bat_discharge")
    grid_buy_h = _hourly_slot(prev_day_hourly, prev_h, "grid_buy")
    grid_sell_h = _hourly_slot(prev_day_hourly, prev_h, "grid_sell")
    soc_h = _hourly_slot(prev_day_hourly, prev_h, "soc")

    if all(v is None for v in (bat_in, bat_out, grid_buy_h, grid_sell_h, soc_h)):
        return None

    min_soc_pct = float(params["min_soc_pct"])
    if soc_h is not None:
        soc_pct = max(min_soc_pct, min(100.0, float(soc_h)))
    else:
        soc_pct = min_soc_pct

    bat_in_kwh = float(bat_in or 0.0)
    bat_out_kwh = float(bat_out or 0.0)
    battery_delta = bat_in_kwh - bat_out_kwh

    epsilon = float(params["epsilon_kwh"])
    grid_import = abs(float(grid_buy_h)) if grid_buy_h is not None and grid_buy_h < 0 else 0.0
    grid_export = float(grid_sell_h) if grid_sell_h is not None and grid_sell_h > 0 else 0.0
    if grid_import <= 0.0 and grid_export <= 0.0 and (bat_in is not None or bat_out is not None):
        grid_import, grid_export = derive_grid_flows_from_balance(
            forecast_pv, forecast_load, battery_delta, epsilon=epsilon,
        )

    hour_dt = datetime.strptime(plan_date, "%Y-%m-%d").replace(hour=0)
    buy_price, g12_zone = get_buy_price(hour_dt, cfg)
    action = classify_action(
        bat_charge=bat_in_kwh,
        bat_discharge=bat_out_kwh,
        grid_import=grid_import,
        grid_export=grid_export,
        production=forecast_pv,
    )
    cash = hour_meter_cash_pln(
        grid_import, grid_export, buy_price, rce_price, cfg, g12_zone=g12_zone,
    )

    return {
        "hour": 0,
        "plan_date": plan_date,
        "start": interval_end_label(hour_dt),
        "production": round(forecast_pv, 3),
        "consumption": round(forecast_load, 3),
        "battery": round(battery_delta, 3),
        "bat_charge": round(bat_in_kwh, 3),
        "bat_discharge": round(bat_out_kwh, 3),
        "grid_import": round(grid_import, 3),
        "grid_export": round(grid_export, 3),
        "soc": round(soc_pct, 1),
        "import_cost": cash["import_cost"],
        "export_revenue": cash["export_revenue"],
        "energy_cost": cash["energy_cost"],
        "service_cost": cash["service_cost"],
        "cost": cash["cost"],
        "action": action,
        "timer_schedule": timer_schedule,
        "rce_price": round(rce_price, 4) if rce_price is not None else None,
        "export_credit": cash["export_credit"],
        "g12_zone": g12_zone,
        "buy_price": round(buy_price, 4),
        "export_planned": False,
        "carryover_hour": True,
    }


def build_completed_history_rows(
    plan_date: str,
    until_hour: int,
    today_hourly: dict[str, list[float | None]],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
    params: dict[str, float | int],
) -> list[dict[str, Any]]:
    """Completed today hours [0, until_hour) from Influx (for collapsed PROD table)."""
    if until_hour <= 0 or not today_hourly:
        return []

    base = datetime.strptime(plan_date, "%Y-%m-%d")
    quarters = quarters_by_date.get(plan_date) or []
    rows: list[dict[str, Any]] = []

    for h in range(0, until_hour):
        dt = base.replace(hour=h)
        c0 = h * 4
        hour_rce_vals = [
            float(v) for v in quarters[c0:c0 + 4] if v is not None
        ]
        rce_price = (
            round(sum(hour_rce_vals) / len(hour_rce_vals), 4)
            if hour_rce_vals else None
        )
        row = _row_from_hourly_actual(
            dt, today_hourly,
            cfg=cfg, params=params, rce_price=rce_price, plan_date=plan_date,
        )
        if row:
            rows.append(row)
    return rows


def build_actual_hour_row(
    plan_hour_start: datetime,
    *,
    forecast_pv: float,
    forecast_load: float,
    today_hourly: dict[str, list[float | None]] | None,
    prev_day_hourly: dict[str, list[float | None]] | None = None,
    live_metrics: dict[str, Any],
    cfg: dict,
    params: dict[str, float | int],
    rce_price: float | None,
    now: datetime,
) -> dict[str, Any]:
    """Build first plan row: last complete hour actuals, label = plan_hour_start."""
    completed_dt, hourly = _completed_hourly(
        plan_hour_start, today_hourly, prev_day_hourly,
    )
    data_hour = completed_dt.hour

    pv_h = _hourly_slot(hourly, data_hour, "pv")
    load_h = _hourly_slot(hourly, data_hour, "load")
    bat_in = _hourly_slot(hourly, data_hour, "bat_charge")
    bat_out = _hourly_slot(hourly, data_hour, "bat_discharge")
    grid_buy_h = _hourly_slot(hourly, data_hour, "grid_buy")
    grid_sell_h = _hourly_slot(hourly, data_hour, "grid_sell")
    soc_h = _hourly_slot(hourly, data_hour, "soc")

    pv = float(pv_h) if pv_h is not None else 0.0
    load = float(load_h) if load_h is not None else 0.0

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = float(params["min_soc_pct"])
    live_soc_pct = max(
        min_soc_pct,
        min(100.0, float(live_metrics.get("battery_soc", 50.0))),
    )
    soc_kwh = (live_soc_pct / 100.0) * battery_cap

    if soc_h is not None:
        soc_pct = max(min_soc_pct, min(100.0, float(soc_h)))
    else:
        soc_pct = live_soc_pct

    if bat_in is not None or bat_out is not None:
        battery_delta = float(bat_in or 0.0) - float(bat_out or 0.0)
    else:
        battery_delta = 0.0

    epsilon = float(params["epsilon_kwh"])
    grid_import = abs(float(grid_buy_h)) if grid_buy_h is not None and grid_buy_h < 0 else 0.0
    grid_export = float(grid_sell_h) if grid_sell_h is not None and grid_sell_h > 0 else 0.0
    if grid_import <= 0.0 and grid_export <= 0.0 and (bat_in is not None or bat_out is not None):
        grid_import, grid_export = derive_grid_flows_from_balance(
            pv, load, battery_delta, epsilon=epsilon,
        )

    buy_price, g12_zone = get_buy_price(completed_dt, cfg)

    bat_in_kwh = float(bat_in or 0.0)
    bat_out_kwh = float(bat_out or 0.0)
    action = classify_action(
        bat_charge=bat_in_kwh,
        bat_discharge=bat_out_kwh,
        grid_import=grid_import,
        grid_export=grid_export,
        production=pv,
    )
    cash = hour_meter_cash_pln(
        grid_import, grid_export, buy_price, rce_price, cfg, g12_zone=g12_zone,
    )

    return {
        "hour": plan_hour_start.hour,
        "start": interval_end_label(plan_hour_start),
        "production": round(pv, 3),
        "consumption": round(load, 3),
        "battery": round(battery_delta, 3),
        "bat_charge": round(bat_in_kwh, 3),
        "bat_discharge": round(bat_out_kwh, 3),
        "grid_import": round(grid_import, 3),
        "grid_export": round(grid_export, 3),
        "soc": round(soc_pct, 1),
        "import_cost": cash["import_cost"],
        "export_revenue": cash["export_revenue"],
        "energy_cost": cash["energy_cost"],
        "service_cost": cash["service_cost"],
        "cost": cash["cost"],
        "action": action,
        "rce_price": round(rce_price, 4) if rce_price is not None else None,
        "export_credit": cash["export_credit"],
        "g12_zone": g12_zone,
        "buy_price": round(buy_price, 4),
        "export_planned": False,
        "actual_hour": True,
        "soc_kwh": soc_kwh,
    }
