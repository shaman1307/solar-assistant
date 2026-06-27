"""Actual Influx data for the last completed hour in plan simulation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .g12_pricing import get_buy_price
from .plan_cost import (
    derive_grid_flows_from_balance,
    hour_meter_cash_pln,
)
from .inverter_sim import _initial_soc_kwh
from .timer_plan import classify_action

SLOTS_PER_HOUR_10M = 6
Q15_PER_HOUR = 4
TEN_MIN_KWH_PER_KW = 10.0 / 60.0
PARTIAL_Q15_SCALE = 1.5


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
    rce_q15: list[float | None] | None = None,
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
        "rce_q15": list(rce_q15) if rce_q15 else None,
        "export_credit": cash["export_credit"],
        "g12_zone": g12_zone,
        "buy_price": round(buy_price, 4),
        "export_planned": False,
        "history_hour": True,
    }


def hour_start_soc_kwh(
    hourly: dict[str, list[float | None]] | None,
    hour: int,
    battery_cap: float,
    min_soc_pct: float,
) -> float | None:
    """SOC at the start of *hour* from Influx (end of previous hour, or backtrack at h=0)."""
    if not hourly or not (0 <= hour < 24):
        return None
    min_kwh = (float(min_soc_pct) / 100.0) * battery_cap
    if hour == 0:
        start_kwh, _ = _initial_soc_kwh(hourly, battery_cap)
        return start_kwh

    soc_series = hourly.get("soc") or [None] * 24
    prev_pct = soc_series[hour - 1] if hour - 1 < len(soc_series) else None
    if prev_pct is not None:
        pct = max(min_soc_pct, min(100.0, float(prev_pct)))
        return (pct / 100.0) * battery_cap

    end_pct = soc_series[hour] if hour < len(soc_series) else None
    if end_pct is None:
        return None
    end_kwh = (max(min_soc_pct, min(100.0, float(end_pct))) / 100.0) * battery_cap
    bc = float((hourly.get("bat_charge") or [None] * 24)[hour] or 0.0)
    bd = float((hourly.get("bat_discharge") or [None] * 24)[hour] or 0.0)
    return max(min_kwh, min(battery_cap, end_kwh - bc + bd))


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
    """Fallback first row at 00:00–01:00 when smart plan slots are unavailable."""
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
        hour_rce_q15 = list(quarters[c0:c0 + 4])
        hour_rce_vals = [
            float(v) for v in hour_rce_q15 if v is not None
        ]
        rce_price = (
            round(sum(hour_rce_vals) / len(hour_rce_vals), 4)
            if hour_rce_vals else None
        )
        row = _row_from_hourly_actual(
            dt, today_hourly,
            cfg=cfg, params=params, rce_price=rce_price, plan_date=plan_date,
            rce_q15=hour_rce_q15,
        )
        if row:
            rows.append(row)
    return rows


def _hour_10m_base(hour: int) -> int:
    return hour * SLOTS_PER_HOUR_10M


def _ten_min_energy_kwh(
    series: list[float | None] | None,
    hour: int,
    start_slot: int,
    count: int,
    *,
    scale: float = 1.0,
) -> float:
    """kWh from 10-min mean kW buckets within one clock hour."""
    if not series or count <= 0:
        return 0.0
    base = _hour_10m_base(hour)
    total = 0.0
    for i in range(start_slot, start_slot + count):
        idx = base + i
        if idx < len(series) and series[idx] is not None:
            total += float(series[idx]) * TEN_MIN_KWH_PER_KW
    return total * scale


def _q15_forecast_energy(
    q15: list[float] | None,
    hour: int,
    q_start: int,
    q_count: int,
) -> float:
    if not q15 or q_count <= 0:
        return 0.0
    base = hour * Q15_PER_HOUR
    total = 0.0
    for q in range(q_start, q_start + q_count):
        idx = base + q
        if idx < len(q15):
            total += float(q15[idx])
    return total


def _actual_energy_for_quarter(
    series: list[float | None] | None,
    hour: int,
    quarter: int,
) -> float:
    """Influx 10-min energy accumulated so far in the hour (scaled at :15/:45)."""
    if quarter <= 0:
        return 0.0
    if quarter == 1:
        return _ten_min_energy_kwh(series, hour, 0, 1, scale=PARTIAL_Q15_SCALE)
    if quarter == 2:
        return _ten_min_energy_kwh(series, hour, 0, 3)
    if quarter == 3:
        first = _ten_min_energy_kwh(series, hour, 0, 3)
        partial = _ten_min_energy_kwh(series, hour, 3, 1, scale=PARTIAL_Q15_SCALE)
        return first + partial
    return 0.0


def _forecast_energy_for_quarter(
    q15: list[float] | None,
    hour: int,
    quarter: int,
) -> float:
    """Forecast q15 kWh for the not-yet-observed tail of the hour."""
    if quarter <= 0:
        return _q15_forecast_energy(q15, hour, 0, Q15_PER_HOUR)
    if quarter == 1:
        return _q15_forecast_energy(q15, hour, 1, 3)
    if quarter == 2:
        return _q15_forecast_energy(q15, hour, 2, 2)
    if quarter == 3:
        return _q15_forecast_energy(q15, hour, 3, 1)
    return 0.0


def _soc_pct_at_10m(
    series: list[float | None] | None,
    hour: int,
    slot: int,
    fallback: float,
) -> float:
    if not series:
        return fallback
    idx = _hour_10m_base(hour) + slot
    if 0 <= idx < len(series) and series[idx] is not None:
        return float(series[idx])
    return fallback


def _actual_soc_delta_for_quarter(
    soc_series: list[float | None] | None,
    hour: int,
    quarter: int,
    soc_start_pct: float,
) -> float:
    if quarter <= 0:
        return 0.0
    base = _hour_10m_base(hour)
    if quarter == 1:
        end = _soc_pct_at_10m(soc_series, hour, 1, soc_start_pct)
        return (end - soc_start_pct) * PARTIAL_Q15_SCALE
    if quarter == 2:
        end = _soc_pct_at_10m(soc_series, hour, 2, soc_start_pct)
        return end - soc_start_pct
    if quarter == 3:
        mid = _soc_pct_at_10m(soc_series, hour, 2, soc_start_pct)
        end = _soc_pct_at_10m(soc_series, hour, 3, mid)
        return (mid - soc_start_pct) + (end - mid) * PARTIAL_Q15_SCALE
    return 0.0


def _forecast_soc_delta_for_quarter(
    q15_slots: list[dict[str, Any]] | None,
    quarter: int,
    soc_start_pct: float,
    forecast_soc_hour_start_pct: float | None = None,
) -> float:
    """SOC change (pp) from forecast q15 plan slots for the remaining hour tail."""
    if not q15_slots:
        return 0.0

    def _slot_soc(qi: int) -> float:
        if qi < 0:
            return soc_start_pct
        if qi >= len(q15_slots):
            return _slot_soc(qi - 1)
        return float(q15_slots[qi].get("soc_pct") or _slot_soc(qi - 1))

    f_start = (
        float(forecast_soc_hour_start_pct)
        if forecast_soc_hour_start_pct is not None
        else soc_start_pct
    )

    if quarter <= 0:
        # Full-hour forecast Δ (18:00–19:00); end-of-hour SOC = live + this delta.
        plan_delta = _slot_soc(3) - soc_start_pct
        return plan_delta

    if quarter == 1:
        # Forecast SOC Δ 18:15–19:00
        return _slot_soc(3) - _slot_soc(0)
    if quarter == 2:
        # Forecast SOC Δ 18:30–19:00
        return _slot_soc(3) - _slot_soc(1)
    if quarter == 3:
        # Forecast SOC Δ 18:45–19:00
        return _slot_soc(3) - _slot_soc(2)
    return 0.0


def blend_current_hour_end(
    hour: int,
    now: datetime,
    *,
    forecast_pv_q15: list[float] | None,
    forecast_load_q15: list[float] | None,
    forecast_pv_hourly: float,
    forecast_load_hourly: float,
    series_10min: dict[str, list[float | None]] | None,
    soc_start_pct: float,
    forecast_q15_slots: list[dict[str, Any]] | None = None,
    forecast_soc_hour_start_pct: float | None = None,
) -> tuple[float, float, float]:
    """Project PV/Load kWh and end-of-hour SOC % for the in-progress hour.

    Blends Influx 10-min actuals with forecast q15 tails; rules match the
      :00 / :15 / :30 / :45 refresh schedule.
    """
    if now.hour != hour:
        quarter = 0
    else:
        quarter = min(3, max(0, now.minute // 15))

    pv_10 = (series_10min or {}).get("pv")
    load_10 = (series_10min or {}).get("load")
    soc_10 = (series_10min or {}).get("soc")

    if quarter == 0:
        pv = float(forecast_pv_hourly)
        load = float(forecast_load_hourly)
        soc_delta = _forecast_soc_delta_for_quarter(
            forecast_q15_slots,
            quarter,
            soc_start_pct,
            forecast_soc_hour_start_pct,
        )
        soc_end = soc_start_pct + soc_delta
    else:
        pv = _actual_energy_for_quarter(pv_10, hour, quarter)
        pv += _forecast_energy_for_quarter(forecast_pv_q15, hour, quarter)
        load = _actual_energy_for_quarter(load_10, hour, quarter)
        load += _forecast_energy_for_quarter(forecast_load_q15, hour, quarter)
        soc_delta = _actual_soc_delta_for_quarter(soc_10, hour, quarter, soc_start_pct)
        soc_delta += _forecast_soc_delta_for_quarter(
            forecast_q15_slots,
            quarter,
            soc_start_pct,
            forecast_soc_hour_start_pct,
        )
        soc_end = soc_start_pct + soc_delta

    return round(pv, 3), round(load, 3), round(soc_end, 1)


def forecast_soc_hour_start_pct(
    smart_today: dict[str, Any] | None,
    hour: int,
    *,
    day_start_soc_pct: float,
    min_soc_pct: float,
) -> float:
    """Forecast SOC (%) at the start of *hour* from the head replay path."""
    if not smart_today or not (0 <= hour < 24):
        return day_start_soc_pct
    q15_by_hour = smart_today.get("q15_by_hour") or {}
    if hour > 0:
        prev_slots = q15_by_hour.get(hour - 1) or []
        if prev_slots:
            v = prev_slots[-1].get("soc_pct")
            if v is not None:
                return max(min_soc_pct, min(100.0, float(v)))
    return day_start_soc_pct


def apply_current_hour_blend(
    pv_hourly: list[float],
    load_hourly: list[float],
    hour: int,
    now: datetime,
    *,
    forecast_pv_q15: list[float] | None,
    forecast_load_q15: list[float] | None,
    series_10min: dict[str, list[float | None]] | None,
    soc_start_pct: float,
    forecast_soc_hour_start_pct: float | None = None,
) -> tuple[list[float], list[float], float, float]:
    """Patch *pv_hourly*/*load_hourly* at *hour* with blended end-of-hour estimates."""
    if not (0 <= hour < len(pv_hourly)):
        return pv_hourly, load_hourly, 0.0, soc_start_pct
    fpv = float(pv_hourly[hour])
    flo = float(load_hourly[hour])
    pv_b, load_b, soc_end = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=forecast_pv_q15,
        forecast_load_q15=forecast_load_q15,
        forecast_pv_hourly=fpv,
        forecast_load_hourly=flo,
        series_10min=series_10min,
        soc_start_pct=soc_start_pct,
        forecast_q15_slots=None,
        forecast_soc_hour_start_pct=forecast_soc_hour_start_pct,
    )
    pv_out = list(pv_hourly)
    load_out = list(load_hourly)
    pv_out[hour] = pv_b
    load_out[hour] = load_b
    return pv_out, load_out, pv_b, load_b


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
