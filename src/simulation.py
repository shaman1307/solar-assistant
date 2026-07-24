"""
Rolling energy balance simulation (15-min optimizer, hourly display).

Plan actions minimise G12 Energa cash cost over the horizon via dynamic
programming (see plan_optimizer.py).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from .g12_pricing import get_buy_price
from .plan_cost import compute_plan_totals
from .debug_smart_plan import (
    build_smart_plan_hour_row,
    collect_q15_schedule_rows,
    merge_today_hourly_profile,
    run_day_smart_q15_plan,
    run_today_smart_q15_plan,
)
from .inverter_sim import _initial_soc_kwh
from .plan_hourly_actuals import (
    apply_current_hour_blend,
    build_blended_current_hour_q15,
    build_completed_history_rows,
    build_h0_carryover_row,
    hour_in_progress,
    hourly_profile_to_q15,
    interval_end_label,
    replay_forward_soc_on_rows,
    sync_blended_current_hour_row,
)
from .plan_optimizer import (
    battery_export_break_even_rce,
    g12_tariff_from_cfg,
)
from .rce import quarter_rce_for_dates
from .plan_timer_override import (
    apply_plan_timer_overrides_if_any,
    get_timer_overrides_for_date,
)
from .simulation_config import get_simulation_params, plan_min_soc_pct
from .timer_plan import (
    derive_timer_schedule_q15,
    quarter_start_minute,
    sa_discharge_timer_for_hour,
)

Q15_PER_HOUR = 4
STEP_SCALE = 1.0 / Q15_PER_HOUR


def _now_warsaw() -> datetime:
    from .influxdb import now_warsaw
    return now_warsaw()


def g12_battery_export_economics(cfg: dict) -> dict[str, float]:
    """G12 net-billing economics for battery→grid export vs self-consumption."""
    g12 = cfg["grid"]["g12"]
    offpeak = float(g12["offpeak_price_pln_kwh"])
    energy = float(g12["offpeak_energy_only_pln_kwh"])
    distribution = offpeak - energy
    tariff = g12_tariff_from_cfg(cfg)
    min_rce = battery_export_break_even_rce(tariff, cfg)
    return {
        "offpeak_full_pln": offpeak,
        "offpeak_energy_pln": energy,
        "distribution_pln": distribution,
        "min_rce_export_pln": min_rce,
        "self_use_value_pln": offpeak,
    }


def battery_export_profitable(rce_price: float | None, cfg: dict) -> bool:
    if rce_price is None:
        return False
    econ = g12_battery_export_economics(cfg)
    rce = float(rce_price)
    return rce >= econ["min_rce_export_pln"]


def _hourly_forecast_kwh(
    dt: datetime,
    today_date,
    pv_today: list[float],
    pv_tomorrow: list[float],
    load_today: list[float],
    load_tomorrow: list[float],
) -> tuple[float, float]:
    hour = dt.hour
    if dt.date() == today_date:
        pv_h = float(pv_today[hour]) if hour < len(pv_today) else 0.0
        load_h = float(load_today[hour]) if hour < len(load_today) else 0.0
    else:
        pv_h = float(pv_tomorrow[hour]) if hour < len(pv_tomorrow) else 0.0
        load_h = float(load_tomorrow[hour]) if hour < len(load_tomorrow) else 0.0
    return pv_h, load_h


def _hourly_soc_kwh(
    hourly: dict[str, list] | None,
    hour: int,
    battery_cap: float,
    min_soc_pct: float,
) -> float | None:
    """End-of-hour SOC in kWh from Influx hourly accruals."""
    if not hourly or not (0 <= hour < 24):
        return None
    soc_series = hourly.get("soc") or [None] * 24
    if hour >= len(soc_series) or soc_series[hour] is None:
        return None
    pct = max(min_soc_pct, min(100.0, float(soc_series[hour])))
    return (pct / 100.0) * battery_cap


def _plan_row_end_soc_kwh(row: dict[str, Any], battery_cap: float) -> float | None:
    """End-of-hour SOC (kWh) from a plan row's last q15 slot or hour soc %."""
    if battery_cap <= 0:
        return None
    q15 = row.get("q15") or []
    if q15:
        try:
            last_soc_pct = float(q15[-1].get("soc") or 0)
            return (last_soc_pct / 100.0) * battery_cap
        except (TypeError, ValueError):
            pass
    soc_pct = row.get("soc")
    if soc_pct is not None:
        try:
            return (float(soc_pct) / 100.0) * battery_cap
        except (TypeError, ValueError):
            pass
    return None


def _smart_plan_hour_end_soc_kwh(
    plan: dict[str, Any] | None,
    hour: int,
    battery_cap: float,
) -> float | None:
    """Forecast end-of-hour SOC (kWh) from optimizer q15 slots for *hour*."""
    if battery_cap <= 0 or plan is None:
        return None
    slots = (plan.get("q15_by_hour") or {}).get(hour) or []
    if not slots:
        return None
    last = slots[-1]
    raw = last.get("soc_pct")
    if raw is None:
        raw = last.get("soc")
    if raw is None:
        return None
    try:
        return (float(raw) / 100.0) * battery_cap
    except (TypeError, ValueError):
        return None


def _committed_current_hour_row(
    today_str: str,
    plan_from_hour: int,
) -> dict[str, Any] | None:
    """SQLite current-hour row with a non-empty Timer Schedule (must not be rewritten).

    When H01 was planned as future Chg and the clock hits 01:00, that row is already
    the committed current hour — optimizer must keep it and seed later hours from
    its end-of-hour SOC.
    """
    try:
        from .sqlite_store import read_plan
        stored = read_plan()
    except Exception:
        return None
    if not stored:
        return None
    for row in stored.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or "") != today_str:
            continue
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if hour != plan_from_hour:
            continue
        if str(row.get("timer_schedule") or "").strip():
            return row
        return None
    return None


def _locked_current_hour_end_soc_kwh(
    plan_from_hour: int,
    today_str: str,
    battery_cap: float,
) -> float | None:
    """SOC at end of current (locked) hour from SQLite plan_latest.

    Returns None when the stored row is not locked or not found.
    """
    try:
        from .sqlite_store import read_plan
        stored = read_plan()
        if not stored:
            return None
        rows = stored.get("rows") or []
        for row in rows:
            if (
                str(row.get("plan_date") or "") == today_str
                and int(row.get("hour", -1)) == plan_from_hour
                and row.get("hour_labels_locked")
                and row.get("start") != "TOTAL"
            ):
                return _plan_row_end_soc_kwh(row, battery_cap)
    except Exception:
        pass
    return None


def _plan_start_soc_kwh(
    plan_from_hour: int,
    today_hourly: dict | None,
    battery_cap: float,
    min_soc_pct: float,
    day_start_soc: float,
    live_soc_kwh: float,
) -> float:
    """SOC at the start of plan_from_hour (end of the last completed hour).

    At hour 0 always use live SOC. ``day_start_soc`` from ``_initial_soc_kwh``
    falls back to 50% of capacity when today's hour-0 Influx SOC is still
    missing (typical just after midnight) and must not seed the blended row.
    """
    del day_start_soc  # unused at hour 0; kept for call-site compatibility
    if plan_from_hour > 0 and today_hourly:
        anchor = _hourly_soc_kwh(
            today_hourly, plan_from_hour - 1, battery_cap, min_soc_pct,
        )
        if anchor is not None:
            return anchor
    # Hour 0, or no prior-hour Influx anchor: live meter is the source of truth.
    return live_soc_kwh


def apply_locked_hour_labels_from_plan(
    result: dict[str, Any],
    existing: dict[str, Any] | None,
    now: datetime,
    cfg: dict | None = None,
) -> None:
    """After any rebuild: keep locked timer/action for current hour from SQLite.

    At :00: if SQLite already has a non-empty Timer for this hour (planned while
    it was still future), keep that timer/action and the row energy/SOC — do not
    adopt a fresh empty Chg wipe from front-load. Otherwise lock fresh labels.
    Mid-hour: keep locked labels from SQLite.
    Once locked, timer_schedule and action are never clipped or rewritten.
    """
    del cfg
    today_str = now.strftime("%Y-%m-%d")
    hour = now.hour

    existing_row = None
    if existing:
        existing_row = next(
            (
                r for r in (existing.get("rows") or [])
                if r.get("start") != "TOTAL"
                and str(r.get("plan_date") or "") == today_str
                and int(r.get("hour", -1)) == hour
            ),
            None,
        )

    if now.minute == 0:
        for row in result.get("rows") or []:
            if row.get("start") == "TOTAL":
                continue
            if str(row.get("plan_date") or "") != today_str or int(row.get("hour", -1)) != hour:
                continue
            if row.get("timer_schedule_manual"):
                row["hour_labels_locked"] = True
                break
            existing_timer = (
                str(existing_row.get("timer_schedule") or "").strip()
                if existing_row is not None
                else ""
            )
            if existing_row is not None and existing_timer:
                # Commit the already-planned current hour; do not take fresh wipe.
                for key, val in existing_row.items():
                    row[key] = copy.deepcopy(val)
                row["hour_labels_locked"] = True
            else:
                row["hour_labels_locked"] = True
            break
        return

    for row in result.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or "") != today_str or int(row.get("hour", -1)) != hour:
            continue
        if (
            existing_row is not None
            and existing_row.get("hour_labels_locked")
            and not row.get("timer_schedule_manual")
        ):
            row["timer_schedule"] = existing_row.get("timer_schedule", "")
            row["action"] = existing_row.get("action", "")
            row["hour_labels_locked"] = True
            # Keep frozen q15 slots from SQLite (from_actual) on full rebuild.
            existing_q15 = list(existing_row.get("q15") or [])
            if existing_q15:
                row["q15"] = copy.deepcopy(existing_q15)
                for key in (
                    "production", "consumption", "battery", "bat_charge",
                    "bat_discharge", "grid_import", "grid_export", "soc",
                    "import_cost", "export_revenue", "energy_cost", "service_cost",
                    "cost",
                ):
                    if key in existing_row:
                        row[key] = copy.deepcopy(existing_row[key])
        break


def build_energy_arbitrage_plan(
    forecast: dict[str, Any],
    live_metrics: dict[str, Any],
    rules: dict[str, Any],
    cfg: dict,
    rce_prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    epsilon = float(params["epsilon_kwh"])

    initial_soc_pct = max(min_soc_pct, min(100.0, float(live_metrics.get("battery_soc", 50.0))))
    soc_kwh = (initial_soc_pct / 100.0) * battery_cap

    pv_today = forecast["today"]["pv"]
    pv_tomorrow = forecast["tomorrow"]["pv"]
    load_today = forecast["today"]["load"]
    load_tomorrow = forecast["tomorrow"]["load"]
    pv_forecast_today = forecast["today"].get("pv_forecast") or pv_today
    load_forecast_today = forecast["today"].get("load_forecast") or load_today

    now = _now_warsaw()
    start_dt = now.replace(minute=0, second=0, microsecond=0)
    today_date = start_dt.date()
    today_str = start_dt.strftime("%Y-%m-%d")
    tomorrow_str = (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_str = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    hour_steps = int(params["horizon_hours"])
    plan_from_hour = start_dt.hour

    rce_dates = [today_str, tomorrow_str]
    if start_dt.hour == 0:
        rce_dates.insert(0, yesterday_str)
    quarters_by_date = quarter_rce_for_dates(*rce_dates)
    tomorrow_remainder_rows = _tomorrow_remainder_rows(
        start_dt=start_dt,
        horizon_hours=hour_steps,
        today_date=today_date,
        pv_tomorrow=pv_tomorrow,
        load_tomorrow=load_tomorrow,
        quarters_by_date=quarters_by_date,
        cfg=cfg,
    )

    actual_step0 = hour_in_progress(now, start_dt)
    today_hourly = live_metrics.get("today_hourly")
    series_10min = live_metrics.get("series_10min")

    # Optimizer: Influx for completed hours only; current hour blended actual+forecast.
    pv_merged, load_merged = merge_today_hourly_profile(
        pv_forecast_today,
        load_forecast_today,
        today_hourly,
        until_hour=plan_from_hour,
    )
    forecast_pv_q15 = forecast["today"].get("pv_forecast_q15") or forecast["today"].get("pv_q15")
    forecast_load_q15 = forecast["today"].get("load_q15")
    day_start_soc, _ = _initial_soc_kwh(today_hourly or {}, battery_cap)
    if not today_hourly:
        day_start_soc = soc_kwh

    rce_today = quarters_by_date.get(today_str) or []
    committed_hour = _committed_current_hour_row(today_str, plan_from_hour)
    committed_end_soc = (
        _plan_row_end_soc_kwh(committed_hour, battery_cap)
        if committed_hour is not None
        else None
    )
    # Current hour already has a Timer (e.g. Chg planned while it was future):
    # do not re-optimize that hour — seed the rest of the day from its end SOC.
    if committed_hour is not None and committed_end_soc is not None and plan_from_hour < 23:
        smart_today = run_day_smart_q15_plan(
            date_str=today_str,
            pv_hourly=pv_merged,
            load_hourly=load_merged,
            tomorrow_pv=pv_tomorrow,
            tomorrow_load=load_tomorrow,
            cfg=cfg,
            rce_quarters=rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None,
            initial_soc_kwh=committed_end_soc,
            from_hour=plan_from_hour + 1,
            front_load_skip_leading_slots=0,
        )
    elif committed_hour is not None and committed_end_soc is not None:
        # Hour 23 committed — only tomorrow is re-planned from this end SOC.
        smart_today = {
            "q15_by_hour": {h: [] for h in range(24)},
            "q15_plan_rows": [],
            "end_soc_kwh": round(committed_end_soc, 3),
            "timer_schedule": {},
            "epsilon": epsilon,
        }
    else:
        smart_today = run_today_smart_q15_plan(
            date_str=today_str,
            pv_hourly=pv_merged,
            load_hourly=load_merged,
            tomorrow_pv=pv_tomorrow,
            tomorrow_load=load_tomorrow,
            cfg=cfg,
            rce_quarters=rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None,
            plan_from_hour=plan_from_hour,
            day_start_soc_kwh=day_start_soc,
            live_soc_kwh=soc_kwh,
        )

    if 0 <= plan_from_hour < 24:
        pv_merged, load_merged = apply_current_hour_blend(
            pv_merged,
            load_merged,
            plan_from_hour,
            now,
            forecast_pv_q15=forecast_pv_q15,
            forecast_load_q15=forecast_load_q15,
            series_10min=series_10min,
        )

    merged_pv_q15_today = hourly_profile_to_q15(pv_merged)
    merged_load_q15_today = hourly_profile_to_q15(load_merged)
    merged_pv_q15_tomorrow = hourly_profile_to_q15([float(v) for v in pv_tomorrow])
    merged_load_q15_tomorrow = hourly_profile_to_q15([float(v) for v in load_tomorrow])

    plan_start_soc_kwh = _plan_start_soc_kwh(
        plan_from_hour,
        today_hourly,
        battery_cap,
        min_soc_pct,
        day_start_soc,
        soc_kwh,
    )

    hours_today_in_plan = max(0, 24 - plan_from_hour)
    need_tomorrow_hours = max(0, hour_steps - hours_today_in_plan)
    smart_tomorrow: dict[str, Any] | None = None
    if need_tomorrow_hours > 0 and smart_today:
        rce_tomorrow = quarters_by_date.get(tomorrow_str) or []
        # Prefer committed/locked current-hour end SOC (charge/discharge already fixed).
        tomorrow_initial_soc = committed_end_soc
        if tomorrow_initial_soc is None:
            tomorrow_initial_soc = _locked_current_hour_end_soc_kwh(
                plan_from_hour, today_str, battery_cap,
            )
        if tomorrow_initial_soc is None:
            tomorrow_initial_soc = float(smart_today["end_soc_kwh"])
        smart_tomorrow = run_day_smart_q15_plan(
            date_str=tomorrow_str,
            pv_hourly=[float(v) for v in pv_tomorrow],
            load_hourly=[float(v) for v in load_tomorrow],
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            rce_quarters=rce_tomorrow if len(rce_tomorrow) >= Q15_PER_HOUR * 24 else None,
            initial_soc_kwh=tomorrow_initial_soc,
        )

    today_timer_ov = get_timer_overrides_for_date(cfg, today_str)
    tomorrow_timer_ov = get_timer_overrides_for_date(cfg, tomorrow_str)
    rce_today_full = rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None
    rce_tomorrow_full = (
        quarters_by_date.get(tomorrow_str) or []
    )
    rce_tomorrow_full = (
        rce_tomorrow_full if len(rce_tomorrow_full) >= Q15_PER_HOUR * 24 else None
    )

    if smart_today and today_timer_ov:
        ov_from = plan_from_hour + 1 if committed_hour is not None else plan_from_hour
        smart_today = apply_plan_timer_overrides_if_any(
            smart_today,
            date_str=today_str,
            pv_hourly=pv_merged,
            load_hourly=load_merged,
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=ov_from,
            rce_quarters=rce_today_full,
        )

    if smart_tomorrow and today_timer_ov and smart_today:
        smart_tomorrow = run_day_smart_q15_plan(
            date_str=tomorrow_str,
            pv_hourly=[float(v) for v in pv_tomorrow],
            load_hourly=[float(v) for v in load_tomorrow],
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            rce_quarters=rce_tomorrow_full,
            initial_soc_kwh=float(smart_today["end_soc_kwh"]),
            from_hour=0,
        )

    if smart_tomorrow and tomorrow_timer_ov:
        smart_tomorrow = apply_plan_timer_overrides_if_any(
            smart_tomorrow,
            date_str=tomorrow_str,
            pv_hourly=[float(v) for v in pv_tomorrow],
            load_hourly=[float(v) for v in load_tomorrow],
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=0,
            rce_quarters=rce_tomorrow_full,
        )

    export_hours: set[int] = set()
    all_rows: list[dict] = []
    remaining = hour_steps
    blended_anchor_kwh: float | None = None
    blended_row_idx: int | None = None

    if smart_today or committed_hour is not None:
        for h in range(plan_from_hour, 24):
            if remaining <= 0:
                break
            if h == plan_from_hour and committed_hour is not None:
                # Keep the committed Timer/Action/SOC path — do not rebuild from fresh DP.
                row = copy.deepcopy(committed_hour)
                row["hour_labels_locked"] = True
                if h == plan_from_hour and now.minute != 0:
                    slots_now = list(row.get("q15") or [])
                    fpv_h = float(pv_merged[h]) if h < len(pv_merged) else 0.0
                    flo_h = float(load_merged[h]) if h < len(load_merged) else 0.0
                    sa_timer = sa_discharge_timer_for_hour(rules, h, cfg=cfg)
                    # Prefer opt slots from committed q15 shape for blend physics.
                    opt_slots = []
                    for s in slots_now:
                        opt_slots.append({
                            "quarter": int(s.get("quarter", 0)),
                            "pv": float(s.get("production") or 0),
                            "load": float(s.get("consumption") or 0),
                            "grid_import": float(s.get("grid_import") or 0),
                            "grid_export": float(s.get("grid_export") or 0),
                            "battery_delta": float(s.get("battery") or 0),
                            "soc_pct": float(s.get("soc") or 0),
                            "grid_charge_kw": 0.0,
                            "battery_export_kwh": float(s.get("grid_export") or 0),
                        })
                    blended_q15 = build_blended_current_hour_q15(
                        h,
                        now,
                        forecast_pv_q15=forecast_pv_q15,
                        forecast_load_q15=forecast_load_q15,
                        series_10min=series_10min,
                        soc_start_kwh=plan_start_soc_kwh,
                        opt_slots=opt_slots,
                        cfg=cfg,
                        pv_hourly=fpv_h,
                        load_hourly=flo_h,
                        sa_timer_txt=sa_timer or None,
                    )
                    pv_blend = round(sum(float(s.get("production") or 0) for s in blended_q15), 3)
                    load_blend = round(sum(float(s.get("consumption") or 0) for s in blended_q15), 3)
                    soc_blend = float(blended_q15[-1].get("soc") or initial_soc_pct)
                    sync_blended_current_hour_row(
                        row,
                        blended_q15,
                        production=pv_blend,
                        consumption=load_blend,
                        soc=soc_blend,
                        cfg=cfg,
                        epsilon=epsilon,
                        hour=h,
                        opt_slots=opt_slots,
                        sa_timer_txt=sa_timer or None,
                        now=now,
                    )
                    # Never let blend clear the committed Timer Schedule.
                    row["timer_schedule"] = committed_hour.get("timer_schedule", "")
                    row["action"] = committed_hour.get("action", "")
                    row["hour_labels_locked"] = True
                    # Current-hour UI may show live blend; forward hours must
                    # chain from the *planned* end-of-hour SOC (same seed as
                    # smart_today from_hour+1) — not from live blend.
                    blended_anchor_kwh = (
                        committed_end_soc
                        if committed_end_soc is not None
                        else (soc_blend / 100.0) * battery_cap
                    )
                    blended_row_idx = len(all_rows)
                elif committed_end_soc is not None:
                    blended_anchor_kwh = committed_end_soc
                    blended_row_idx = len(all_rows)
                all_rows.append(row)
                if row.get("export_planned"):
                    export_hours.add(h)
                remaining -= 1
                continue

            slots = (smart_today or {}).get("q15_by_hour", {}).get(h) or []
            if not slots:
                if h == 0 and plan_from_hour == 0:
                    prev_day_hourly = live_metrics.get("prev_day_hourly")
                    if prev_day_hourly:
                        dt0 = datetime.strptime(today_str, "%Y-%m-%d").replace(hour=0)
                        disp_pv, disp_load = _hourly_forecast_kwh(
                            dt0, today_date, pv_merged, pv_tomorrow, load_merged, load_tomorrow,
                        )
                        q0 = quarters_by_date.get(today_str) or []
                        rce_vals = [float(v) for v in q0[0:4] if v is not None]
                        rce_h0 = round(sum(rce_vals) / len(rce_vals), 4) if rce_vals else None
                        carry = build_h0_carryover_row(
                            today_str,
                            prev_day_hourly,
                            forecast_pv=disp_pv,
                            forecast_load=disp_load,
                            cfg=cfg,
                            params=params,
                            rce_price=rce_h0,
                        )
                        if carry:
                            all_rows.append(carry)
                            remaining -= 1
                continue
            dt = datetime.strptime(today_str, "%Y-%m-%d").replace(hour=h)
            disp_pv, disp_load = _hourly_forecast_kwh(
                dt, today_date, pv_merged, pv_tomorrow, load_merged, load_tomorrow,
            )
            row = build_smart_plan_hour_row(
                dt,
                slots,
                cfg=cfg,
                epsilon=epsilon,
                display_pv=disp_pv,
                display_load=disp_load,
                manual_timer_schedule=(
                    today_timer_ov[h] if h in today_timer_ov else None
                ),
                not_before_min=(
                    quarter_start_minute(now) if h == plan_from_hour else None
                ),
            )
            if h == plan_from_hour:
                slots_now = (smart_today or {}).get("q15_by_hour", {}).get(h) or []
                fpv_h = float(pv_merged[h]) if h < len(pv_merged) else 0.0
                flo_h = float(load_merged[h]) if h < len(load_merged) else 0.0
                sa_timer = sa_discharge_timer_for_hour(rules, h, cfg=cfg)
                blended_q15 = build_blended_current_hour_q15(
                    h,
                    now,
                    forecast_pv_q15=forecast_pv_q15,
                    forecast_load_q15=forecast_load_q15,
                    series_10min=series_10min,
                    soc_start_kwh=plan_start_soc_kwh,
                    opt_slots=slots_now,
                    cfg=cfg,
                    pv_hourly=fpv_h,
                    load_hourly=flo_h,
                    sa_timer_txt=sa_timer or None,
                )
                pv_blend = round(sum(float(s.get("production") or 0) for s in blended_q15), 3)
                load_blend = round(sum(float(s.get("consumption") or 0) for s in blended_q15), 3)
                soc_blend = float(blended_q15[-1].get("soc") or initial_soc_pct)
                sync_blended_current_hour_row(
                    row,
                    blended_q15,
                    production=pv_blend,
                    consumption=load_blend,
                    soc=soc_blend,
                    cfg=cfg,
                    epsilon=epsilon,
                    hour=h,
                    opt_slots=slots_now,
                    sa_timer_txt=sa_timer or None,
                    now=now,
                )
                # Forward SOC from optimizer forecast EOH for this hour (quarterly
                # replan seed), not from the live-blended display SOC.
                planned_eoh = _smart_plan_hour_end_soc_kwh(
                    smart_today, h, battery_cap,
                )
                blended_anchor_kwh = (
                    planned_eoh
                    if planned_eoh is not None
                    else (soc_blend / 100.0) * battery_cap
                )
                blended_row_idx = len(all_rows)
            all_rows.append(row)
            if row.get("export_planned"):
                export_hours.add(h)
            remaining -= 1

    if smart_tomorrow and remaining > 0:
        for h in range(24):
            if remaining <= 0:
                break
            slots = smart_tomorrow["q15_by_hour"].get(h) or []
            if not slots:
                continue
            dt = datetime.strptime(tomorrow_str, "%Y-%m-%d").replace(hour=h)
            disp_pv, disp_load = _hourly_forecast_kwh(
                dt, today_date, pv_merged, pv_tomorrow, load_merged, load_tomorrow,
            )
            row = build_smart_plan_hour_row(
                dt,
                slots,
                cfg=cfg,
                epsilon=epsilon,
                display_pv=disp_pv,
                display_load=disp_load,
                manual_timer_schedule=(
                    tomorrow_timer_ov[h] if h in tomorrow_timer_ov else None
                ),
            )
            all_rows.append(row)
            if row.get("export_planned"):
                export_hours.add(h)
            remaining -= 1

    if blended_anchor_kwh is not None and blended_row_idx is not None:
        replay_forward_soc_on_rows(
            all_rows[blended_row_idx + 1:],
            anchor_soc_kwh=blended_anchor_kwh,
            q15_plan_by_date={
                today_str: (smart_today or {}).get("q15_by_hour") or {},
                tomorrow_str: (smart_tomorrow or {}).get("q15_by_hour") or {},
            },
            pv_q15_by_date={
                today_str: merged_pv_q15_today,
                tomorrow_str: merged_pv_q15_tomorrow,
            },
            load_q15_by_date={
                today_str: merged_load_q15_today,
                tomorrow_str: merged_load_q15_tomorrow,
            },
            cfg=cfg,
        )

    def _soc_q15_from_q15_by_hour(plan: dict[str, Any] | None) -> list[float | None]:
        """Full-day q15 SOC from the optimizer (today chart plan line)."""
        out: list[float | None] = [None] * 96
        if not plan:
            return out
        q15_by_hour = plan.get("q15_by_hour") or {}
        for h in range(24):
            slots = q15_by_hour.get(h) or []
            for slot in slots:
                q = int(slot.get("quarter", 0))
                if not (0 <= q < 4):
                    continue
                idx = h * 4 + q
                v = slot.get("soc_pct")
                out[idx] = round(float(v), 1) if v is not None else None
        return out

    # Chart SOC plan = one full-day simulator run as if 00:00 (day-start SOC +
    # day forecast). Mid-day pipeline freezes this series; live smart_today above
    # is only for EA rows / timers from plan_from_hour.
    day_plan_for_soc = run_day_smart_q15_plan(
        date_str=today_str,
        pv_hourly=pv_forecast_today,
        load_hourly=load_forecast_today,
        tomorrow_pv=pv_tomorrow,
        tomorrow_load=load_tomorrow,
        cfg=cfg,
        rce_quarters=rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None,
        initial_soc_kwh=day_start_soc,
        from_hour=0,
    )
    # Manual Timer Schedule overrides (incl. past-hour data patches) must feed
    # the solid SOC curve, not only the rolling EA plan.
    if today_timer_ov:
        day_plan_for_soc = apply_plan_timer_overrides_if_any(
            day_plan_for_soc,
            date_str=today_str,
            pv_hourly=pv_forecast_today,
            load_hourly=load_forecast_today,
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=0,
            rce_quarters=rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None,
            gap_mode="idle",
        )
    today_plan_soc = _soc_q15_from_q15_by_hour(day_plan_for_soc)

    schedule_from = (
        now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        if actual_step0 else start_dt
    )
    q15_schedule_rows = collect_q15_schedule_rows(
        smart_today=smart_today,
        smart_tomorrow=smart_tomorrow,
        today_str=today_str,
        tomorrow_str=tomorrow_str,
        from_dt=schedule_from,
    )
    timer_schedule = derive_timer_schedule_q15(q15_schedule_rows, cfg, rules)

    history_rows: list[dict] = []
    if today_hourly:
        history_rows = build_completed_history_rows(
            today_str,
            plan_from_hour,
            today_hourly,
            quarters_by_date,
            cfg,
            params,
        )
        # Seed only for empty/new-day SQLite. Same-day history is attached later
        # via attach_immutable_history and never overwritten on rebuild.

    rows = all_rows
    today_plan_rows = [r for r in all_rows if r.get("plan_date") == today_str]
    # TOTAL = completed actuals + remaining today plan (matches visible today rows).
    today_totals = compute_plan_totals(history_rows + today_plan_rows)
    delta = compute_balance_delta(forecast, live_metrics, cfg)

    return {
        "rows": rows,
        "history_rows": history_rows,
        "has_history_rows": bool(history_rows),
        "plan_from_hour": plan_from_hour,
        "live_soc_pct": round(initial_soc_pct, 1),
        "battery_capacity_kwh": battery_cap,
        "today_date": today_str,
        "plan_soc_q15": {
            "today": today_plan_soc,
            "tomorrow": _soc_q15_from_q15_by_hour(smart_tomorrow),
        },
        # Candidate for lock; plan_simulation freezes today once locked.
        "plan_soc_day_locked": False,
        "totals": today_totals,
        "tomorrow_remainder_rows": tomorrow_remainder_rows,
        "has_tomorrow_remainder": bool(tomorrow_remainder_rows),
        "delta_kwh": delta,
        "plan_charge": delta < 0,
        "plan_export_hours": sorted(export_hours),
        "forecast_tomorrow": {
            "pv_total": round(float(forecast["tomorrow"]["pv_total"]), 2),
            "load_total": round(float(forecast["tomorrow"]["load_total"]), 2),
            "balance_kwh": round(
                float(forecast["tomorrow"]["pv_total"]) - float(forecast["tomorrow"]["load_total"]),
                2,
            ),
        },
        "proposed_schedule": timer_schedule,
        "g12_tariff_name": cfg["grid"]["g12"].get("tariff_name", "G12"),
    }


def run_simulation(
    forecast: dict[str, Any],
    live_metrics: dict[str, Any],
    rules: dict[str, Any],
    cfg: dict,
    rce_prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rolling energy arbitrage plan (same core as Rules / Debug smart today)."""
    return build_energy_arbitrage_plan(
        forecast, live_metrics, rules, cfg, rce_prices=rce_prices,
    )


def compute_balance_delta(
    forecast: dict[str, Any],
    live_metrics: dict[str, Any],
    cfg: dict,
) -> float:
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    soc_pct = float(live_metrics.get("battery_soc", 50.0))
    soc_kwh = (soc_pct / 100.0) * battery_cap

    overrides = cfg.get("overrides", {})
    pv_tomorrow = (
        float(overrides["tomorrow_pv_kwh"])
        if overrides.get("tomorrow_pv_kwh") is not None
        else forecast["tomorrow"]["pv_total"]
    )
    load_tomorrow = (
        float(overrides["tomorrow_load_kwh"])
        if overrides.get("tomorrow_load_kwh") is not None
        else forecast["tomorrow"]["load_total"]
    )
    return round((pv_tomorrow + soc_kwh) - load_tomorrow, 3)


compute_nightly_delta = compute_balance_delta


def _tomorrow_remainder_rows(
    *,
    start_dt: datetime,
    horizon_hours: int,
    today_date,
    pv_tomorrow: list[float],
    load_tomorrow: list[float],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
) -> list[dict[str, Any]]:
    """Rows for the *uncalculated* remainder of tomorrow after the 24h horizon.

    This is a UI affordance only: show PV/Load + prices for the rest of tomorrow,
    without any simulated flows, SOC, cost, or actions.
    """
    tomorrow_date = (start_dt + timedelta(days=1)).date()
    tomorrow_str = (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    last_dt = start_dt + timedelta(hours=max(0, horizon_hours - 1))
    if last_dt.date() > tomorrow_date:
        return []

    if last_dt.date() == tomorrow_date:
        start_hour = last_dt.hour + 1
    else:
        start_hour = 0

    if start_hour > 23:
        return []

    quarters = quarters_by_date.get(tomorrow_str) or []
    rows: list[dict[str, Any]] = []
    for h in range(start_hour, 24):
        dt = datetime.strptime(tomorrow_str, "%Y-%m-%d").replace(hour=h)
        pv_h = float(pv_tomorrow[h]) if h < len(pv_tomorrow) else 0.0
        load_h = float(load_tomorrow[h]) if h < len(load_tomorrow) else 0.0
        buy_price, g12_zone = get_buy_price(dt, cfg)
        chunk = quarters[h * Q15_PER_HOUR:(h + 1) * Q15_PER_HOUR]
        vals = [float(v) for v in chunk if v is not None]
        rce_price = round(sum(vals) / len(vals), 4) if vals else None
        rce_q15 = list(chunk) if chunk else [None] * Q15_PER_HOUR
        while len(rce_q15) < Q15_PER_HOUR:
            rce_q15.append(None)

        rows.append(
            {
                "hour": h,
                "plan_date": tomorrow_str,
                "start": interval_end_label(dt),
                "production": round(pv_h, 3),
                "consumption": round(load_h, 3),
                "battery": None,
                "bat_charge": None,
                "bat_discharge": None,
                "grid_import": None,
                "grid_export": None,
                "soc": None,
                "import_cost": None,
                "export_revenue": None,
                "energy_cost": None,
                "service_cost": None,
                "cost": None,
                "action": "",
                "timer_schedule": "",
                "rce_price": rce_price,
                "rce_q15": rce_q15,
                "export_credit": None,
                "g12_zone": g12_zone,
                "buy_price": round(buy_price, 4),
                "export_planned": False,
                "uncalculated": True,
            }
        )
    return rows
