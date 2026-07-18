"""Tail-hour balance cost after optimization horizon (physics + G12 tariff)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .g12_pricing import get_buy_price


def pv_load_energy_split(
    pv: float,
    load: float,
    *,
    eta_pv_load: float,
) -> tuple[float, float]:
    """AC load deficit after PV→load; remaining PV (DC kWh) for battery/export."""
    if eta_pv_load <= 0:
        return max(0.0, load), max(0.0, pv)
    deficit = max(0.0, load - pv * eta_pv_load)
    pv_surplus = max(0.0, pv - load / eta_pv_load)
    return deficit, pv_surplus


def _natural_hour(
    soc: float,
    pv: float,
    load: float,
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    epsilon: float,
) -> tuple[float, float, float]:
    """Battery+PV only. Returns (soc_end, grid_import, grid_export)."""
    grid_import = 0.0
    grid_export = 0.0

    deficit, pv_surplus = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    available = max(0.0, soc - min_kwh)

    if deficit > epsilon and available > epsilon and eta_out > 0:
        supplied = min(deficit, available * eta_out)
        soc -= supplied / eta_out
        available = max(0.0, soc - min_kwh)
        if deficit > supplied + epsilon:
            grid_import += deficit - supplied
    elif deficit > epsilon:
        grid_import += deficit

    export_headroom = max(0.0, ac_cap_kw - load)
    head_room = max(0.0, battery_cap - soc)
    if pv_surplus > epsilon:
        if head_room > epsilon and eta_pv_battery > 0:
            taken = min(pv_surplus, head_room / eta_pv_battery)
            stored = taken * eta_pv_battery
            soc += stored
            pv_surplus -= taken
        if pv_surplus > epsilon and export_headroom > epsilon and eta_pv_grid > 0:
            grid_export += min(pv_surplus * eta_pv_grid, export_headroom)

    soc = max(min_kwh, min(battery_cap, soc))
    return soc, grid_import, grid_export


def _forecast_day_key(dt: datetime, today_date) -> str:
    return "today" if dt.date() == today_date else "tomorrow"


def build_tail_hour_arrays(
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any],
    cfg: dict,
    rce_map: dict[tuple[str, int], float | None],
    export_credit_fn,
    *,
    tail_start_hour: int | None = None,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """PV/load/buy/export-credit for calendar hours after the optimized horizon.

  *tail_start_hour* is the first calendar hour not covered by DP steps (e.g. 24 when
    the plan ends at 23:45). Defaults to *end_dt.hour* for legacy hourly horizons.
    """
    day_key = _forecast_day_key(end_dt, today_date)
    pv_day = forecast[day_key]["pv"]
    load_day = forecast[day_key]["load"]
    tail_pv: list[float] = []
    tail_load: list[float] = []
    tail_buy: list[float] = []
    tail_export_credit: list[float] = []
    date_str = end_dt.strftime("%Y-%m-%d")
    start_h = end_dt.hour if tail_start_hour is None else int(tail_start_hour)
    for h in range(start_h, 24):
        tail_pv.append(float(pv_day[h]))
        tail_load.append(float(load_day[h]))
        dt = end_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        buy, _ = get_buy_price(dt, cfg)
        tail_buy.append(buy)
        rce = rce_map.get((date_str, h))
        tail_export_credit.append(export_credit_fn(rce, from_battery=False))
    return tail_pv, tail_load, tail_buy, tail_export_credit


def tail_balance_cost_pln(
    soc_kwh: float,
    tail_pv: list[float],
    tail_load: list[float],
    tail_buy: list[float],
    tail_export_credit: list[float],
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    epsilon: float,
) -> float:
    """PLN: grid imports + PV spill opportunity cost (buy − export credit) after horizon."""
    soc = soc_kwh
    cost = 0.0
    for pv, load, buy, export_credit in zip(
        tail_pv, tail_load, tail_buy, tail_export_credit,
    ):
        soc, grid_import, grid_export = _natural_hour(
            soc, pv, load,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=ac_cap_kw,
            eta_out=eta_out, eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
            epsilon=epsilon,
        )
        cost += grid_import * buy
        if grid_export > epsilon:
            cost += grid_export * max(0.0, buy - export_credit)
    return cost
