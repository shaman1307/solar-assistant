"""
G12 Energa bill model and horizon cost minimization (dynamic programming).

Objective:
  Σ_h [ grid_import_h × buy_brutto_h − grid_export_h × export_credit_h ]
  + tail_balance_cost(soc_end)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

from .grid_config import grid_export_threshold_pln_kwh
from .plan_spill import build_tail_hour_arrays, pv_load_energy_split, tail_balance_cost_pln
from .simulation_config import (
    plan_min_soc_kwh,
    plan_reserve_min_soc_kwh,
    plan_timer_charge_power_kw,
    plan_timer_discharge_ac_kw,
)


@dataclass(frozen=True)
class G12Tariff:
    offpeak_full: float
    offpeak_energy: float
    peak_full: float
    peak_energy: float


def g12_tariff_from_cfg(cfg: dict) -> G12Tariff:
    g12 = cfg["grid"]["g12"]
    return G12Tariff(
        offpeak_full=float(g12["offpeak_price_pln_kwh"]),
        offpeak_energy=float(g12["offpeak_energy_only_pln_kwh"]),
        peak_full=float(g12["peak_price_pln_kwh"]),
        peak_energy=float(g12["peak_energy_only_pln_kwh"]),
    )


def battery_export_break_even_rce(tariff: G12Tariff, cfg: dict | None = None) -> float:
    """Minimum RCE (PLN/kWh) for battery export vs self-use at offpeak buy."""
    if cfg is not None:
        return grid_export_threshold_pln_kwh(cfg)
    return tariff.offpeak_full


def _rce_at_or_above(
    rce_series: list[float | None],
    step: int,
    floor: float,
    *,
    epsilon: float = 0.0,
) -> bool:
    if step < 0 or step >= len(rce_series):
        return False
    rce = rce_series[step]
    return rce is not None and float(rce) >= floor - epsilon


def battery_export_step_allowed(
    step: int,
    rce_series: list[float | None],
    floor: float,
    *,
    step_scale: float = 0.25,
    epsilon: float = 0.0,
) -> bool:
    """Battery export only when two consecutive q15 slots in the hour are above G12 offpeak."""
    if not _rce_at_or_above(rce_series, step, floor, epsilon=epsilon):
        return False
    slots_per_hour = max(1, int(round(1.0 / step_scale)) if step_scale > 0 else 1)
    q_in_hour = step % slots_per_hour
    if q_in_hour >= 1 and _rce_at_or_above(rce_series, step - 1, floor, epsilon=epsilon):
        return True
    if q_in_hour < slots_per_hour - 1 and _rce_at_or_above(
        rce_series, step + 1, floor, epsilon=epsilon,
    ):
        return True
    if slots_per_hour == 1 and _rce_at_or_above(rce_series, step - 1, floor, epsilon=epsilon):
        return True
    return False


def optimization_battery_export_value(
    rce: float | None,
    tariff: G12Tariff,
    cfg: dict | None = None,
) -> float:
    """Marginal bill benefit of battery export vs self-use at offpeak buy."""
    if rce is None:
        return 0.0
    rce_f = float(rce)
    floor = battery_export_break_even_rce(tariff, cfg)
    if rce_f < floor:
        return 0.0
    return rce_f - floor


def export_credit_price(
    rce: float | None,
    tariff: G12Tariff,
    *,
    from_battery: bool,
    cfg: dict | None = None,
) -> float:
    if rce is None:
        return 0.0
    if from_battery and float(rce) < battery_export_break_even_rce(tariff, cfg):
        return 0.0
    return float(rce)


def hourly_cash_pln(
    grid_import: float,
    grid_export: float,
    buy_brutto: float,
    export_credit: float,
) -> float:
    return grid_import * buy_brutto - grid_export * export_credit


@dataclass
class HourControl:
    grid_charge_kw: float
    battery_export_kwh: float
    load_from_grid: bool = False


@dataclass
class HourPhysics:
    soc_end: float
    battery_delta: float
    grid_import: float
    grid_export: float


def simulate_hour(
    soc_kwh: float,
    pv: float,
    load: float,
    control: HourControl,
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kw: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    epsilon: float,
    reserve_soc_kwh: float | None = None,
) -> HourPhysics:
    """One step: PV (DC) vs load (AC); PV→battery is 1:1 (DC/DC)."""
    reserve = min_kwh if reserve_soc_kwh is None else max(min_kwh, reserve_soc_kwh)
    soc = soc_kwh
    grid_import = 0.0
    grid_export = 0.0
    battery_delta = 0.0

    deficit, pv_surplus = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    available = max(0.0, soc - min_kwh)
    grid_charging = control.grid_charge_kw > epsilon
    load_on_grid = grid_charging or control.load_from_grid

    if deficit > epsilon:
        if load_on_grid:
            # Timer grid charge: load from grid in parallel; battery charges at set rate.
            grid_import += deficit
        else:
            supplied = min(deficit, available * eta_out)
            withdraw_load = supplied / eta_out if eta_out > 0 else 0.0
            soc -= withdraw_load
            battery_delta -= withdraw_load
            available = max(0.0, soc - min_kwh)
            if deficit > supplied + epsilon:
                grid_import += deficit - supplied

    export_headroom = max(0.0, ac_cap_kw - load)
    batt_export = min(max(0.0, control.battery_export_kwh), export_headroom)
    available_export = max(0.0, soc - reserve)
    if batt_export > epsilon and available_export > epsilon and eta_out > 0:
        export_withdraw = min(batt_export / eta_out, available_export)
        soc -= export_withdraw
        batt_export = export_withdraw * eta_out
        grid_export += batt_export
        battery_delta -= export_withdraw
        available = max(0.0, soc - min_kwh)

    head_room = max(0.0, battery_cap - soc)
    if pv_surplus > epsilon:
        if head_room > epsilon:
            stored = min(pv_surplus, head_room)
            soc += stored
            battery_delta += stored
            pv_surplus -= stored
        if pv_surplus > epsilon:
            pv_exp = min(
                pv_surplus * eta_pv_grid,
                max(0.0, export_headroom - grid_export),
            )
            grid_export += pv_exp

    head_room = max(0.0, battery_cap - soc)
    if control.grid_charge_kw > epsilon and head_room > epsilon:
        stored_grid = min(control.grid_charge_kw, head_room)
        grid_import += stored_grid / eta_grid if eta_grid > 0 else 0.0
        soc += stored_grid
        battery_delta += stored_grid

    soc = max(min_kwh, min(battery_cap, soc))
    return HourPhysics(
        soc_end=soc,
        battery_delta=battery_delta,
        grid_import=grid_import,
        grid_export=grid_export,
    )


def _soc_bin(soc_kwh: float, min_kwh: float, bin_kwh: float) -> int:
    return max(0, int(round((soc_kwh - min_kwh) / bin_kwh)))


def _soc_from_bin(idx: int, min_kwh: float, bin_kwh: float) -> float:
    return min_kwh + idx * bin_kwh


def _reserve_soc_kwh_from_step(
    step: int,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    *,
    slots_per_hour: int = 4,
) -> float:
    """Battery kWh that must remain after step (load until PV covers house).

    PV coverage is checked per clock hour (not single q15) so one sunny
    15-min slot does not understate the night reserve.
    """
    need = 0.0
    j = step + 1
    while j < len(pv_series):
        deficit, _ = pv_load_energy_split(
            pv_series[j], load_series[j], eta_pv_load=eta_pv_load,
        )
        if deficit > epsilon:
            need += deficit / eta_out if eta_out > 0 else deficit
        j += 1
        if slots_per_hour > 0 and j % slots_per_hour == 0:
            h_start = j - slots_per_hour
            pv_h = sum(pv_series[h_start:j])
            load_h = sum(load_series[h_start:j])
            if pv_h * eta_pv_load >= load_h - epsilon:
                break
    return need + reserve_floor_kwh


def _max_battery_export_kwh(
    soc_kwh: float,
    pv: float,
    load: float,
    *,
    min_kwh: float,
    ac_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    reserve_soc_kwh: float,
    epsilon: float,
) -> float:
    """Max meter kWh exportable from battery after load, respecting reserve floor."""
    export_headroom = max(0.0, ac_cap_kw - load)
    if export_headroom <= epsilon or eta_out <= 0:
        return 0.0
    soc = soc_kwh
    deficit, _ = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    if deficit > epsilon and eta_out > 0:
        withdraw = min(deficit / eta_out, max(0.0, soc - min_kwh))
        soc -= withdraw
    exportable_soc = max(0.0, soc - max(min_kwh, reserve_soc_kwh))
    return min(exportable_soc * eta_out, export_headroom)


def _allow_grid_charge(
    soc_kwh: float,
    pv: float,
    load: float,
    buy_p: float,
    offpeak_buy: float,
    reserve_soc_kwh: float,
    min_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
) -> bool:
    """Grid charge at offpeak only to restore load reserve or cover load deficit."""
    if buy_p > offpeak_buy + epsilon:
        return False
    if reserve_soc_kwh > soc_kwh + epsilon:
        return True
    _, pv_surplus = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    if pv_surplus > epsilon:
        return False
    deficit, _ = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    if deficit <= epsilon or eta_out <= 0:
        return False
    available = max(0.0, soc_kwh - min_kwh)
    return available * eta_out + epsilon < deficit


def _control_options(
    soc_kwh: float,
    pv: float,
    load: float,
    *,
    battery_cap: float,
    min_kwh: float,
    discharge_ac_cap_kw: float,
    charge_batt_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    buy_p: float,
    offpeak_buy: float,
    reserve_soc_kwh: float,
    allow_battery_export: bool,
) -> list[HourControl]:
    head_room = battery_cap - soc_kwh
    offpeak = buy_p <= offpeak_buy + epsilon
    allow_charge = (
        _allow_grid_charge(
            soc_kwh, pv, load, buy_p, offpeak_buy, reserve_soc_kwh,
            min_kwh, eta_out, eta_pv_load, epsilon,
        )
        and head_room > epsilon
    )
    charge_rate = min(charge_batt_cap_kw, head_room) if allow_charge else 0.0

    # Flat off-peak: fill battery while forward reserve exceeds SOC (no idle gaps).
    if offpeak and reserve_soc_kwh > soc_kwh + epsilon and charge_rate > epsilon:
        return [HourControl(charge_rate, 0.0)]

    opts = [HourControl(0.0, 0.0)]
    if charge_rate > epsilon:
        opts.append(HourControl(charge_rate, 0.0))

    max_batt_export = _max_battery_export_kwh(
        soc_kwh, pv, load,
        min_kwh=min_kwh, ac_cap_kw=discharge_ac_cap_kw,
        eta_out=eta_out, eta_pv_load=eta_pv_load,
        reserve_soc_kwh=reserve_soc_kwh,
        epsilon=epsilon,
    )
    # Power-tier export options (max / half / quarter of slot AC cap), capped by SOC reserve.
    min_viable = max(epsilon, discharge_ac_cap_kw * 0.25)
    if max_batt_export >= min_viable and allow_battery_export:
        for frac in (1.0, 0.5, 0.25):
            tier_cap = discharge_ac_cap_kw * frac
            tier_export = min(max_batt_export, tier_cap)
            if tier_export >= min_viable:
                opts.append(HourControl(0.0, tier_export))

    seen: set[tuple[float, float]] = set()
    out: list[HourControl] = []
    for o in opts:
        key = (round(o.grid_charge_kw, 3), round(o.battery_export_kwh, 3))
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def build_extended_pv_load_for_reserve(
    pv_series: list[float],
    load_series: list[float],
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
) -> tuple[list[float], list[float]]:
    """Extend PV/load into tomorrow for reserve when the horizon ends same calendar day."""
    forecast_data = forecast or {
        "today": {"pv": [], "load": []},
        "tomorrow": {"pv": [], "load": []},
    }
    pv_for_reserve = pv_series
    load_for_reserve = load_series
    if (
        step_scale < 1.0
        and end_dt.date() == today_date
        and forecast_data.get("tomorrow", {}).get("pv")
        and forecast_data.get("tomorrow", {}).get("load")
    ):
        rep = int(round(1.0 / step_scale))
        if rep > 0:
            pv_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["pv"] or [])][:24]
            load_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["load"] or [])][:24]
            pv_ext = list(pv_series)
            load_ext = list(load_series)
            for h in range(24):
                pv_ext.extend([pv_tomorrow[h] * step_scale] * rep)
                load_ext.extend([load_tomorrow[h] * step_scale] * rep)
            pv_for_reserve = pv_ext
            load_for_reserve = load_ext
    return pv_for_reserve, load_for_reserve


def reserve_soc_per_step(
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    *,
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    step_scale: float = 1.0,
    end_dt: datetime | None = None,
    today_date=None,
    forecast: dict[str, Any] | None = None,
) -> list[float]:
    """Reserve floor (kWh) after each step — load until PV covers house again."""
    end = end_dt or datetime.now()
    pv_r, load_r = build_extended_pv_load_for_reserve(
        pv_series, load_series,
        step_scale=step_scale, end_dt=end, today_date=today_date, forecast=forecast,
    )
    eps_step = max(epsilon * step_scale, 0.001)
    return [
        _reserve_soc_kwh_from_step(
            s, pv_r, load_r, reserve_floor_kwh, eta_out, eta_pv_load, eps_step,
        )
        for s in range(steps)
    ]


def _tail_start_hour(
    *,
    steps: int,
    rce_step_offset: int,
    step_scale: float,
    end_dt: datetime,
) -> int:
    """First calendar hour after the last optimized step (no double-count with DP)."""
    if steps <= 0:
        return end_dt.hour
    slots_per_hour = max(1, int(round(1.0 / step_scale)) if step_scale > 0 else 1)
    last_global = rce_step_offset + steps - 1
    return (last_global // slots_per_hour) + 1


def optimize_horizon(
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    rce_series: list[float | None],
    initial_soc_kwh: float,
    cfg: dict,
    params: dict[str, float | int],
    end_dt: datetime,
    today_date,
    rce_map: dict[tuple[str, int], float | None],
    forecast: dict[str, Any] | None = None,
    step_scale: float = 1.0,
    rce_step_offset: int = 0,
) -> list[HourControl]:
    from .plan_cost import hour_grid_cash_pln

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    reserve_floor_kwh = plan_reserve_min_soc_kwh(cfg)
    discharge_ac_kw = plan_timer_discharge_ac_kw(cfg)
    charge_dc_kw = plan_timer_charge_power_kw(cfg)
    epsilon = float(params["epsilon_kwh"])
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    tariff = g12_tariff_from_cfg(cfg)
    discharge_ac_step = discharge_ac_kw * step_scale
    charge_dc_step = charge_dc_kw * step_scale
    eps_step = max(epsilon * step_scale, 0.001)

    bin_kwh = max(0.5, battery_cap / max(1, int((battery_cap - min_kwh) / 0.5)))
    max_bin = int(math.ceil((battery_cap - min_kwh) / bin_kwh))
    inf = 1e15

    dp: list[dict[int, float]] = [{} for _ in range(steps + 1)]
    soc_at: list[dict[int, float]] = [{} for _ in range(steps + 1)]
    back: dict[tuple[int, int], tuple[int, HourControl]] = {}

    s0 = _soc_bin(initial_soc_kwh, min_kwh, bin_kwh)
    dp[0][s0] = 0.0
    soc_at[0][s0] = min(battery_cap, max(min_kwh, initial_soc_kwh))
    forecast_data = forecast or {
        "today": {"pv": [], "load": [], "pv_total": 0.0, "load_total": 0.0},
        "tomorrow": {"pv": [], "load": [], "pv_total": 0.0, "load_total": 0.0},
    }

    def _pv_export_credit(rce: float | None, *, from_battery: bool) -> float:
        return export_credit_price(rce, tariff, from_battery=from_battery, cfg=cfg)

    tail_start = _tail_start_hour(
        steps=steps, rce_step_offset=rce_step_offset,
        step_scale=step_scale, end_dt=end_dt,
    )
    tail_pv, tail_load, tail_buy, tail_export_credit = build_tail_hour_arrays(
        end_dt, today_date, forecast_data, cfg, rce_map, _pv_export_credit,
        tail_start_hour=tail_start,
    )

    # Reserve for battery export must cover the night until PV can carry the house load again.
    # In rolling production simulation, pv_series/load_series already include next-day hours.
    # In daily debug replay, optimization stops at 23:45, so we extend *only for reserve*
    # into tomorrow using forecast, otherwise the plan can over-export in the evening and
    # hit min SOC before morning.
    pv_for_reserve, load_for_reserve = build_extended_pv_load_for_reserve(
        pv_series, load_series,
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    )

    reserves = [
        _reserve_soc_kwh_from_step(
            s, pv_for_reserve, load_for_reserve, reserve_floor_kwh,
            eta_out, eta_pv_load, eps_step,
        )
        for s in range(steps)
    ]

    offpeak_buy = tariff.offpeak_full
    export_floor = grid_export_threshold_pln_kwh(cfg)

    for step in range(steps):
        pv = pv_series[step]
        load = load_series[step]
        buy_p = buy_prices[step]
        rce_idx = rce_step_offset + step
        rce = rce_series[rce_idx] if rce_idx < len(rce_series) else None
        allow_battery_export = battery_export_step_allowed(
            rce_idx, rce_series, export_floor,
            step_scale=step_scale, epsilon=eps_step,
        )

        for soc_bin, cost_in in list(dp[step].items()):
            soc = soc_at[step].get(
                soc_bin,
                min(battery_cap, _soc_from_bin(soc_bin, min_kwh, bin_kwh)),
            )
            reserve = reserves[step]
            for ctrl in _control_options(
                soc, pv, load,
                battery_cap=battery_cap, min_kwh=min_kwh,
                discharge_ac_cap_kw=discharge_ac_step,
                charge_batt_cap_kw=charge_dc_step,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                epsilon=eps_step, buy_p=buy_p, offpeak_buy=offpeak_buy,
                reserve_soc_kwh=reserve,
                allow_battery_export=allow_battery_export,
            ):
                phys = simulate_hour(
                    soc, pv, load, ctrl,
                    battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=discharge_ac_step,
                    eta_grid=eta_grid, eta_out=eta_out,
                    eta_pv_load=eta_pv_load,
                    eta_pv_grid=eta_pv_grid, epsilon=eps_step,
                    reserve_soc_kwh=reserve,
                )
                step_cost = hour_grid_cash_pln(
                    phys.grid_import, phys.grid_export, buy_p, rce, cfg,
                    battery_export=min(ctrl.battery_export_kwh, phys.grid_export),
                    g12_zone="peak" if buy_p > (tariff.peak_full + tariff.offpeak_full) * 0.5 else "offpeak",
                )["cost"]
                nb = min(max_bin, _soc_bin(phys.soc_end, min_kwh, bin_kwh))
                total = cost_in + step_cost
                if total < dp[step + 1].get(nb, inf):
                    dp[step + 1][nb] = total
                    back[(step + 1, nb)] = (soc_bin, ctrl)
                    soc_at[step + 1][nb] = phys.soc_end

    if not dp[steps]:
        return [HourControl(0.0, 0.0) for _ in range(steps)]

    def _total_cost(path_cost: float, soc_bin: int) -> float:
        soc_end = soc_at[steps].get(
            soc_bin,
            min(battery_cap, _soc_from_bin(soc_bin, min_kwh, bin_kwh)),
        )
        tail = tail_balance_cost_pln(
            soc_end, tail_pv, tail_load, tail_buy, tail_export_credit,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=discharge_ac_kw,
            eta_out=eta_out, eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid, epsilon=epsilon,
        )
        return path_cost + tail

    best_bin = min(dp[steps], key=lambda b: _total_cost(dp[steps][b], b))

    controls: list[HourControl] = []
    b = best_bin
    for step in range(steps, 0, -1):
        soc_bin, ctrl = back.get((step, b), (0, HourControl(0.0, 0.0)))
        controls.append(ctrl)
        b = soc_bin
    controls.reverse()
    return controls
