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
    plan_timer_discharge_power_kw,
    plan_timer_min_block_minutes,
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
    """Whether a PV-cover hour ends the overnight need walk.

    Evening-started walks (from noon onward) must reach the *next* calendar
    day's first PV-cover hour — same-day afternoon/evening sun must not end
    the survive-until-morning reserve.
    """
    started_evening = int(start_local_hour) >= _ALL_OFFPEAK_COVER_HOUR_END
    if seen_insufficient:
        if crossed_midnight:
            return True
        # Still today: evening-started walks keep going through tonight.
        if started_evening:
            return False
        if cover_bound is not None:
            return int(local_hour) < int(cover_bound)
        # All-offpeak day: only a morning-started walk may stop before midnight.
        return int(local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
    # Already self-sufficient — no overnight gap ahead today.
    if started_evening:
        return False
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
    """Q15 neighbour RCE gate used by unit tests.

    Production export assignment uses ranked hourly average RCE
    (``plan_battery_grid_export``).
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


def hour_rce_rating(
    rce_series: list[float | None],
    hour: int,
    *,
    slots_per_hour: int = 4,
) -> float | None:
    """Hour rating for export ranking: avg RCE rounded to 0.01 (same avg → same rating)."""
    avg = hourly_avg_rce(rce_series, hour, slots_per_hour=slots_per_hour)
    if avg is None:
        return None
    return round(float(avg), 2)


def rank_hours_by_avg_rce(
    hours: list[int],
    rce_series: list[float | None],
    floor: float,
    *,
    slots_per_hour: int = 4,
    epsilon: float = 0.0,
) -> list[int]:
    """Hours with rating ≥ floor, richest first (ties: earlier hour).

    Rating is avg RCE rounded to hundredths. Prefer
    ``pick_next_export_hour`` during allocation so equal ratings prefer
    proximity to the last successfully assigned hour.
    """
    scored: list[tuple[float, int]] = []
    for h in hours:
        rating = hour_rce_rating(rce_series, h, slots_per_hour=slots_per_hour)
        if rating is not None and rating + epsilon >= floor:
            scored.append((rating, int(h)))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [h for _, h in scored]


def pick_next_export_hour(
    remaining: list[int],
    ratings: dict[int, float],
    *,
    last_hour: int | None,
) -> int:
    """Next hour to try: highest rating; ties → closest to *last_hour* (else earliest)."""
    if not remaining:
        raise ValueError("remaining hours empty")
    best = max(float(ratings[h]) for h in remaining)
    tied = [h for h in remaining if float(ratings[h]) == best]
    if last_hour is None:
        return min(tied)
    return min(tied, key=lambda h: (abs(int(h) - int(last_hour)), int(h)))


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


@dataclass(frozen=True)
class _BatteryGridExportHourClaim:
    """Export assignment for one clock hour (rank-order greedy)."""

    hour: int
    span: tuple[int, int]
    export_q: tuple[float, float, float, float]
    bat_discharge_kwh: float

    @property
    def export_ac_kwh(self) -> float:
        return float(sum(self.export_q))


def _hold_soc_for_later_battery_grid_export_claims(
    claims: dict[int, _BatteryGridExportHourClaim],
    *,
    from_hour: int,
    eta_out: float,
) -> float:
    """DC kWh to reserve for already-assigned higher-rank hours after *from_hour*."""
    need_ac = 0.0
    for h, claim in claims.items():
        if int(h) <= int(from_hour):
            continue
        need_ac += claim.export_ac_kwh
    if eta_out <= 0:
        return need_ac
    return need_ac / eta_out


def _apply_battery_grid_export_claims_chrono(
    base_controls: list[HourControl],
    claims: dict[int, _BatteryGridExportHourClaim],
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    rce_step_offset: int,
    step_scale: float,
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
) -> tuple[list[HourControl], list[float]]:
    """Chrono replay of DP base + claims. Returns controls and soc_at_step_start."""
    slots = slots_per_hour_from_scale(step_scale)
    out: list[HourControl] = []
    soc_starts: list[float] = []
    soc = initial_soc_kwh
    for step in range(steps):
        soc_starts.append(soc)
        base = base_controls[step] if step < len(base_controls) else HourControl(0.0, 0.0)
        global_step = rce_step_offset + step
        hour = global_step // slots
        q = global_step % slots
        export = 0.0
        claim = claims.get(hour)
        if (
            claim is not None
            and claim.span[0] <= q < claim.span[1]
            and base.grid_charge_kw <= eps_step
        ):
            export = float(claim.export_q[q])
        reserve_soc = float(reserves[step])
        if claim is not None and claim.span[0] <= q < claim.span[1] and claim.span[1] >= 4:
            next_idxs = _hour_steps_in_horizon(
                hour=hour + 1,
                steps=steps,
                rce_step_offset=rce_step_offset,
                slots_per_hour=slots,
            )
            if next_idxs:
                reserve_soc = min(reserve_soc, float(reserves[next_idxs[0]]))
        ctrl = HourControl(base.grid_charge_kw, export, base.load_from_grid)
        phys = simulate_hour(
            soc, pv_series[step], load_series[step], ctrl,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=discharge_dc_step,
            eta_grid=eta_grid, eta_out=eta_out,
            eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery, epsilon=eps_step,
            reserve_soc_kwh=reserve_soc,
        )
        delivered = min(ctrl.battery_export_kwh, phys.grid_export)
        out.append(HourControl(base.grid_charge_kw, delivered, base.load_from_grid))
        soc = phys.soc_end
    soc_starts.append(soc)
    return out, soc_starts


def _sim_hour_battery_grid_export_at_cap(
    *,
    soc0: float,
    pv_q: list[float],
    load_q: list[float],
    reserve_q: list[float],
    base_charge_q: list[float],
    span: tuple[int, int],
    dc_cap_per_q: float,
    hold_soc_kwh: float,
    hour_end_floor_kwh: float | None,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
) -> tuple[list[float], float]:
    """One clock hour at a constant DC discharge budget per active quarter.

    Returns (export_q[4], bat_discharge_kwh in the export *span* only).

    Floor checks must not credit load-only quarters outside the Dis window —
    otherwise a 0.5 kWh orphan export passes min_hourly via overnight house load.
    """
    exports = [0.0, 0.0, 0.0, 0.0]
    soc = soc0
    bat_dis = 0.0
    for q in range(4):
        charge = float(base_charge_q[q]) if q < len(base_charge_q) else 0.0
        export = 0.0
        in_span = span[0] <= q < span[1]
        step_dc = dc_cap_per_q if in_span else discharge_dc_step
        reserve_floor = float(reserve_q[q])
        if in_span and charge <= eps_step:
            if span[1] >= 4 and hour_end_floor_kwh is not None:
                reserve_floor = min(reserve_floor, float(hour_end_floor_kwh))
            effective_reserve = max(reserve_floor, min_kwh) + hold_soc_kwh
            export = _max_battery_export_kwh(
                soc, pv_q[q], load_q[q],
                min_kwh=min_kwh,
                ac_cap_kw=inverter_ac_step,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                reserve_soc_kwh=effective_reserve,
                epsilon=eps_step,
                discharge_dc_cap_kwh=step_dc,
            )
        ctrl = HourControl(charge, export, False)
        phys = simulate_hour(
            soc, pv_q[q], load_q[q], ctrl,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=step_dc,
            eta_grid=eta_grid, eta_out=eta_out,
            eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery, epsilon=eps_step,
            reserve_soc_kwh=max(reserve_floor, min_kwh),
        )
        delivered = min(export, phys.grid_export)
        exports[q] = delivered
        if in_span:
            bat_dis += max(0.0, -phys.battery_delta)
        soc = phys.soc_end
    return exports, bat_dis


def _trim_span_to_active_battery_grid_exports(
    role: str,
    span: tuple[int, int],
    exports: list[float],
    *,
    eps: float,
) -> tuple[int, int] | None:
    """Shrink *span* to contiguous active export quarters still legal for *role*."""
    active = [q for q in range(span[0], span[1]) if exports[q] > eps]
    if not active:
        return None
    lo, hi = active[0], active[-1] + 1
    if active != list(range(lo, hi)):
        return None
    trimmed = (lo, hi)
    if trimmed not in export_span_candidates(role):
        return None
    return trimmed


def _plan_hour_battery_grid_export_claim(
    *,
    hour: int,
    role: str,
    soc0: float,
    hold_soc_kwh: float,
    pv_q: list[float],
    load_q: list[float],
    reserve_q: list[float],
    base_charge_q: list[float],
    hour_end_floor_kwh: float | None = None,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    min_hourly_kwh: float,
) -> _BatteryGridExportHourClaim | None:
    """Pick span + per-quarter export for one hour from remaining SOC budget.

    Prefers max DC power; if SOC cannot fill the span, tries a lower uniform
    power so Bat Discharge still meets *min_hourly_kwh*. Any window role may
    use reduced power (not only the last hour).
    """
    legal = set(export_span_candidates(role))
    best: _BatteryGridExportHourClaim | None = None
    best_key: tuple[float, int] = (-1.0, -1)
    common = dict(
        soc0=soc0, pv_q=pv_q, load_q=load_q, reserve_q=reserve_q,
        base_charge_q=base_charge_q, hold_soc_kwh=hold_soc_kwh,
        hour_end_floor_kwh=hour_end_floor_kwh,
        battery_cap=battery_cap, min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step, inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery, eps_step=eps_step,
    )

    for span in export_span_candidates(role):
        # 1) Max power, then trim trailing empty quarters to a legal sub-span.
        exports, bat_dis = _sim_hour_battery_grid_export_at_cap(
            span=span, dc_cap_per_q=discharge_dc_step, **common,
        )
        trimmed = _trim_span_to_active_battery_grid_exports(role, span, exports, eps=eps_step)
        if trimmed is not None and trimmed in legal:
            exp_trim = [
                exports[q] if trimmed[0] <= q < trimmed[1] else 0.0 for q in range(4)
            ]
            ok_floor = min_hourly_kwh <= eps_step or bat_dis + eps_step >= min_hourly_kwh
            if trimmed != span:
                exports2, bat_dis2 = _sim_hour_battery_grid_export_at_cap(
                    span=trimmed, dc_cap_per_q=discharge_dc_step, **common,
                )
                exp_trim = exports2
                bat_dis = bat_dis2
                ok_floor = (
                    min_hourly_kwh <= eps_step or bat_dis + eps_step >= min_hourly_kwh
                )
            if sum(exp_trim) > eps_step and ok_floor:
                key = (sum(exp_trim), trimmed[1] - trimmed[0])
                if key > best_key:
                    best_key = key
                    best = _BatteryGridExportHourClaim(
                        hour=hour, span=trimmed,
                        export_q=(exp_trim[0], exp_trim[1], exp_trim[2], exp_trim[3]),
                        bat_discharge_kwh=bat_dis,
                    )
                    return best

        # 2) Uniform reduced DC power across the full candidate span.
        if span[1] - span[0] < 1:
            continue
        for level in range(19, 0, -1):
            cap = discharge_dc_step * level / 20.0
            if cap <= eps_step:
                break
            exports, bat_dis = _sim_hour_battery_grid_export_at_cap(
                span=span, dc_cap_per_q=cap, **common,
            )
            if any(exports[q] <= eps_step for q in range(span[0], span[1])):
                continue
            if min_hourly_kwh > eps_step and bat_dis + eps_step < min_hourly_kwh:
                continue
            key = (sum(exports), span[1] - span[0])
            if key > best_key:
                best_key = key
                best = _BatteryGridExportHourClaim(
                    hour=hour, span=span,
                    export_q=(exports[0], exports[1], exports[2], exports[3]),
                    bat_discharge_kwh=bat_dis,
                )
            break

    return best


def plan_battery_grid_export(
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
    discharge_dc_step: float,
    inverter_ac_step: float,
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
    """Plan battery→grid export for hours above the RCE floor.

    Eligible hours are chosen by hourly avg-RCE rank (0.01 rating; ties prefer
    the neighbour of the last success). SOC is claimed in chronological order
    inside each contiguous run using the same per-step survive reserve in claim
    planning and final replay. A later hour opens only when leftover SOC still
    exceeds the next-hour reserve by at least one min-hourly transfer.
    """
    if steps <= 0:
        return list(base_controls)
    slots = slots_per_hour_from_scale(step_scale)
    hours = sorted({
        (rce_step_offset + i) // slots for i in range(steps)
    })
    ratings: dict[int, float] = {}
    for h in hours:
        rating = hour_rce_rating(rce_series, h, slots_per_hour=slots)
        if rating is not None and rating + eps_step >= export_floor:
            ratings[int(h)] = float(rating)
    if not ratings:
        return [
            HourControl(c.grid_charge_kw, 0.0, c.load_from_grid) for c in base_controls
        ]

    def _hour_inputs(hour: int, soc_starts: list[float]) -> tuple[
        float, list[float], list[float], list[float], list[float], float | None,
    ] | None:
        idxs = _hour_steps_in_horizon(
            hour=hour, steps=steps, rce_step_offset=rce_step_offset,
            slots_per_hour=slots,
        )
        if not idxs:
            return None
        soc0 = float(soc_starts[idxs[0]])
        pv_q = [0.0, 0.0, 0.0, 0.0]
        load_q = [0.0, 0.0, 0.0, 0.0]
        reserve_q = [min_kwh, min_kwh, min_kwh, min_kwh]
        charge_q = [0.0, 0.0, 0.0, 0.0]
        for step in idxs:
            global_step = rce_step_offset + step
            q = global_step % slots
            if 0 <= q < 4:
                pv_q[q] = float(pv_series[step])
                load_q[q] = float(load_series[step])
                reserve_q[q] = float(reserves[step])
                base = base_controls[step] if step < len(base_controls) else HourControl(0.0, 0.0)
                charge_q[q] = float(base.grid_charge_kw)
        next_idxs = _hour_steps_in_horizon(
            hour=hour + 1, steps=steps, rce_step_offset=rce_step_offset,
            slots_per_hour=slots,
        )
        hour_end_floor = float(reserves[next_idxs[0]]) if next_idxs else None
        if hour_end_floor is not None:
            reserve_q = [min(r, hour_end_floor) for r in reserve_q]
        return soc0, pv_q, load_q, reserve_q, charge_q, hour_end_floor

    common = dict(
        steps=steps,
        pv_series=pv_series,
        load_series=load_series,
        rce_step_offset=rce_step_offset,
        step_scale=step_scale,
        initial_soc_kwh=initial_soc_kwh,
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step,
        inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid,
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid,
        eta_pv_battery=eta_pv_battery,
        eps_step=eps_step,
        reserves=reserves,
    )
    claim_kw = dict(
        battery_cap=battery_cap, min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step, inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
        eps_step=eps_step, min_hourly_kwh=min_hourly_kwh,
    )

    # Pass 1: select hours by rating; equal ratings → closest to last success.
    selected: set[int] = set()
    draft: dict[int, _BatteryGridExportHourClaim] = {}
    remaining = list(ratings.keys())
    last_assigned: int | None = None
    while remaining:
        h = pick_next_export_hour(remaining, ratings, last_hour=last_assigned)
        remaining = [x for x in remaining if x != h]
        trial = selected | {h}
        roles = export_window_roles(trial)
        _, soc_starts = _apply_battery_grid_export_claims_chrono(base_controls, draft, **common)
        inputs = _hour_inputs(h, soc_starts)
        if inputs is None:
            continue
        soc0, pv_q, load_q, reserve_q, charge_q, hour_end_floor = inputs
        hold = _hold_soc_for_later_battery_grid_export_claims(draft, from_hour=h, eta_out=eta_out)
        claim = _plan_hour_battery_grid_export_claim(
            hour=h, role=roles[h], soc0=soc0, hold_soc_kwh=hold,
            pv_q=pv_q, load_q=load_q, reserve_q=reserve_q, base_charge_q=charge_q,
            hour_end_floor_kwh=hour_end_floor,
            **claim_kw,
        )
        if claim is None or claim.export_ac_kwh <= eps_step:
            continue
        draft[h] = claim
        selected = trial
        last_assigned = h

    if not selected:
        return [
            HourControl(c.grid_charge_kw, 0.0, c.load_from_grid) for c in base_controls
        ]

    # Pass 2: claim SOC chronologically. Open the next hour in a run only when
    # SOC still exceeds survive-if-stopped-after-(h-1) by at least one min-hourly
    # transfer — sell leftover down to post_dis(h), do not leave a fat morning
    # buffer, and do not open a razor-thin orphan Dis.
    roles = export_window_roles(selected)
    claims: dict[int, _BatteryGridExportHourClaim] = {}
    min_next_dc = 0.0
    if min_hourly_kwh > eps_step and eta_out > 0:
        min_next_dc = float(min_hourly_kwh) / float(eta_out)

    for h in sorted(selected):
        _, soc_starts = _apply_battery_grid_export_claims_chrono(base_controls, claims, **common)
        inputs = _hour_inputs(h, soc_starts)
        if inputs is None:
            continue
        soc0, pv_q, load_q, reserve_q, charge_q, hour_end_floor = inputs
        if (h - 1) in claims:
            stop_floor = min_kwh
            cur_idxs = _hour_steps_in_horizon(
                hour=h, steps=steps, rce_step_offset=rce_step_offset,
                slots_per_hour=slots,
            )
            if cur_idxs:
                stop_floor = float(reserves[cur_idxs[0]])
            surplus_dc = max(0.0, soc0 - max(min_kwh, stop_floor))
            if surplus_dc + eps_step < min_next_dc:
                continue
        claim = _plan_hour_battery_grid_export_claim(
            hour=h, role=roles.get(h, "single"), soc0=soc0,
            hold_soc_kwh=_hold_soc_for_later_battery_grid_export_claims(draft, from_hour=h, eta_out=eta_out),
            pv_q=pv_q, load_q=load_q, reserve_q=reserve_q, base_charge_q=charge_q,
            hour_end_floor_kwh=hour_end_floor,
            **claim_kw,
        )
        if claim is not None and claim.export_ac_kwh > eps_step:
            claims[h] = claim

    # After a power-limited last Dis, open the next rated hour if leftover
    # above post_dis(last) still covers min_hourly — sell down toward morning min.
    while claims:
        last_h = max(claims)
        nxt = last_h + 1
        if nxt not in ratings or nxt in claims:
            break
        idxs = _hour_steps_in_horizon(
            hour=nxt, steps=steps, rce_step_offset=rce_step_offset,
            slots_per_hour=slots,
        )
        if not idxs:
            break
        _, soc_starts = _apply_battery_grid_export_claims_chrono(base_controls, claims, **common)
        inputs = _hour_inputs(nxt, soc_starts)
        if inputs is None:
            break
        soc0, pv_q, load_q, reserve_q, charge_q, hour_end_floor = inputs
        stop_floor = min_kwh
        if idxs:
            stop_floor = float(reserves[idxs[0]])
        surplus_dc = max(0.0, soc0 - max(min_kwh, stop_floor))
        if surplus_dc + eps_step < min_next_dc:
            break
        trial_roles = export_window_roles(set(claims) | {nxt})
        claim = _plan_hour_battery_grid_export_claim(
            hour=nxt, role=trial_roles.get(nxt, "last"), soc0=soc0, hold_soc_kwh=0.0,
            pv_q=pv_q, load_q=load_q, reserve_q=reserve_q, base_charge_q=charge_q,
            hour_end_floor_kwh=hour_end_floor,
            **claim_kw,
        )
        if claim is None or claim.export_ac_kwh <= eps_step:
            break
        claims[nxt] = claim
        draft[nxt] = claim

    # Recompute roles for the hours that actually received a claim.
    if claims:
        roles = export_window_roles(set(claims))
        final: dict[int, _BatteryGridExportHourClaim] = {}
        for h in sorted(claims):
            _, soc_starts = _apply_battery_grid_export_claims_chrono(base_controls, final, **common)
            inputs = _hour_inputs(h, soc_starts)
            if inputs is None:
                continue
            soc0, pv_q, load_q, reserve_q, charge_q, hour_end_floor = inputs
            claim = _plan_hour_battery_grid_export_claim(
                hour=h, role=roles.get(h, "single"), soc0=soc0,
                hold_soc_kwh=_hold_soc_for_later_battery_grid_export_claims(claims, from_hour=h, eta_out=eta_out),
                pv_q=pv_q, load_q=load_q, reserve_q=reserve_q, base_charge_q=charge_q,
                hour_end_floor_kwh=hour_end_floor,
                **claim_kw,
            )
            if claim is not None and claim.export_ac_kwh > eps_step:
                final[h] = claim
        claims = final

    controls, _ = _apply_battery_grid_export_claims_chrono(base_controls, claims, **common)
    return controls


assign_ranked_battery_export = plan_battery_grid_export
_plan_hour_export_claim = _plan_hour_battery_grid_export_claim


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
    discharge_dc_cap_kwh: float | None = None,
) -> HourPhysics:
    """One step: AC-meter PV vs AC load; PV→battery applies eta_pv_battery.

    ``ac_cap_kw`` is inverter AC headroom this step (export bus).
    ``discharge_dc_cap_kwh`` caps total battery DC withdraw (load + export);
    default None keeps legacy behaviour (no separate DC power ceiling).

    grid_charge_kw is AC kWh from the meter this step (Chg 6kW × 1h → 6 kWh import).
    DC stored = AC × eta_grid. Charge is applied before house load when load stays on
    the battery, so a Chg hour at min SOC nets charge − house on the battery.
    """
    reserve = min_kwh if reserve_soc_kwh is None else max(min_kwh, reserve_soc_kwh)
    soc = soc_kwh
    grid_import = 0.0
    grid_export = 0.0
    battery_delta = 0.0
    dc_used = 0.0

    def _dc_room() -> float:
        if discharge_dc_cap_kwh is None:
            return float("inf")
        return max(0.0, float(discharge_dc_cap_kwh) - dc_used)

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
            max_from_dc = _dc_room() * eta_out if eta_out > 0 else 0.0
            supplied = min(deficit, available * eta_out, max_from_dc)
            withdraw_load = supplied / eta_out if eta_out > 0 else 0.0
            soc -= withdraw_load
            battery_delta -= withdraw_load
            dc_used += withdraw_load
            available = max(0.0, soc - min_kwh)
            if deficit > supplied + epsilon:
                grid_import += deficit - supplied

    batt_export = min(max(0.0, control.battery_export_kwh), export_headroom)
    available_export = max(0.0, soc - reserve)
    if batt_export > epsilon and available_export > epsilon and eta_out > 0:
        export_withdraw = min(
            batt_export / eta_out,
            available_export,
            _dc_room(),
        )
        soc -= export_withdraw
        batt_export = export_withdraw * eta_out
        grid_export += batt_export
        battery_delta -= export_withdraw
        dc_used += export_withdraw

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

    # Cap SOC at capacity; leave below-min SOC unchanged (no lift to min_kwh).
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
    """Battery kWh to keep after *step* for self-use until morning PV covers house.

    Sums load deficits from step+1 until the first next-day hour where PV covers
    load (evening-started walks must cross midnight), then adds
    *reserve_floor_kwh* (min SOC). At the end of today's last discharge hour this
    is the SOC that must remain in the battery.

    Does **not** by itself justify grid→battery charging — see
    `_grid_charge_target_soc_kwh_from_step`.
    """
    return _forward_soc_need_from_step(
        step, pv_series, load_series, reserve_floor_kwh, eta_out, eta_pv_load, epsilon,
        buy_series=buy_series, offpeak_buy=offpeak_buy, peak_deficits_only=False,
        slots_per_hour=slots_per_hour, global_step_offset=global_step_offset,
    )


def apply_post_discharge_reserve_floor(
    reserves: list[float],
    controls: list[HourControl],
    *,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    rce_step_offset: int,
    slots_per_hour: int,
    buy_series: list[float] | None = None,
    offpeak_buy: float | None = None,
) -> tuple[list[float], float | None]:
    """Raise per-hour Dis reserves to the survive floor *if that hour were last*.

    For each export hour H in a contiguous run, steps in H must leave
    ``post_discharge_reserve_soc_kwh(H)`` (load after H until next-day PV cover
    + min SOC). A later hour in the run may go lower — its own post_dis(H) —
    so chrono fill can sell leftover above post_dis(prev) without shaving the
    overnight stock below the true end-of-window need. Mid-hour Dis tails also
    raise the last export step via ``_reserve_soc_kwh_from_step``.

    Returns (reserves, end_floor_kwh or None when no export).
    """
    if not controls or not reserves:
        return list(reserves), None
    eps = float(epsilon)
    slots = max(1, int(slots_per_hour))
    export_steps = [
        i for i, c in enumerate(controls)
        if i < len(reserves) and c.battery_export_kwh > eps
    ]
    if not export_steps:
        return list(reserves), None

    export_hours = sorted({
        (rce_step_offset + i) // slots for i in export_steps
    })
    out = list(reserves)
    global_floor: float | None = None

    for h in export_hours:
        hour_steps = [
            i for i in export_steps
            if (rce_step_offset + i) // slots == h
        ]
        if not hour_steps:
            continue
        floor_h = post_discharge_reserve_soc_kwh(
            h,
            pv_series,
            load_series,
            reserve_floor_kwh,
            eta_out,
            eta_pv_load,
            eps,
            buy_series=buy_series,
            offpeak_buy=offpeak_buy,
            slots_per_hour=slots,
            global_step_offset=rce_step_offset,
        )
        last_step = max(hour_steps)
        floor_tail = _reserve_soc_kwh_from_step(
            last_step,
            pv_series,
            load_series,
            reserve_floor_kwh,
            eta_out,
            eta_pv_load,
            eps,
            buy_series=buy_series,
            offpeak_buy=offpeak_buy,
            slots_per_hour=slots,
            global_step_offset=rce_step_offset,
        )
        end_floor = max(float(floor_h), float(floor_tail))
        # Window end target tracks the last export hour processed (sorted).
        global_floor = end_floor
        for i in range(len(out)):
            if (rce_step_offset + i) // slots == h and i <= last_step and out[i] < end_floor:
                out[i] = end_floor

    return out, global_floor


def post_discharge_reserve_soc_kwh(
    last_discharge_hour: int,
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
    """SOC to leave at end of a full-hour Dis (survive until next-day PV).

    Counts house deficits from the first hour *after* *last_discharge_hour*
    through the first next-day hour that covers load, then adds min-SOC floor.
    Mid-hour Dis ends use ``apply_post_discharge_reserve_floor`` (last export
    step) so load after the Dis window stays inside the budget.
    """
    slots = max(1, int(slots_per_hour))
    # from_step = last q15 of last_discharge_hour → walk starts at next hour.
    last_global = int(last_discharge_hour) * slots + (slots - 1)
    from_step = last_global - int(global_step_offset)
    if from_step < -1:
        return float(reserve_floor_kwh)
    return _reserve_soc_kwh_from_step(
        max(-1, from_step),
        pv_series,
        load_series,
        reserve_floor_kwh,
        eta_out,
        eta_pv_load,
        epsilon,
        buy_series=buy_series,
        offpeak_buy=offpeak_buy,
        slots_per_hour=slots,
        global_step_offset=global_step_offset,
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
    """SOC worth buying from the grid: floor + future *peak* house deficits only.

    Offpeak deficits are not part of the *purchase* budget — reserve/discharge
    already plans to avoid offpeak import (including midnight→morning peak
    selection). Grid→battery covers only missing kWh for the nearest peak hours
    until PV covers within the morning tariff horizon.
    Weekend / all-offpeak: floor only.
    """
    hour_buys = _day_hour_buys_from_series(
        buy_series,
        (global_step_offset + step) // max(1, slots_per_hour * HOURS_PER_DAY),
        slots_per_hour=slots_per_hour,
        global_step_offset=global_step_offset,
        offpeak_buy=offpeak_buy,
    )
    if morning_cover_bound_from_hour_buys(
        hour_buys, offpeak_buy=offpeak_buy, epsilon=epsilon,
    ) is None:
        return float(reserve_floor_kwh)

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
    discharge_dc_cap_kwh: float | None = None,
) -> float:
    """Max meter kWh exportable from battery after load, respecting reserve floor."""
    export_headroom = max(0.0, ac_cap_kw - load)
    if export_headroom <= epsilon or eta_out <= 0:
        return 0.0
    soc = soc_kwh
    deficit, _ = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    dc_used = 0.0
    if deficit > epsilon and eta_out > 0:
        withdraw = min(deficit / eta_out, max(0.0, soc - min_kwh))
        if discharge_dc_cap_kwh is not None:
            withdraw = min(withdraw, max(0.0, float(discharge_dc_cap_kwh) - dc_used))
        soc -= withdraw
        dc_used += withdraw
    exportable_soc = max(0.0, soc - max(min_kwh, reserve_soc_kwh))
    dc_room = (
        max(0.0, float(discharge_dc_cap_kwh) - dc_used)
        if discharge_dc_cap_kwh is not None
        else exportable_soc
    )
    return min(exportable_soc * eta_out, export_headroom, dc_room * eta_out)


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

    Returns AC kWh to charge this step (0 if not allowed). Uses only the AC
    still needed to reach *charge_target_soc_kwh* (not always the hardware cap).
    """
    if buy_p > offpeak_buy + epsilon:
        return 0.0
    if charge_target_soc_kwh <= soc_kwh + epsilon:
        return 0.0
    if head_room_kwh <= epsilon:
        return 0.0
    need_dc = charge_target_soc_kwh - soc_kwh
    need_ac = need_dc / eta_grid if eta_grid > 0 else need_dc
    max_ac = head_room_kwh / eta_grid if eta_grid > 0 else head_room_kwh
    return min(charge_ac_cap_kw, max_ac, max(0.0, need_ac))


def _control_options(
    soc_kwh: float,
    pv: float,
    load: float,
    *,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_cap_kwh: float,
    inverter_ac_cap_kw: float,
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
        min_kwh=min_kwh, ac_cap_kw=inverter_ac_cap_kw,
        eta_out=eta_out, eta_pv_load=eta_pv_load,
        reserve_soc_kwh=reserve_soc_kwh,
        epsilon=epsilon,
        discharge_dc_cap_kwh=discharge_dc_cap_kwh,
    )
    # Tier caps are AC export from battery at the DC power ceiling.
    export_ac_cap = (
        discharge_dc_cap_kwh * eta_out if eta_out > 0 else discharge_dc_cap_kwh
    )
    min_viable = max(epsilon, export_ac_cap * EXPORT_MIN_FRAC)
    if max_batt_export >= min_viable and allow_battery_export:
        for frac in EXPORT_POWER_FRACS:
            tier_cap = export_ac_cap * frac
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


def _battery_grid_charge_step_ac(
    budget_ac: float,
    *,
    charge_ac_step: float,
    step_scale: float,
    min_block_minutes: int,
    eps_step: float,
) -> float:
    """AC kWh per fill step: pack at hardware max (dense earliest offpeak).

    Do not dilute power across extra quarters just to hit ``min_block`` — that
    splits one budget into multiple half-hour Chg rows (e.g. 01:00-01:30 then
    02:00-02:30). Fill consecutive steps at max until the budget is spent so
    ~4 kWh fits in one clock hour when inverter/battery allow it. Timer
    ``min_block`` / ``min_hourly_transfer_kwh`` stay enforced in timer_plan /
    ``enforce_min_hourly_battery_grid_limits``.
    """
    del step_scale, min_block_minutes  # unused here; callers may still pass them
    if budget_ac <= eps_step or charge_ac_step <= eps_step:
        return 0.0
    return float(charge_ac_step)


def plan_battery_grid_charge(
    controls: list[HourControl],
    *,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    offpeak_buy: float,
    charge_targets: list[float],
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    charge_ac_step: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    step_scale: float = 1.0,
    skip_leading_slots: int | None = None,
    min_block_minutes: int | None = None,
    min_hourly_kwh: float = 0.0,
) -> list[HourControl]:
    """Plan battery grid-charge slots: move DP pre-peak volume to earliest offpeak steps.

    Default: skip the first clock hour of the horizon (current hour) so Chg is
    not placed in the in-progress hour. Pass ``skip_leading_slots=0`` when the
    horizon already starts after a committed current hour (future-only replan).

    Relocate the optimizer AC budget into consecutive offpeak steps from
    ``fill_from_step`` at hardware max so one budget packs into the earliest
    clock hour(s).

    Drop the budget when it is below ``min_hourly_kwh`` and forcing a min block
    would cost more than buying the same house energy at peak.

    ``charge_targets`` is unused; callers may still pass it.
    House load stays on the battery during the relocated fill.
    """
    del charge_targets
    if not controls:
        return controls

    slots_per_hour = slots_per_hour_from_scale(step_scale)
    if skip_leading_slots is None:
        skip_leading_slots = slots_per_hour
    fill_from_step = min(len(controls), max(0, int(skip_leading_slots)))
    if min_block_minutes is None:
        min_block_minutes = 30

    first_peak = len(controls)
    peak_buy = float(offpeak_buy)
    for i, p in enumerate(buy_prices):
        if i >= len(controls):
            break
        price = float(p)
        if price > offpeak_buy + eps_step:
            first_peak = i
            peak_buy = price
            break

    # Optimizer-decided volume: all pre-peak offpeak charge DP already chose.
    budget_ac = sum(
        float(controls[i].grid_charge_kw)
        for i in range(min(first_peak, len(controls)))
        if float(buy_prices[i] if i < len(buy_prices) else offpeak_buy)
        <= offpeak_buy + eps_step
    )
    if budget_ac > eps_step and not offpeak_min_block_charge_is_worth(
        need_ac_kwh=budget_ac,
        min_hourly_kwh=float(min_hourly_kwh),
        offpeak_buy=float(offpeak_buy),
        peak_buy=float(peak_buy),
        eta_grid=float(eta_grid),
        eta_out=float(eta_out),
        epsilon=float(eps_step),
    ):
        budget_ac = 0.0
    if budget_ac <= eps_step:
        # No budget (or economics rejected it): clear all pre-peak offpeak Chg,
        # including DP leftovers that would otherwise stay as thin orphan slots.
        out_clear: list[HourControl] = []
        for step, prev in enumerate(controls):
            buy_p = float(buy_prices[step]) if step < len(buy_prices) else offpeak_buy
            clear_chg = (
                step < first_peak
                and buy_p <= offpeak_buy + eps_step
                and float(prev.grid_charge_kw) > eps_step
            )
            if clear_chg:
                out_clear.append(
                    HourControl(0.0, prev.battery_export_kwh, prev.load_from_grid)
                )
            else:
                out_clear.append(prev)
        return out_clear

    step_ac = _battery_grid_charge_step_ac(
        budget_ac,
        charge_ac_step=charge_ac_step,
        step_scale=step_scale,
        min_block_minutes=int(min_block_minutes),
        eps_step=eps_step,
    )

    fill_done = False
    budget_left = float(budget_ac)
    out: list[HourControl] = []
    soc = float(initial_soc_kwh)
    for step, prev in enumerate(controls):
        pv = float(pv_series[step]) if step < len(pv_series) else 0.0
        load = float(load_series[step]) if step < len(load_series) else 0.0
        buy_p = float(buy_prices[step]) if step < len(buy_prices) else offpeak_buy
        reserve = float(reserves[step]) if step < len(reserves) else min_kwh

        if step >= first_peak:
            ctrl = HourControl(prev.grid_charge_kw, 0.0, prev.load_from_grid)
        elif step < fill_from_step:
            # Current hour: never start grid charge here.
            ctrl = HourControl(0.0, 0.0, False)
        else:
            charge_kw = 0.0
            if (
                not fill_done
                and buy_p <= offpeak_buy + eps_step
                and budget_left > eps_step
            ):
                head_room = max(0.0, battery_cap - soc)
                max_ac = head_room / eta_grid if eta_grid > 0 else head_room
                charge_kw = min(step_ac, charge_ac_step, max_ac, budget_left)
                if charge_kw <= eps_step:
                    charge_kw = 0.0
            ctrl = HourControl(charge_kw, 0.0, False)

        phys = simulate_hour(
            soc, pv, load, ctrl,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=discharge_dc_step,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            epsilon=eps_step,
            reserve_soc_kwh=reserve,
        )
        out.append(ctrl)
        soc = phys.soc_end
        if ctrl.grid_charge_kw > eps_step:
            budget_left = max(0.0, budget_left - float(ctrl.grid_charge_kw))
            if budget_left <= eps_step:
                fill_done = True
    return out



def enforce_min_hourly_battery_grid_limits(
    controls: list[HourControl],
    *,
    rce_step_offset: int,
    step_scale: float,
    min_hourly_kwh: float,
    epsilon: float,
) -> list[HourControl]:
    """Enforce min_hourly_transfer_kwh on battery↔grid flows per clock hour.

    Export or charge below the floor is cleared. Do not scale a thin overnight
    top-up up to the floor — that forces an uneconomic min block (e.g. 2 kWh
    Chg to cover a 0.3 kWh morning gap).
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
            for i in idxs:
                c = out[i]
                out[i] = HourControl(0.0, c.battery_export_kwh, c.load_from_grid)
    return out


_front_load_offpeak_grid_charge = plan_battery_grid_charge
_front_load_charge_step_ac = _battery_grid_charge_step_ac
_correct_min_hourly_transfer_controls = enforce_min_hourly_battery_grid_limits


def offpeak_min_block_charge_is_worth(
    *,
    need_ac_kwh: float,
    min_hourly_kwh: float,
    offpeak_buy: float,
    peak_buy: float,
    eta_grid: float,
    eta_out: float,
    epsilon: float,
) -> bool:
    """Whether an offpeak grid→battery block pays for itself vs peak house buy.

    When *need_ac_kwh* is below *min_hourly_kwh*, the timer must take the full
    min block (or nothing). Skip the block when its offpeak cost exceeds the
    peak-tariff cost of buying only the needed house energy.
    """
    need = max(0.0, float(need_ac_kwh))
    if need <= epsilon:
        return False
    floor = max(0.0, float(min_hourly_kwh))
    off = max(0.0, float(offpeak_buy))
    peak = max(off, float(peak_buy))
    eta_g = max(1e-9, float(eta_grid))
    eta_o = max(1e-9, float(eta_out))
    # Battery DC from AC charge ≈ need*eta_grid; that DC serves peak AC load *eta_out.
    avoided_peak_ac = need * eta_g * eta_o
    cost_avoided = avoided_peak_ac * peak
    charge_ac = need if need + epsilon >= floor or floor <= epsilon else floor
    cost_charge = charge_ac * off
    return cost_charge <= cost_avoided + epsilon


def _should_extend_forecast_lookahead(
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
) -> bool:
    """True when q15 reserve/charge should append forecast through end of tomorrow."""
    forecast_data = forecast or {}
    tomorrow = (forecast_data.get("tomorrow") or {})
    if step_scale >= 1.0:
        return False
    if not tomorrow.get("pv") or not tomorrow.get("load"):
        return False
    tomorrow_date = today_date + timedelta(days=1)
    return end_dt.date() <= tomorrow_date


def _tomorrow_lookahead_start_hour(
    *,
    end_dt: datetime,
    today_date,
    series_len: int = 0,
    global_step_offset: int = 0,
    step_scale: float = 0.25,
) -> int | None:
    """First clock hour of tomorrow not yet in the optimized series (0..23), or None.

    Prefer series coverage (offset + length) over *end_dt* alone so a mismatched
    end timestamp cannot invent or skip tomorrow hours.
    """
    tomorrow_date = today_date + timedelta(days=1)
    slots = slots_per_hour_from_scale(step_scale)
    if series_len > 0:
        last_abs_hour = (global_step_offset + series_len - 1) // slots
        if last_abs_hour < HOURS_PER_DAY:
            # Series still on today — classic overnight append when end is today.
            if end_dt.date() == today_date:
                return 0
            return None
        last_tom_h = last_abs_hour - HOURS_PER_DAY
        nxt = last_tom_h + 1
        return nxt if nxt < HOURS_PER_DAY else None

    # Fallback when callers omit series length: end_dt marks the last plan slot.
    if end_dt.date() == today_date:
        return 0
    if end_dt.date() == tomorrow_date:
        nxt = int(end_dt.hour) + 1
        if nxt >= HOURS_PER_DAY:
            return None
        return nxt
    return None


def build_extended_pv_load_for_reserve(
    pv_series: list[float],
    load_series: list[float],
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
    global_step_offset: int = 0,
) -> tuple[list[float], list[float]]:
    """Append PV/load through end of tomorrow for reserve / charge-target walks.

    Optimized steps stay unchanged. Only hours after the series up to tomorrow
    23:00 are appended (full tomorrow when the plan still ends today; otherwise
    the missing tomorrow tail after a rolling 24h window).
    """
    forecast_data = forecast or {
        "today": {"pv": [], "load": []},
        "tomorrow": {"pv": [], "load": []},
    }
    if not _should_extend_forecast_lookahead(
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    ):
        return pv_series, load_series
    start_h = _tomorrow_lookahead_start_hour(
        end_dt=end_dt,
        today_date=today_date,
        series_len=len(pv_series),
        global_step_offset=global_step_offset,
        step_scale=step_scale,
    )
    if start_h is None:
        return pv_series, load_series
    rep = slots_per_hour_from_scale(step_scale)
    pv_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["pv"] or [])][:HOURS_PER_DAY]
    load_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["load"] or [])][:HOURS_PER_DAY]
    pv_ext = list(pv_series)
    load_ext = list(load_series)
    for h in range(start_h, HOURS_PER_DAY):
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
    global_step_offset: int = 0,
) -> list[float]:
    """Extend buy prices through end of tomorrow in lockstep with PV/load lookahead."""
    forecast_data = forecast or {
        "today": {"pv": [], "load": []},
        "tomorrow": {"pv": [], "load": []},
    }
    buy_for_reserve = list(buy_series)
    if not _should_extend_forecast_lookahead(
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    ):
        return buy_for_reserve
    start_h = _tomorrow_lookahead_start_hour(
        end_dt=end_dt,
        today_date=today_date,
        series_len=len(buy_series),
        global_step_offset=global_step_offset,
        step_scale=step_scale,
    )
    if start_h is None:
        return buy_for_reserve
    rep = slots_per_hour_from_scale(step_scale)
    tomorrow = today_date + timedelta(days=1)
    base = datetime(tomorrow.year, tomorrow.month, tomorrow.day)
    for h in range(start_h, HOURS_PER_DAY):
        price, _ = get_buy_price(base.replace(hour=h), cfg)
        buy_for_reserve.extend([float(price)] * rep)
    return buy_for_reserve


# Back-compat alias for callers/tests that still use the old name.
_should_extend_reserve_horizon = _should_extend_forecast_lookahead


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
        global_step_offset=global_step_offset,
    )
    buy_r = list(buy_prices) if buy_prices is not None else []
    if cfg is not None and buy_prices is not None:
        buy_r = build_extended_buy_for_reserve(
            buy_prices,
            step_scale=step_scale, end_dt=end, today_date=today_date,
            forecast=forecast, cfg=cfg,
            global_step_offset=global_step_offset,
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
    front_load_skip_leading_slots: int | None = None,
) -> list[HourControl]:
    from .plan_cost import hour_grid_cash_pln

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    reserve_floor_kwh = plan_reserve_min_soc_kwh(cfg)
    # Timer Dis is DC kW; inverter bus is separate AC headroom for export.
    discharge_dc_kw = plan_timer_discharge_power_kw(cfg)
    inverter_ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
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
    discharge_dc_step = discharge_dc_kw * step_scale
    inverter_ac_step = inverter_ac_kw * step_scale
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

    # Reserve / charge-target walks use forecast through end of tomorrow when the
    # rolling plan window is shorter (e.g. ends at tomorrow H04). Optimized DP
    # steps stay within the plan window only.
    pv_for_reserve, load_for_reserve = build_extended_pv_load_for_reserve(
        pv_series, load_series,
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
        global_step_offset=rce_step_offset,
    )
    buy_for_reserve = build_extended_buy_for_reserve(
        buy_prices,
        step_scale=step_scale, end_dt=end_dt, today_date=today_date,
        forecast=forecast_data, cfg=cfg,
        global_step_offset=rce_step_offset,
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
                discharge_dc_cap_kwh=discharge_dc_step,
                inverter_ac_cap_kw=inverter_ac_step,
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
                    battery_cap=battery_cap, min_kwh=min_kwh,
                    ac_cap_kw=inverter_ac_step,
                    discharge_dc_cap_kwh=discharge_dc_step,
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
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac_kw,
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

    controls = plan_battery_grid_charge(
        controls,
        pv_series=pv_series,
        load_series=load_series,
        buy_prices=buy_prices,
        offpeak_buy=offpeak_buy,
        charge_targets=charge_targets,
        initial_soc_kwh=initial_soc_kwh,
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        charge_ac_step=charge_ac_step,
        discharge_dc_step=discharge_dc_step,
        inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid,
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid,
        eta_pv_battery=eta_pv_battery,
        eps_step=eps_step,
        reserves=reserves,
        step_scale=step_scale,
        skip_leading_slots=front_load_skip_leading_slots,
        min_block_minutes=plan_timer_min_block_minutes(cfg),
        min_hourly_kwh=min_hourly_transfer,
    )

    controls = plan_battery_grid_export(
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
        discharge_dc_step=discharge_dc_step,
        inverter_ac_step=inverter_ac_step,
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

    return enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=rce_step_offset,
        step_scale=step_scale,
        min_hourly_kwh=min_hourly_transfer,
        epsilon=eps_step,
    )
