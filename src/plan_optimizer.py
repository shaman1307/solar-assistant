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
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

from .g12_pricing import get_buy_price
from .grid_config import grid_export_threshold_pln_kwh
from .plan_spill import build_tail_hour_arrays, pv_load_energy_split, tail_balance_cost_pln
from .simulation_config import (
    plan_min_soc_kwh,
    plan_reserve_min_soc_kwh,
    plan_timer_charge_grid_kw,
    plan_timer_discharge_ac_kw,
    plan_timer_min_hourly_transfer_kwh,
)

# --- Policy / DP constants ---
DP_SOC_BIN_KWH = 0.5
DP_COST_INF = 1e15
MIN_EPS_STEP_KWH = 0.001
EXPORT_POWER_FRACS = (1.0, 0.5, 0.25)
EXPORT_MIN_FRAC = 0.25
CONTROL_DEDUP_DECIMALS = 3
HOURS_PER_DAY = 24
# Calendar AM bound only when that day has no peak hours at all (e.g. weekend).
_ALL_OFFPEAK_COVER_HOUR_END = 12


def morning_cover_bound_from_hour_buys(
    hour_buys: list[float],
    *,
    offpeak_buy: float,
    epsilon: float = 0.0,
) -> int | None:
    """Exclusive clock hour when daytime PV cover may end overnight need.

    Derived from this calendar day's buy prices (not hardcoded 6–13):
    - two+ peak blocks → end of the first (morning) block;
    - one block starting before noon → its end;
    - one block starting late (evening only, e.g. truncated series) → its start
      so evening PV does not look like morning cover;
    - all offpeak (weekend) → None.
    """
    n = min(HOURS_PER_DAY, len(hour_buys))
    if n <= 0:
        return None
    off = float(offpeak_buy)
    eps = float(epsilon)
    is_peak = [float(hour_buys[h]) > off + eps for h in range(n)]
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not is_peak[i]:
            i += 1
            continue
        j = i + 1
        while j < n and is_peak[j]:
            j += 1
        blocks.append((i, j))
        i = j
    if not blocks:
        return None
    if len(blocks) >= 2:
        return blocks[0][1]
    start, end = blocks[0]
    if start < _ALL_OFFPEAK_COVER_HOUR_END:
        return end
    # Single late block only (evening peak visible; morning absent from series).
    return start


def _day_hour_buys_from_series(
    buy_series: list[float] | None,
    day_index: int,
    *,
    slots_per_hour: int,
    global_step_offset: int,
    offpeak_buy: float,
) -> list[float]:
    """24 hourly buy samples for calendar day_index in the step timeline."""
    slots_per_day = HOURS_PER_DAY * slots_per_hour
    off = float(offpeak_buy)
    out: list[float] = []
    for h in range(HOURS_PER_DAY):
        global_step = day_index * slots_per_day + h * slots_per_hour
        si = global_step - global_step_offset
        if buy_series is None or si < 0 or si >= len(buy_series):
            out.append(off)
        else:
            out.append(float(buy_series[si]))
    return out


def _pv_cover_ends_overnight_need(
    *,
    local_hour: int,
    start_local_hour: int,
    crossed_midnight: bool,
    seen_insufficient: bool,
    cover_bound: int | None,
) -> bool:
    """Whether a PV-cover hour ends the overnight need walk."""
    if seen_insufficient:
        if crossed_midnight:
            return True
        if cover_bound is not None:
            return int(local_hour) < int(cover_bound)
        # All-offpeak day: only a morning-started walk may stop before midnight.
        return (
            int(start_local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
            and int(local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
        )
    # Already self-sufficient — no overnight gap ahead today.
    if cover_bound is not None:
        return (
            int(start_local_hour) < int(cover_bound)
            and int(local_hour) < int(cover_bound)
        )
    return (
        int(start_local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
        and int(local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
    )


def eps_step_kwh(epsilon: float, step_scale: float) -> float:
    """Per-step epsilon floor shared by optimizer and q15 replay callers."""
    return max(float(epsilon) * float(step_scale), MIN_EPS_STEP_KWH)


def slots_per_hour_from_scale(step_scale: float) -> int:
    return max(1, int(round(1.0 / step_scale)) if step_scale > 0 else 1)


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
    """Legacy q15 neighbour gate (kept for unit tests / docs).

    Live export assignment uses ranked hourly average RCE instead
    (see assign_ranked_battery_export).
    """
    if not _rce_at_or_above(rce_series, step, floor, epsilon=epsilon):
        return False
    slots_per_hour = slots_per_hour_from_scale(step_scale)
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


def hourly_avg_rce(
    rce_series: list[float | None],
    hour: int,
    *,
    slots_per_hour: int = 4,
) -> float | None:
    """Mean RCE over the clock hour (None if no priced quarters)."""
    start = int(hour) * int(slots_per_hour)
    vals: list[float] = []
    for i in range(int(slots_per_hour)):
        idx = start + i
        if 0 <= idx < len(rce_series) and rce_series[idx] is not None:
            vals.append(float(rce_series[idx]))
    if not vals:
        return None
    return sum(vals) / len(vals)


def rank_hours_by_avg_rce(
    hours: list[int],
    rce_series: list[float | None],
    floor: float,
    *,
    slots_per_hour: int = 4,
    epsilon: float = 0.0,
) -> list[int]:
    """Hours with avg RCE ≥ floor, richest first (rank 1 = first element)."""
    scored: list[tuple[float, int]] = []
    for h in hours:
        avg = hourly_avg_rce(rce_series, h, slots_per_hour=slots_per_hour)
        if avg is not None and avg + epsilon >= floor:
            scored.append((avg, int(h)))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [h for _, h in scored]


def export_window_roles(selected_hours: set[int] | list[int]) -> dict[int, str]:
    """Classify each selected hour as single|first|middle|last in its run."""
    hours = sorted({int(h) for h in selected_hours})
    roles: dict[int, str] = {}
    if not hours:
        return roles
    run = [hours[0]]
    for h in hours[1:]:
        if h == run[-1] + 1:
            run.append(h)
        else:
            _assign_export_run_roles(run, roles)
            run = [h]
    _assign_export_run_roles(run, roles)
    return roles


def _assign_export_run_roles(run: list[int], roles: dict[int, str]) -> None:
    if len(run) == 1:
        roles[run[0]] = "single"
        return
    roles[run[0]] = "first"
    for h in run[1:-1]:
        roles[h] = "middle"
    roles[run[-1]] = "last"


def export_span_candidates(role: str) -> list[tuple[int, int]]:
    """Allowed (start_q, end_q_exclusive) spans for a window role, longest first.

    Quarters: 0=:00-:15 … 3=:45-:00. end_exclusive=4 means end at next :00.
    """
    if role == "middle":
        return [(0, 4)]
    if role == "first":
        # Start :00/:15/:30; must end at next :00.
        return [(0, 4), (1, 4), (2, 4)]
    if role == "last":
        # Start :00 only; end :00 / :45 / :30.
        return [(0, 4), (0, 3), (0, 2)]
    # single-hour window: start :00/:15/:30, end :30/:45/:00
    cands: list[tuple[int, int, int]] = []
    for start in (0, 1, 2):
        for end in (2, 3, 4):
            if end > start:
                cands.append((start, end, end - start))
    cands.sort(key=lambda t: (-t[2], t[0], t[1]))
    return [(s, e) for s, e, _ in cands]


def _hour_steps_in_horizon(
    *,
    hour: int,
    steps: int,
    rce_step_offset: int,
    slots_per_hour: int,
) -> list[int]:
    out: list[int] = []
    for step in range(steps):
        global_step = rce_step_offset + step
        if global_step // slots_per_hour == hour:
            out.append(step)
    return out


def _simulate_export_selection(
    base_controls: list[HourControl],
    selected: set[int],
    spans: dict[int, tuple[int, int]],
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    rce_step_offset: int,
    step_scale: float,
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    discharge_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    min_hourly_kwh: float,
    hour_avg_rce: dict[int, float] | None = None,
) -> tuple[list[HourControl], dict[int, float]]:
    """Chrono replay: max-power export on selected quarters; return controls + export/hour.

    Higher avg-RCE hours keep SOC priority: earlier cheaper hours leave headroom for
    later richer hours still ahead in the horizon.
    """
    slots = slots_per_hour_from_scale(step_scale)
    avgs = hour_avg_rce or {}
    out: list[HourControl] = []
    export_by_hour: dict[int, float] = {h: 0.0 for h in selected}
    soc = initial_soc_kwh

    def _future_higher_export_soc_need(from_hour: int) -> float:
        """SOC kWh to keep for later selected hours with strictly higher avg RCE."""
        need_ac = 0.0
        cur_avg = avgs.get(from_hour, 0.0)
        for hh in selected:
            if hh <= from_hour:
                continue
            if avgs.get(hh, 0.0) + eps_step < cur_avg:
                continue
            span = spans.get(hh)
            if not span:
                continue
            q_count = max(0, span[1] - span[0])
            need_ac += discharge_ac_step * q_count
        if eta_out <= 0:
            return 0.0
        return need_ac / eta_out

    for step in range(steps):
        base = base_controls[step] if step < len(base_controls) else HourControl(0.0, 0.0)
        global_step = rce_step_offset + step
        hour = global_step // slots
        q = global_step % slots
        export = 0.0
        span = spans.get(hour)
        if (
            span is not None
            and hour in selected
            and span[0] <= q < span[1]
            and base.grid_charge_kw <= eps_step
        ):
            hold = _future_higher_export_soc_need(hour)
            effective_reserve = max(float(reserves[step]), min_kwh) + hold
            export = _max_battery_export_kwh(
                soc, pv_series[step], load_series[step],
                min_kwh=min_kwh,
                ac_cap_kw=discharge_ac_step,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                reserve_soc_kwh=effective_reserve,
                epsilon=eps_step,
            )
            export = min(export, discharge_ac_step)
        ctrl = HourControl(base.grid_charge_kw, export, base.load_from_grid)
        phys = simulate_hour(
            soc, pv_series[step], load_series[step], ctrl,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=discharge_ac_step,
            eta_grid=eta_grid, eta_out=eta_out,
            eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery, epsilon=eps_step,
            reserve_soc_kwh=reserves[step],
        )
        delivered = min(ctrl.battery_export_kwh, phys.grid_export)
        if hour in export_by_hour:
            export_by_hour[hour] += delivered
        out.append(HourControl(base.grid_charge_kw, delivered, base.load_from_grid))
        soc = phys.soc_end

    # Drop hours that could not deliver the configured hourly floor.
    if min_hourly_kwh > eps_step:
        weak = {h for h, e in export_by_hour.items() if e + eps_step < min_hourly_kwh}
        if weak:
            for step in range(steps):
                global_step = rce_step_offset + step
                hour = global_step // slots
                if hour in weak and out[step].battery_export_kwh > eps_step:
                    c = out[step]
                    out[step] = HourControl(c.grid_charge_kw, 0.0, c.load_from_grid)
            for h in weak:
                export_by_hour[h] = 0.0
    return out, export_by_hour


def _pick_spans_for_selection(
    selected: set[int],
    *,
    roles: dict[int, str],
    base_controls: list[HourControl],
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    rce_step_offset: int,
    step_scale: float,
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    discharge_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    min_hourly_kwh: float,
    hour_avg_rce: dict[int, float],
) -> dict[int, tuple[int, int]]:
    """Longest legal span per hour that still yields ≥ min_hourly when possible."""
    spans: dict[int, tuple[int, int]] = {}
    for h in sorted(selected):
        role = roles.get(h, "single")
        chosen: tuple[int, int] | None = None
        for cand in export_span_candidates(role):
            trial = dict(spans)
            trial[h] = cand
            _, by_hour = _simulate_export_selection(
                base_controls, selected, trial,
                steps=steps, pv_series=pv_series, load_series=load_series,
                rce_step_offset=rce_step_offset, step_scale=step_scale,
                initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
                min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
                eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
                eps_step=eps_step, reserves=reserves, min_hourly_kwh=0.0,
                hour_avg_rce=hour_avg_rce,
            )
            got = by_hour.get(h, 0.0)
            if got > eps_step and (min_hourly_kwh <= eps_step or got + eps_step >= min_hourly_kwh):
                chosen = cand
                break
            if chosen is None and got > eps_step:
                chosen = cand
        if chosen is None:
            chosen = export_span_candidates(role)[0]
        spans[h] = chosen
    return spans


def assign_ranked_battery_export(
    base_controls: list[HourControl],
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    rce_series: list[float | None],
    rce_step_offset: int,
    step_scale: float,
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    discharge_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    export_floor: float,
    min_hourly_kwh: float,
) -> list[HourControl]:
    """Fill export by hourly avg-RCE rank at max power; shape quarters by window role.

    DP *base_controls* keep charge/idle; this overlays battery→grid export only.
    """
    if steps <= 0:
        return list(base_controls)
    slots = slots_per_hour_from_scale(step_scale)
    hours = sorted({
        (rce_step_offset + i) // slots for i in range(steps)
    })
    hour_avg = {
        h: avg for h in hours
        if (avg := hourly_avg_rce(rce_series, h, slots_per_hour=slots)) is not None
    }
    ranked = rank_hours_by_avg_rce(
        hours, rce_series, export_floor,
        slots_per_hour=slots, epsilon=eps_step,
    )

    selected: set[int] = set()
    for h in ranked:
        trial = selected | {h}
        roles = export_window_roles(trial)
        # Tentative full-length spans for feasibility.
        trial_spans = {
            hh: export_span_candidates(roles[hh])[0] for hh in trial
        }
        _, by_hour = _simulate_export_selection(
            base_controls, trial, trial_spans,
            steps=steps, pv_series=pv_series, load_series=load_series,
            rce_step_offset=rce_step_offset, step_scale=step_scale,
            initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
            min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
            eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
            eps_step=eps_step, reserves=reserves, min_hourly_kwh=0.0,
            hour_avg_rce=hour_avg,
        )
        if by_hour.get(h, 0.0) > eps_step:
            if min_hourly_kwh > eps_step and by_hour.get(h, 0.0) + eps_step < min_hourly_kwh:
                # Try shorter legal spans before rejecting the hour.
                ok = False
                for cand in export_span_candidates(roles[h]):
                    trial_spans[h] = cand
                    _, by2 = _simulate_export_selection(
                        base_controls, trial, trial_spans,
                        steps=steps, pv_series=pv_series, load_series=load_series,
                        rce_step_offset=rce_step_offset, step_scale=step_scale,
                        initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
                        min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
                        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
                        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
                        eps_step=eps_step, reserves=reserves, min_hourly_kwh=0.0,
                        hour_avg_rce=hour_avg,
                    )
                    if by2.get(h, 0.0) + eps_step >= min_hourly_kwh:
                        ok = True
                        break
                if not ok:
                    continue
            selected = trial

    if not selected:
        return [
            HourControl(c.grid_charge_kw, 0.0, c.load_from_grid) for c in base_controls
        ]

    roles = export_window_roles(selected)
    spans = _pick_spans_for_selection(
        selected, roles=roles, base_controls=base_controls,
        steps=steps, pv_series=pv_series, load_series=load_series,
        rce_step_offset=rce_step_offset, step_scale=step_scale,
        initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
        min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
        eps_step=eps_step, reserves=reserves, min_hourly_kwh=min_hourly_kwh,
        hour_avg_rce=hour_avg,
    )
    controls, export_by_hour = _simulate_export_selection(
        base_controls, selected, spans,
        steps=steps, pv_series=pv_series, load_series=load_series,
        rce_step_offset=rce_step_offset, step_scale=step_scale,
        initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
        min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
        eps_step=eps_step, reserves=reserves, min_hourly_kwh=min_hourly_kwh,
        hour_avg_rce=hour_avg,
    )

    # Middle hours must be a full :00–:00 block; drop partial deliveries.
    middle_hours = {h for h, role in roles.items() if role == "middle"}
    if middle_hours:
        bad_middle: set[int] = set()
        for h in middle_hours:
            idxs = _hour_steps_in_horizon(
                hour=h, steps=steps, rce_step_offset=rce_step_offset,
                slots_per_hour=slots,
            )
            if len(idxs) < slots:
                continue
            if any(controls[i].battery_export_kwh <= eps_step for i in idxs):
                bad_middle.add(h)
        if bad_middle:
            selected -= bad_middle
            if not selected:
                return [
                    HourControl(c.grid_charge_kw, 0.0, c.load_from_grid)
                    for c in base_controls
                ]
            roles = export_window_roles(selected)
            spans = _pick_spans_for_selection(
                selected, roles=roles, base_controls=base_controls,
                steps=steps, pv_series=pv_series, load_series=load_series,
                rce_step_offset=rce_step_offset, step_scale=step_scale,
                initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
                min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
                eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
                eps_step=eps_step, reserves=reserves, min_hourly_kwh=min_hourly_kwh,
                hour_avg_rce=hour_avg,
            )
            controls, _ = _simulate_export_selection(
                base_controls, selected, spans,
                steps=steps, pv_series=pv_series, load_series=load_series,
                rce_step_offset=rce_step_offset, step_scale=step_scale,
                initial_soc_kwh=initial_soc_kwh, battery_cap=battery_cap,
                min_kwh=min_kwh, discharge_ac_step=discharge_ac_step,
                eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
                eps_step=eps_step, reserves=reserves, min_hourly_kwh=min_hourly_kwh,
                hour_avg_rce=hour_avg,
            )
    return controls


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
    # Grid charge energy this step (kWh AC on the meter). DC into battery = AC × eta_grid.
    grid_charge_kw: float
    battery_export_kwh: float
    load_from_grid: bool = False


@dataclass
class HourPhysics:
    soc_end: float
    battery_delta: float
    grid_import: float
    grid_export: float


def _apply_grid_charge_ac(
    *,
    soc: float,
    battery_delta: float,
    grid_import: float,
    ac_charge_kwh: float,
    battery_cap: float,
    eta_grid: float,
    epsilon: float,
) -> tuple[float, float, float]:
    """Draw ac_charge_kwh from grid; store AC × eta into battery."""
    head_room = max(0.0, battery_cap - soc)
    if ac_charge_kwh <= epsilon or head_room <= epsilon:
        return soc, battery_delta, grid_import
    max_ac = head_room / eta_grid if eta_grid > 0 else head_room
    ac = min(ac_charge_kwh, max_ac)
    stored = ac * eta_grid if eta_grid > 0 else ac
    return soc + stored, battery_delta + stored, grid_import + ac


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
    eta_pv_battery: float,
    epsilon: float,
    reserve_soc_kwh: float | None = None,
) -> HourPhysics:
    """One step: PV (DC) vs load (AC); PV→battery applies eta_pv_battery.

    grid_charge_kw is AC kWh from the meter this step (Chg 6kW × 1h → 6 kWh import).
    DC stored = AC × eta_grid. Charge is applied before house load when load stays on
    the battery, so a Chg hour at min SOC nets charge − house on the battery.
    """
    reserve = min_kwh if reserve_soc_kwh is None else max(min_kwh, reserve_soc_kwh)
    soc = soc_kwh
    grid_import = 0.0
    grid_export = 0.0
    battery_delta = 0.0

    deficit, pv_surplus = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    # Load from grid only when explicitly requested. Grid charge does not force
    # house load onto the meter — load priority stays on the battery (SOC > min).
    load_on_grid = control.load_from_grid
    # Charge-before-load when house is on battery: same-hour Chg can supply load.
    charge_before_load = (
        control.grid_charge_kw > epsilon and not load_on_grid
    )

    export_headroom = max(0.0, ac_cap_kw - load)

    head_room = max(0.0, battery_cap - soc)
    if pv_surplus > epsilon:
        if head_room > epsilon and eta_pv_battery > 0:
            taken = min(pv_surplus, head_room / eta_pv_battery)
            stored = taken * eta_pv_battery
            soc += stored
            battery_delta += stored
            pv_surplus -= taken
        if pv_surplus > epsilon:
            pv_exp = min(
                pv_surplus * eta_pv_grid,
                max(0.0, export_headroom - grid_export),
            )
            grid_export += pv_exp

    if charge_before_load:
        soc, battery_delta, grid_import = _apply_grid_charge_ac(
            soc=soc,
            battery_delta=battery_delta,
            grid_import=grid_import,
            ac_charge_kwh=control.grid_charge_kw,
            battery_cap=battery_cap,
            eta_grid=eta_grid,
            epsilon=epsilon,
        )

    available = max(0.0, soc - min_kwh)
    if deficit > epsilon:
        if load_on_grid:
            grid_import += deficit
        else:
            supplied = min(deficit, available * eta_out)
            withdraw_load = supplied / eta_out if eta_out > 0 else 0.0
            soc -= withdraw_load
            battery_delta -= withdraw_load
            available = max(0.0, soc - min_kwh)
            if deficit > supplied + epsilon:
                grid_import += deficit - supplied

    batt_export = min(max(0.0, control.battery_export_kwh), export_headroom)
    available_export = max(0.0, soc - reserve)
    if batt_export > epsilon and available_export > epsilon and eta_out > 0:
        export_withdraw = min(batt_export / eta_out, available_export)
        soc -= export_withdraw
        batt_export = export_withdraw * eta_out
        grid_export += batt_export
        battery_delta -= export_withdraw

    if not charge_before_load:
        soc, battery_delta, grid_import = _apply_grid_charge_ac(
            soc=soc,
            battery_delta=battery_delta,
            grid_import=grid_import,
            ac_charge_kwh=control.grid_charge_kw,
            battery_cap=battery_cap,
            eta_grid=eta_grid,
            epsilon=epsilon,
        )

    # Cap at full only — never raise SOC to min_kwh (that invents energy).
    soc = min(battery_cap, max(0.0, soc))
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
    buy_series: list[float] | None = None,
    offpeak_buy: float | None = None,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """Battery kWh to keep for self-use until morning PV covers house.

    Walk ends when generation covers load within that calendar day's real
    tariff morning horizon (from buy prices; weekends have no morning peak).
    Does **not** by itself justify grid→battery charging — see
    `_grid_charge_target_soc_kwh_from_step`.
    """
    return _forward_soc_need_from_step(
        step, pv_series, load_series, reserve_floor_kwh, eta_out, eta_pv_load, epsilon,
        buy_series=buy_series, offpeak_buy=offpeak_buy, peak_deficits_only=False,
        slots_per_hour=slots_per_hour, global_step_offset=global_step_offset,
    )


def _grid_charge_target_soc_kwh_from_step(
    step: int,
    pv_series: list[float],
    load_series: list[float],
    buy_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    offpeak_buy: float,
    *,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """SOC worth buying from the grid into the battery.

    Only future *peak*-priced house deficits count (e.g. weekday morning until
    PV covers). Overnight/offpeak deficits do not — buying offpeak into the
    battery to later serve offpeak load loses round-trip energy; better to
    import for the house when SOC hits min (especially all-offpeak weekends).
    """
    return _forward_soc_need_from_step(
        step, pv_series, load_series, reserve_floor_kwh, eta_out, eta_pv_load, epsilon,
        buy_series=buy_series, offpeak_buy=offpeak_buy, peak_deficits_only=True,
        slots_per_hour=slots_per_hour, global_step_offset=global_step_offset,
    )


def _forward_soc_need_from_step(
    step: int,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    *,
    buy_series: list[float] | None,
    offpeak_buy: float | None,
    peak_deficits_only: bool,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """Walk forward until PV covers load in that day's tariff morning horizon.

    Cover bound comes from each calendar day's buy prices (morning peak end,
    or evening-peak start, or all-offpeak weekend). Afternoon PV cover must
    not end the walk before tonight's deficits. With peak_deficits_only, only
    peak-priced deficits are summed.
    """
    need = 0.0
    j = step + 1
    slots_per_hour = max(1, slots_per_hour)
    slots_per_day = HOURS_PER_DAY * slots_per_hour
    start_day = (global_step_offset + step) // slots_per_day
    start_local_hour = ((global_step_offset + step) % slots_per_day) // slots_per_hour
    off = float(offpeak_buy or 0.0)
    bound_cache: dict[int, int | None] = {}
    seen_insufficient_hour = False

    def cover_bound_for_day(day_index: int) -> int | None:
        if day_index not in bound_cache:
            hour_buys = _day_hour_buys_from_series(
                buy_series, day_index,
                slots_per_hour=slots_per_hour,
                global_step_offset=global_step_offset,
                offpeak_buy=off,
            )
            bound_cache[day_index] = morning_cover_bound_from_hour_buys(
                hour_buys, offpeak_buy=off, epsilon=epsilon,
            )
        return bound_cache[day_index]

    while j < len(pv_series):
        deficit, _ = pv_load_energy_split(
            pv_series[j], load_series[j], eta_pv_load=eta_pv_load,
        )
        if deficit > epsilon:
            count = True
            if peak_deficits_only:
                buy_p = float(buy_series[j]) if buy_series is not None and j < len(buy_series) else 0.0
                count = buy_p > off + epsilon
            if count:
                need += deficit / eta_out if eta_out > 0 else deficit
            seen_insufficient_hour = True
        j += 1
        if j % slots_per_hour == 0:
            h_start = j - slots_per_hour
            pv_h = sum(pv_series[h_start:j])
            load_h = sum(load_series[h_start:j])
            if pv_h * eta_pv_load < load_h - epsilon:
                continue
            hour_day = (global_step_offset + j - 1) // slots_per_day
            local_hour = ((global_step_offset + j - 1) % slots_per_day) // slots_per_hour
            crossed_midnight = hour_day > start_day
            if _pv_cover_ends_overnight_need(
                local_hour=local_hour,
                start_local_hour=start_local_hour,
                crossed_midnight=crossed_midnight,
                seen_insufficient=seen_insufficient_hour,
                cover_bound=cover_bound_for_day(hour_day),
            ):
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


def _grid_charge_ac_kw(
    soc_kwh: float,
    *,
    buy_p: float,
    offpeak_buy: float,
    charge_target_soc_kwh: float,
    head_room_kwh: float,
    charge_ac_cap_kw: float,
    eta_grid: float,
    epsilon: float,
) -> float:
    """Single grid→battery decision: offpeak + below peak-cover target + headroom.

    Returns AC kWh to charge this step (0 if not allowed). Continuous fill is
    expressed by callers taking only this action when the returned rate > 0.
    """
    if buy_p > offpeak_buy + epsilon:
        return 0.0
    if charge_target_soc_kwh <= soc_kwh + epsilon:
        return 0.0
    if head_room_kwh <= epsilon:
        return 0.0
    max_ac = head_room_kwh / eta_grid if eta_grid > 0 else head_room_kwh
    return min(charge_ac_cap_kw, max_ac)


def _control_options(
    soc_kwh: float,
    pv: float,
    load: float,
    *,
    battery_cap: float,
    min_kwh: float,
    discharge_ac_cap_kw: float,
    charge_ac_cap_kw: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    buy_p: float,
    offpeak_buy: float,
    reserve_soc_kwh: float,
    charge_target_soc_kwh: float,
    allow_battery_export: bool,
) -> list[HourControl]:
    """Build DP actions for one step.

    - reserve_soc_kwh: export floor (self-use through overnight / morning).
    - charge_target_soc_kwh: peak-priced cover only (grid charge justification).
    When charge is allowed, options collapse to charge-only (continuous offpeak fill).
    """
    head_room = battery_cap - soc_kwh
    charge_rate = _grid_charge_ac_kw(
        soc_kwh,
        buy_p=buy_p,
        offpeak_buy=offpeak_buy,
        charge_target_soc_kwh=charge_target_soc_kwh,
        head_room_kwh=head_room,
        charge_ac_cap_kw=charge_ac_cap_kw,
        eta_grid=eta_grid,
        epsilon=epsilon,
    )
    if charge_rate > epsilon:
        return [HourControl(charge_rate, 0.0)]

    opts = [HourControl(0.0, 0.0)]

    max_batt_export = _max_battery_export_kwh(
        soc_kwh, pv, load,
        min_kwh=min_kwh, ac_cap_kw=discharge_ac_cap_kw,
        eta_out=eta_out, eta_pv_load=eta_pv_load,
        reserve_soc_kwh=reserve_soc_kwh,
        epsilon=epsilon,
    )
    min_viable = max(epsilon, discharge_ac_cap_kw * EXPORT_MIN_FRAC)
    if max_batt_export >= min_viable and allow_battery_export:
        for frac in EXPORT_POWER_FRACS:
            tier_cap = discharge_ac_cap_kw * frac
            tier_export = min(max_batt_export, tier_cap)
            if tier_export >= min_viable:
                opts.append(HourControl(0.0, tier_export))

    seen: set[tuple[float, float]] = set()
    out: list[HourControl] = []
    for o in opts:
        key = (
            round(o.grid_charge_kw, CONTROL_DEDUP_DECIMALS),
            round(o.battery_export_kwh, CONTROL_DEDUP_DECIMALS),
        )
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def _correct_min_hourly_transfer_controls(
    controls: list[HourControl],
    *,
    rce_step_offset: int,
    step_scale: float,
    min_hourly_kwh: float,
    epsilon: float,
) -> list[HourControl]:
    """Per clock hour: enforce min_hourly_transfer_kwh on battery↔grid flows.

    Export below the floor is cleared (avoid tiny Dis blocks).
    Charge below the floor is scaled up to the floor so a real overnight
    reserve top-up is not deleted (that re-opens a morning SOC gap).
    """
    if min_hourly_kwh <= epsilon or not controls:
        return controls
    slots_per_hour = slots_per_hour_from_scale(step_scale)
    out = list(controls)
    by_hour: dict[int, list[int]] = {}
    for i in range(len(out)):
        hour = (rce_step_offset + i) // slots_per_hour
        by_hour.setdefault(hour, []).append(i)

    for idxs in by_hour.values():
        export_h = sum(out[i].battery_export_kwh for i in idxs)
        charge_h = sum(out[i].grid_charge_kw for i in idxs)
        if epsilon < export_h < min_hourly_kwh:
            for i in idxs:
                c = out[i]
                out[i] = HourControl(c.grid_charge_kw, 0.0, c.load_from_grid)
        if epsilon < charge_h < min_hourly_kwh:
            scale = min_hourly_kwh / charge_h
            for i in idxs:
                c = out[i]
                if c.grid_charge_kw > epsilon:
                    out[i] = HourControl(
                        c.grid_charge_kw * scale,
                        c.battery_export_kwh,
                        c.load_from_grid,
                    )
    return out


def _should_extend_reserve_horizon(
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
) -> bool:
    """True when same-calendar-day q15 horizon should append tomorrow for reserve."""
    forecast_data = forecast or {}
    return (
        step_scale < 1.0
        and end_dt.date() == today_date
        and bool((forecast_data.get("tomorrow") or {}).get("pv"))
        and bool((forecast_data.get("tomorrow") or {}).get("load"))
    )


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
    if not _should_extend_reserve_horizon(
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    ):
        return pv_series, load_series
    rep = slots_per_hour_from_scale(step_scale)
    pv_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["pv"] or [])][:HOURS_PER_DAY]
    load_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["load"] or [])][:HOURS_PER_DAY]
    pv_ext = list(pv_series)
    load_ext = list(load_series)
    for h in range(HOURS_PER_DAY):
        pv_h = pv_tomorrow[h] if h < len(pv_tomorrow) else 0.0
        load_h = load_tomorrow[h] if h < len(load_tomorrow) else 0.0
        pv_ext.extend([pv_h * step_scale] * rep)
        load_ext.extend([load_h * step_scale] * rep)
    return pv_ext, load_ext


def build_extended_buy_for_reserve(
    buy_series: list[float],
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
    cfg: dict,
) -> list[float]:
    """Extend buy prices into tomorrow in lockstep with extended reserve PV/load."""
    forecast_data = forecast or {
        "today": {"pv": [], "load": []},
        "tomorrow": {"pv": [], "load": []},
    }
    buy_for_reserve = list(buy_series)
    if not _should_extend_reserve_horizon(
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    ):
        return buy_for_reserve
    rep = slots_per_hour_from_scale(step_scale)
    tomorrow = today_date + timedelta(days=1)
    base = datetime(tomorrow.year, tomorrow.month, tomorrow.day)
    for h in range(HOURS_PER_DAY):
        price, _ = get_buy_price(base.replace(hour=h), cfg)
        buy_for_reserve.extend([float(price)] * rep)
    return buy_for_reserve


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
    global_step_offset: int = 0,
    buy_prices: list[float] | None = None,
    cfg: dict | None = None,
    offpeak_buy: float | None = None,
) -> list[float]:
    """Reserve floor (kWh) after each step — through midnight until next-day PV."""
    end = end_dt or datetime.now()
    pv_r, load_r = build_extended_pv_load_for_reserve(
        pv_series, load_series,
        step_scale=step_scale, end_dt=end, today_date=today_date, forecast=forecast,
    )
    buy_r = list(buy_prices) if buy_prices is not None else []
    if cfg is not None and buy_prices is not None:
        buy_r = build_extended_buy_for_reserve(
            buy_prices,
            step_scale=step_scale, end_dt=end, today_date=today_date,
            forecast=forecast, cfg=cfg,
        )
    off = float(offpeak_buy) if offpeak_buy is not None else (
        float(cfg["grid"]["g12"]["offpeak_price_pln_kwh"]) if cfg is not None else 0.0
    )
    eps_step = eps_step_kwh(epsilon, step_scale)
    slots_per_hour = slots_per_hour_from_scale(step_scale)
    return [
        _reserve_soc_kwh_from_step(
            s, pv_r, load_r, reserve_floor_kwh, eta_out, eta_pv_load, eps_step,
            buy_series=buy_r if buy_r else None,
            offpeak_buy=off,
            slots_per_hour=slots_per_hour,
            global_step_offset=global_step_offset,
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
    slots_per_hour = slots_per_hour_from_scale(step_scale)
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
    # Timer/optimizer charge cap as AC on the meter (DC into bat = AC × eta).
    charge_ac_kw = plan_timer_charge_grid_kw(cfg)
    min_hourly_transfer = plan_timer_min_hourly_transfer_kwh(cfg)
    epsilon = float(params["epsilon_kwh"])
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])
    tariff = g12_tariff_from_cfg(cfg)
    discharge_ac_step = discharge_ac_kw * step_scale
    charge_ac_step = charge_ac_kw * step_scale
    eps_step = eps_step_kwh(epsilon, step_scale)

    bin_kwh = max(DP_SOC_BIN_KWH, battery_cap / max(1, int((battery_cap - min_kwh) / DP_SOC_BIN_KWH)))
    max_bin = int(math.ceil((battery_cap - min_kwh) / bin_kwh))
    inf = DP_COST_INF

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
    buy_for_reserve = build_extended_buy_for_reserve(
        buy_prices,
        step_scale=step_scale, end_dt=end_dt, today_date=today_date,
        forecast=forecast_data, cfg=cfg,
    )
    slots_per_hour = slots_per_hour_from_scale(step_scale)

    reserves = [
        _reserve_soc_kwh_from_step(
            s, pv_for_reserve, load_for_reserve, reserve_floor_kwh,
            eta_out, eta_pv_load, eps_step,
            buy_series=buy_for_reserve,
            offpeak_buy=tariff.offpeak_full,
            slots_per_hour=slots_per_hour,
            global_step_offset=rce_step_offset,
        )
        for s in range(steps)
    ]
    offpeak_buy = tariff.offpeak_full
    charge_targets = [
        _grid_charge_target_soc_kwh_from_step(
            s, pv_for_reserve, load_for_reserve, buy_for_reserve,
            reserve_floor_kwh, eta_out, eta_pv_load, eps_step, offpeak_buy,
            slots_per_hour=slots_per_hour,
            global_step_offset=rce_step_offset,
        )
        for s in range(steps)
    ]

    export_floor = grid_export_threshold_pln_kwh(cfg)

    for step in range(steps):
        pv = pv_series[step]
        load = load_series[step]
        buy_p = buy_prices[step]
        rce_idx = rce_step_offset + step
        rce = rce_series[rce_idx] if rce_idx < len(rce_series) else None
        # Charge/idle only in DP; battery export is assigned by hourly RCE rank after.
        allow_battery_export = False
        # Same peak/offpeak split as charge_target (two discrete G12 buy rates).
        g12_zone = "peak" if buy_p > offpeak_buy + eps_step else "offpeak"

        for soc_bin, cost_in in list(dp[step].items()):
            soc = soc_at[step].get(
                soc_bin,
                min(battery_cap, _soc_from_bin(soc_bin, min_kwh, bin_kwh)),
            )
            reserve = reserves[step]
            charge_target = charge_targets[step]
            for ctrl in _control_options(
                soc, pv, load,
                battery_cap=battery_cap, min_kwh=min_kwh,
                discharge_ac_cap_kw=discharge_ac_step,
                charge_ac_cap_kw=charge_ac_step,
                eta_grid=eta_grid,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                epsilon=eps_step, buy_p=buy_p, offpeak_buy=offpeak_buy,
                reserve_soc_kwh=reserve,
                charge_target_soc_kwh=charge_target,
                allow_battery_export=allow_battery_export,
            ):
                phys = simulate_hour(
                    soc, pv, load, ctrl,
                    battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=discharge_ac_step,
                    eta_grid=eta_grid, eta_out=eta_out,
                    eta_pv_load=eta_pv_load,
                    eta_pv_grid=eta_pv_grid,
                    eta_pv_battery=eta_pv_battery,
                    epsilon=eps_step,
                    reserve_soc_kwh=reserve,
                )
                step_cost = hour_grid_cash_pln(
                    phys.grid_import, phys.grid_export, buy_p, rce, cfg,
                    battery_export=min(ctrl.battery_export_kwh, phys.grid_export),
                    g12_zone=g12_zone,
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
            eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
            epsilon=epsilon,
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

    controls = assign_ranked_battery_export(
        controls,
        steps=steps,
        pv_series=pv_series,
        load_series=load_series,
        rce_series=rce_series,
        rce_step_offset=rce_step_offset,
        step_scale=step_scale,
        initial_soc_kwh=initial_soc_kwh,
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        discharge_ac_step=discharge_ac_step,
        eta_grid=eta_grid,
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid,
        eta_pv_battery=eta_pv_battery,
        eps_step=eps_step,
        reserves=reserves,
        export_floor=export_floor,
        min_hourly_kwh=min_hourly_transfer,
    )
    # Sub-pass per clock hour: sum battery↔grid flows across 15-min slots; zero if below floor.
    return _correct_min_hourly_transfer_controls(
        controls,
        rce_step_offset=rce_step_offset,
        step_scale=step_scale,
        min_hourly_kwh=min_hourly_transfer,
        epsilon=eps_step,
    )
