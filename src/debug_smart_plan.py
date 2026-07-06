"""Bill-minimizing smart plan for debug replay (G12 buy + RCE export, 15-min steps)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .config import load_config
from .g12_pricing import get_buy_price
from .plan_optimizer import HourControl, optimize_horizon, reserve_soc_per_step, simulate_hour
from .plan_hourly_actuals import interval_end_label
from .simulation_config import (
    get_simulation_params,
    merge_simulation_defaults,
    plan_min_soc_kwh,
    plan_reserve_min_soc_kwh,
    plan_timer_discharge_ac_kw,
)
from .timer_plan import (
    classify_action as timer_classify_action,
    build_hour_timer_schedule,
    derive_timer_schedule_q15,
    summarize_hour_actions_debug,
)

Q15_PER_HOUR = 4
DAY_STEPS = 24 * Q15_PER_HOUR
STEP_SCALE = 1.0 / Q15_PER_HOUR


def hourly_rows_from_pv_load(
    pv_hourly: list[float],
    load_hourly: list[float],
) -> list[dict[str, Any]]:
    """Build optimizer horizon rows from hourly PV/load kWh lists."""
    rows: list[dict[str, Any]] = []
    for h in range(24):
        pv = float(pv_hourly[h]) if h < len(pv_hourly) else 0.0
        load = float(load_hourly[h]) if h < len(load_hourly) else 0.0
        rows.append({
            "hour": h,
            "pv": pv,
            "load": load,
            "skipped": False,
        })
    return rows


def hourly_rows_from_accruals(hourly: dict[str, Any]) -> list[dict[str, Any]]:
    """Build horizon rows from Influx hourly dict."""
    pv_s = hourly.get("pv") or [None] * 24
    load_s = hourly.get("load") or [None] * 24
    pv = [float(v) if v is not None else 0.0 for v in pv_s]
    load = [float(v) if v is not None else 0.0 for v in load_s]
    return hourly_rows_from_pv_load(pv, load)


def _split_energy_hourly_to_q15(values: list[float]) -> list[float]:
    """Split hourly kWh into four equal 15-min energy steps."""
    out: list[float] = []
    for v in values:
        quarter = float(v) * STEP_SCALE
        out.extend([quarter] * Q15_PER_HOUR)
    return out


def _hourly_arrays(rows: list[dict[str, Any]], date_str: str, cfg: dict) -> tuple[list[float], list[float], list[float]]:
    pv = [0.0] * 24
    load = [0.0] * 24
    buy = [0.0] * 24
    base = datetime.strptime(date_str, "%Y-%m-%d")

    for row in rows:
        h = row.get("hour")
        if h is None or not (0 <= int(h) < 24):
            continue
        hi = int(h)
        if row.get("skipped"):
            continue
        pv[hi] = float(row.get("pv") or 0.0)
        load[hi] = float(row.get("load") or 0.0)
        if row.get("buy_price") is not None:
            buy[hi] = float(row["buy_price"])
        else:
            buy[hi] = get_buy_price(base.replace(hour=hi), cfg)[0]

    for h in range(24):
        if buy[h] == 0.0:
            buy[h] = get_buy_price(base.replace(hour=h), cfg)[0]

    return pv, load, buy


def _resolve_rce_quarters(
    date_str: str,
    rce_quarters: list[float | None] | None,
) -> list[float | None]:
    if rce_quarters and len(rce_quarters) >= DAY_STEPS:
        return list(rce_quarters[:DAY_STEPS])
    from . import rce as rce_mod

    fetched = rce_mod.quarter_rce_for_dates(date_str).get(date_str)
    if fetched and len(fetched) >= DAY_STEPS:
        return list(fetched[:DAY_STEPS])
    return [None] * DAY_STEPS


def _tomorrow_hourly(rows: list[dict[str, Any]]) -> tuple[list[float], list[float], float, float]:
    pv = [0.0] * 24
    load = [0.0] * 24
    for row in rows:
        if row.get("skipped"):
            continue
        h = row.get("hour")
        if h is None or not (0 <= int(h) < 24):
            continue
        hi = int(h)
        pv[hi] = float(row.get("pv") or 0.0)
        load[hi] = float(row.get("load") or 0.0)
    return pv, load, round(sum(pv), 3), round(sum(load), 3)


def timer_schedule_by_hour(
    q15_by_hour: dict[int, list[dict[str, Any]]],
    cfg: dict,
    epsilon: float,
) -> dict[int, str]:
    """Per-hour Timer Schedule from q15 slots (shared by debug replay and Energy arbitrage)."""
    timer_by_hour: dict[int, str] = {}
    for h in range(24):
        slots = q15_by_hour.get(h) or []
        timer_by_hour[h] = build_hour_timer_schedule(h, slots, cfg, epsilon=epsilon)
    return timer_by_hour


def merge_today_hourly_profile(
    forecast_pv: list[float],
    forecast_load: list[float],
    today_hourly: dict[str, Any] | None,
    *,
    until_hour: int | None = None,
) -> tuple[list[float], list[float]]:
    """Actual completed hours from Influx; forecast fill for the rest.

    When *until_hour* is set, only hours ``h < until_hour`` take Influx values
    (PROD: completed history). Current hour and later keep forecast for the optimizer.
    """
    pv = [float(v) for v in forecast_pv]
    load = [float(v) for v in forecast_load]
    if not today_hourly:
        return pv, load
    cap = 24 if until_hour is None else max(0, min(24, int(until_hour)))
    pv_src = today_hourly.get("pv") or []
    load_src = today_hourly.get("load") or []
    for h in range(min(cap, len(pv))):
        if h < len(pv_src) and pv_src[h] is not None:
            pv[h] = float(pv_src[h])
        if h < len(load_src) and load_src[h] is not None:
            load[h] = float(load_src[h])
    return pv, load


def run_day_smart_q15_plan(
    *,
    date_str: str,
    pv_hourly: list[float],
    load_hourly: list[float],
    tomorrow_pv: list[float],
    tomorrow_load: list[float],
    cfg: dict,
    rce_quarters: list[float | None] | None = None,
    initial_soc_kwh: float,
    from_hour: int = 0,
) -> dict[str, Any] | None:
    """15-min optimizer replay from *from_hour* through end of day (shared by debug and PROD)."""
    cfg = merge_simulation_defaults(cfg)
    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    discharge_ac_kw = plan_timer_discharge_ac_kw(cfg)
    epsilon = float(params["epsilon_kwh"])
    eps_q = max(epsilon * STEP_SCALE, 0.001)
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])

    if sum(pv_hourly) + sum(load_hourly) <= epsilon:
        return None

    from_hour = max(0, min(23, int(from_hour)))
    start_step = from_hour * Q15_PER_HOUR

    rce_q = _resolve_rce_quarters(date_str, rce_quarters)
    pv_q_full = _split_energy_hourly_to_q15(pv_hourly)
    load_q_full = _split_energy_hourly_to_q15(load_hourly)
    buy1 = [0.0] * 24
    base = datetime.strptime(date_str, "%Y-%m-%d")
    for h in range(24):
        buy1[h] = get_buy_price(base.replace(hour=h), cfg)[0]
    buy_q_full = [b for b in buy1 for _ in range(Q15_PER_HOUR)]

    pv_q = pv_q_full[start_step:]
    load_q = load_q_full[start_step:]
    buy_q = buy_q_full[start_step:]
    steps = len(pv_q)

    start_dt = datetime.strptime(date_str, "%Y-%m-%d")
    today_date = start_dt.date()
    end_dt = start_dt.replace(hour=23, minute=45)
    rce_map: dict[tuple[str, int], float | None] = {}
    for h in range(24):
        chunk = rce_q[h * Q15_PER_HOUR:(h + 1) * Q15_PER_HOUR]
        vals = [float(v) for v in chunk if v is not None]
        rce_map[(date_str, h)] = round(sum(vals) / len(vals), 4) if vals else None

    pv2_total = round(sum(tomorrow_pv), 3)
    load2_total = round(sum(tomorrow_load), 3)
    forecast: dict[str, Any] = {
        "today": {
            "pv": pv_hourly,
            "load": load_hourly,
            "pv_total": round(sum(pv_hourly), 3),
            "load_total": round(sum(load_hourly), 3),
        },
        "tomorrow": {
            "pv": tomorrow_pv,
            "load": tomorrow_load,
            "pv_total": pv2_total,
            "load_total": load2_total,
        },
    }

    controls = optimize_horizon(
        steps=steps,
        pv_series=pv_q,
        load_series=load_q,
        buy_prices=buy_q,
        rce_series=rce_q,
        initial_soc_kwh=initial_soc_kwh,
        cfg=cfg,
        params=params,
        end_dt=end_dt,
        today_date=today_date,
        rce_map=rce_map,
        forecast=forecast,
        step_scale=STEP_SCALE,
        rce_step_offset=start_step,
    )

    reserves = reserve_soc_per_step(
        steps, pv_q, load_q,
        reserve_floor_kwh=plan_reserve_min_soc_kwh(cfg),
        eta_out=eta_out, eta_pv_load=eta_pv_load, epsilon=epsilon,
        step_scale=STEP_SCALE, end_dt=end_dt, today_date=today_date, forecast=forecast,
    )

    soc = initial_soc_kwh
    q15_plan_rows: list[dict[str, Any]] = []
    q15_by_hour: dict[int, list[dict[str, Any]]] = {h: [] for h in range(24)}

    for step in range(steps):
        global_step = start_step + step
        h = global_step // Q15_PER_HOUR
        quarter = global_step % Q15_PER_HOUR
        ctrl = controls[step] if step < len(controls) else HourControl(0.0, 0.0)
        phys = simulate_hour(
            soc, pv_q[step], load_q[step], ctrl,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=discharge_ac_kw * STEP_SCALE,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            epsilon=eps_q,
            reserve_soc_kwh=reserves[step],
        )
        batt_exp = min(ctrl.battery_export_kwh, phys.grid_export)
        soc_pct = (phys.soc_end / battery_cap) * 100.0 if battery_cap else 0.0
        action = timer_classify_action(
            bat_charge=max(0.0, phys.battery_delta),
            bat_discharge=max(0.0, -phys.battery_delta),
            grid_import=phys.grid_import,
            grid_export=phys.grid_export,
            production=pv_q[step],
            epsilon=eps_q,
        )
        dt = start_dt + timedelta(hours=h, minutes=quarter * 15)
        slot = {
            "hour": h,
            "quarter": quarter,
            "action": action,
            "pv": pv_q[step],
            "load": load_q[step],
            "grid_import": phys.grid_import,
            "grid_export": phys.grid_export,
            "battery_delta": phys.battery_delta,
            "battery_export_kwh": batt_exp,
            "grid_charge_kw": ctrl.grid_charge_kw,
            "load_from_grid": ctrl.load_from_grid,
            "ctrl_battery_export_kwh": ctrl.battery_export_kwh,
            "soc_pct": soc_pct,
            "soc_end": phys.soc_end,
            "reserve_kwh": reserves[step],
            "rce": rce_q[global_step] if global_step < len(rce_q) else None,
        }
        q15_by_hour[h].append(slot)
        q15_plan_rows.append({
            "start": dt.strftime("%d-%m-%Y %H:%M"),
            "hour": h,
            "action": action,
            "soc": soc_pct,
            "grid_import": round(phys.grid_import, 4),
            "grid_export": round(phys.grid_export, 4),
            "battery": round(phys.battery_delta, 4),
        })
        soc = phys.soc_end

    return {
        "q15_by_hour": q15_by_hour,
        "q15_plan_rows": q15_plan_rows,
        "end_soc_kwh": round(soc, 3),
        "timer_schedule": derive_timer_schedule_q15(q15_plan_rows, cfg),
        "epsilon": epsilon,
    }


def _merge_smart_day_plans(
    head: dict[str, Any],
    tail: dict[str, Any],
    *,
    from_hour: int,
    cfg: dict,
) -> dict[str, Any]:
    """Completed hours from head replay; current hour onward from live-SOC tail."""
    from_hour = max(0, min(24, int(from_hour)))
    q15_by_hour: dict[int, list[dict[str, Any]]] = {h: [] for h in range(24)}
    head_q15 = head.get("q15_by_hour") or {}
    tail_q15 = tail.get("q15_by_hour") or {}
    for h in range(from_hour):
        q15_by_hour[h] = list(head_q15.get(h) or [])
    for h in range(from_hour, 24):
        q15_by_hour[h] = list(tail_q15.get(h) or [])

    q15_plan_rows: list[dict[str, Any]] = [
        r for r in (head.get("q15_plan_rows") or [])
        if int(r.get("hour", 0)) < from_hour
    ]
    q15_plan_rows.extend(
        r for r in (tail.get("q15_plan_rows") or [])
        if int(r.get("hour", 0)) >= from_hour
    )

    return {
        "q15_by_hour": q15_by_hour,
        "q15_plan_rows": q15_plan_rows,
        "end_soc_kwh": tail["end_soc_kwh"],
        "timer_schedule": derive_timer_schedule_q15(q15_plan_rows, cfg),
        "epsilon": tail.get("epsilon", head.get("epsilon")),
    }


def build_history_hour_timer_schedule(
    hour: int,
    row: dict[str, Any],
    *,
    date_str: str,
    pv_hourly: list[float],
    load_hourly: list[float],
    tomorrow_pv: list[float],
    tomorrow_load: list[float],
    today_hourly: dict[str, list] | None,
    cfg: dict,
    rce_quarters: list[float | None] | None,
    battery_cap: float,
    min_soc_pct: float,
) -> str:
    """Timer Schedule for a completed hour: Influx SOC at hour start + optimizer reserve.

    PV/load use the forecast profile (as at plan time), not merged actuals for later hours.
    Timer lines reflect actual meter flows — grid-to-load without battery charge gets no Chg slot.
    """
    from .plan_hourly_actuals import hour_start_soc_kwh
    from .timer_plan import ACTION_CHARGE_GRID, ACTION_DISCHARGE_GRID, classify_action

    params = get_simulation_params(cfg)
    epsilon = float(params["epsilon_kwh"])
    actual_action = classify_action(
        bat_charge=float(row.get("bat_charge") or 0),
        bat_discharge=float(row.get("bat_discharge") or 0),
        grid_import=float(row.get("grid_import") or 0),
        grid_export=float(row.get("grid_export") or 0),
        production=float(row.get("production") or 0),
        epsilon=epsilon,
    )
    row["action"] = actual_action

    if actual_action == ACTION_CHARGE_GRID:
        if float(row.get("bat_charge") or 0) <= epsilon:
            return ""
    elif actual_action == ACTION_DISCHARGE_GRID:
        if float(row.get("grid_export") or 0) <= epsilon:
            return ""
    else:
        return ""

    start_soc = hour_start_soc_kwh(today_hourly, hour, battery_cap, min_soc_pct)
    if start_soc is None:
        return ""

    plan = run_day_smart_q15_plan(
        date_str=date_str,
        pv_hourly=pv_hourly,
        load_hourly=load_hourly,
        tomorrow_pv=tomorrow_pv,
        tomorrow_load=tomorrow_load,
        cfg=cfg,
        rce_quarters=rce_quarters,
        initial_soc_kwh=start_soc,
        from_hour=hour,
    )
    if not plan:
        return ""
    slots = (plan.get("q15_by_hour") or {}).get(hour) or []
    return build_hour_timer_schedule(
        hour,
        slots,
        cfg,
        epsilon=epsilon,
        action=actual_action,
        grid_export=float(row.get("grid_export") or 0),
        bat_charge=float(row.get("bat_charge") or 0),
    )


def run_today_smart_q15_plan(
    *,
    date_str: str,
    pv_hourly: list[float],
    load_hourly: list[float],
    tomorrow_pv: list[float],
    tomorrow_load: list[float],
    cfg: dict,
    rce_quarters: list[float | None] | None = None,
    plan_from_hour: int,
    day_start_soc_kwh: float,
    live_soc_kwh: float,
) -> dict[str, Any] | None:
    """Today's smart plan: live SOC at plan_from_hour; earlier hours unchanged replay."""
    plan_from_hour = max(0, min(23, int(plan_from_hour)))
    common = dict(
        date_str=date_str,
        pv_hourly=pv_hourly,
        load_hourly=load_hourly,
        tomorrow_pv=tomorrow_pv,
        tomorrow_load=tomorrow_load,
        cfg=cfg,
        rce_quarters=rce_quarters,
    )

    if plan_from_hour <= 0:
        return run_day_smart_q15_plan(
            **common,
            initial_soc_kwh=live_soc_kwh,
            from_hour=0,
        )

    head = run_day_smart_q15_plan(
        **common,
        initial_soc_kwh=day_start_soc_kwh,
        from_hour=0,
    )
    tail = run_day_smart_q15_plan(
        **common,
        initial_soc_kwh=live_soc_kwh,
        from_hour=plan_from_hour,
    )
    if tail is None:
        return head
    if head is None:
        return tail
    return _merge_smart_day_plans(head, tail, from_hour=plan_from_hour, cfg=cfg)


def _slot_to_q15_plan_row(dt: datetime, hour: int, slot: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": dt.strftime("%d-%m-%Y %H:%M"),
        "hour": hour,
        "action": slot.get("action"),
        "soc": slot.get("soc_pct"),
        "grid_import": round(float(slot.get("grid_import") or 0), 4),
        "grid_export": round(float(slot.get("grid_export") or 0), 4),
        "battery": round(float(slot.get("battery_delta") or 0), 4),
    }


def collect_q15_schedule_rows(
    *,
    smart_today: dict[str, Any] | None,
    smart_tomorrow: dict[str, Any] | None,
    today_str: str,
    tomorrow_str: str,
    from_dt: datetime,
) -> list[dict[str, Any]]:
    """q15 rows for SA timer compression — only steps at or after from_dt."""
    cutoff = from_dt.replace(second=0, microsecond=0)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.replace(tzinfo=None)
    rows: list[dict[str, Any]] = []

    def _append_day(date_str: str, plan: dict[str, Any] | None) -> None:
        if not plan:
            return
        base = datetime.strptime(date_str, "%Y-%m-%d")
        q15_by_hour = plan.get("q15_by_hour") or {}
        for h in range(24):
            for slot in q15_by_hour.get(h) or []:
                q = int(slot.get("quarter", 0))
                dt = base.replace(hour=h, minute=q * 15)
                if dt < cutoff:
                    continue
                rows.append(_slot_to_q15_plan_row(dt, h, slot))

    _append_day(today_str, smart_today)
    _append_day(tomorrow_str, smart_tomorrow)
    return rows


def build_smart_plan_hour_row(
    dt: datetime,
    slots: list[dict[str, Any]],
    *,
    cfg: dict,
    epsilon: float,
    display_pv: float | None = None,
    display_load: float | None = None,
    manual_timer_schedule: str | None = None,
) -> dict[str, Any]:
    """Hourly PROD row from smart q15 slots (same action/timer logic as debug)."""
    from .plan_cost import hour_grid_cash_pln

    hour = dt.hour
    date_str = dt.strftime("%Y-%m-%d")
    buy_price, g12_zone = get_buy_price(dt, cfg)

    production = display_pv if display_pv is not None else sum(float(s["pv"]) for s in slots)
    consumption = display_load if display_load is not None else sum(float(s["load"]) for s in slots)
    battery = sum(float(s["battery_delta"]) for s in slots)
    bat_charge = sum((max(0.0, float(s["battery_delta"])) for s in slots))
    bat_discharge = sum((max(0.0, -float(s["battery_delta"])) for s in slots))
    grid_import = sum(float(s["grid_import"]) for s in slots)
    grid_export = sum(float(s["grid_export"]) for s in slots)
    soc_pct = float(slots[-1]["soc_pct"]) if slots else 0.0

    rce_vals = [s["rce"] for s in slots if s.get("rce") is not None]
    rce_price = round(sum(float(v) for v in rce_vals) / len(rce_vals), 4) if rce_vals else None
    rce_q15: list[float | None] = []
    for s in slots:
        rv = s.get("rce")
        rce_q15.append(round(float(rv), 4) if rv is not None else None)
    while len(rce_q15) < 4:
        rce_q15.append(None)

    batt_exp = sum(float(s.get("battery_export_kwh") or 0.0) for s in slots)
    cash = hour_grid_cash_pln(
        grid_import, grid_export, buy_price, rce_price, cfg,
        battery_export=batt_exp,
        g12_zone=g12_zone,
    )

    action = summarize_hour_actions_debug(slots, hour, cfg, epsilon=epsilon)
    from .plan_hourly_actuals import ea_q15_from_optimizer_slots
    from .simulation_config import plan_min_soc_pct

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    return {
        "hour": hour,
        "plan_date": date_str,
        "start": interval_end_label(dt),
        "q15": ea_q15_from_optimizer_slots(
            slots, battery_cap, min_soc_pct=plan_min_soc_pct(cfg),
        ),
        "production": round(production, 3),
        "consumption": round(consumption, 3),
        "battery": round(battery, 3),
        "bat_charge": round(bat_charge, 3),
        "bat_discharge": round(bat_discharge, 3),
        "grid_import": round(grid_import, 3),
        "grid_export": round(grid_export, 3),
        "soc": round(soc_pct, 1),
        "import_cost": cash["import_cost"],
        "export_revenue": cash["export_revenue"],
        "energy_cost": cash["energy_cost"],
        "service_cost": cash["service_cost"],
        "cost": cash["cost"],
        "action": action,
        "timer_schedule": (
            manual_timer_schedule
            if manual_timer_schedule is not None
            else build_hour_timer_schedule(
                hour, slots, cfg, epsilon=epsilon, action=action, grid_export=grid_export,
            )
        ),
        "timer_schedule_manual": manual_timer_schedule is not None,
        "rce_price": rce_price,
        "rce_q15": rce_q15,
        "export_credit": cash["export_credit"],
        "g12_zone": g12_zone,
        "buy_price": round(buy_price, 4),
        "export_planned": batt_exp > epsilon,
    }


def apply_smart_plan_for_day(
    day: dict[str, Any],
    tomorrow_rows: list[dict[str, Any]],
    date_str: str,
    cfg: dict | None = None,
    rce_quarters: list[float | None] | None = None,
    initial_soc_kwh: float | None = None,
    plan_from_hour: int | None = None,
    live_soc_kwh: float | None = None,
) -> float | None:
    """Attach action, smart flows, and timer schedule to hourly rows (15-min optimizer).

    Returns end SOC kWh after replay, or None if plan was skipped.
    """
    cfg = merge_simulation_defaults(cfg or load_config())
    params = get_simulation_params(cfg)
    rows = day.get("rows") or []
    if not rows:
        return None

    epsilon = float(params["epsilon_kwh"])
    pv1, load1, _buy1 = _hourly_arrays(rows, date_str, cfg)
    pv2, load2, _, _ = _tomorrow_hourly(tomorrow_rows)

    if initial_soc_kwh is not None:
        start_soc = float(initial_soc_kwh)
    else:
        start_soc = float(day.get("initial_soc_kwh") or plan_min_soc_kwh(cfg))

    if plan_from_hour is not None and live_soc_kwh is not None:
        plan = run_today_smart_q15_plan(
            date_str=date_str,
            pv_hourly=pv1,
            load_hourly=load1,
            tomorrow_pv=pv2,
            tomorrow_load=load2,
            cfg=cfg,
            rce_quarters=rce_quarters,
            plan_from_hour=plan_from_hour,
            day_start_soc_kwh=start_soc,
            live_soc_kwh=float(live_soc_kwh),
        )
    else:
        plan = run_day_smart_q15_plan(
            date_str=date_str,
            pv_hourly=pv1,
            load_hourly=load1,
            tomorrow_pv=pv2,
            tomorrow_load=load2,
            cfg=cfg,
            rce_quarters=rce_quarters,
            initial_soc_kwh=start_soc,
        )
    if plan is None:
        return None

    day["timer_schedule"] = plan["timer_schedule"]
    q15_by_hour = plan["q15_by_hour"]
    rce_q = _resolve_rce_quarters(date_str, rce_quarters)

    smart_by_hour: dict[int, dict[str, Any]] = {}
    action_by_hour: dict[int, str] = {}
    rce_q15_by_hour: dict[int, list[float | None]] = {}

    for h in range(24):
        slots = q15_by_hour.get(h) or []
        grid_used = round(sum(s["grid_import"] for s in slots), 3)
        grid_export = round(sum(s["grid_export"] for s in slots), 3)
        bat_charge = round(sum(max(0.0, s["battery_delta"]) for s in slots), 3)
        bat_discharge = round(sum(max(0.0, -s["battery_delta"]) for s in slots), 3)
        soc_pct = round(slots[-1]["soc_pct"], 1) if slots else 0.0
        soc_kwh = round(slots[-1]["soc_end"], 2) if slots else 0.0
        reserve_kwh = round(slots[-1]["reserve_kwh"], 2) if slots else 0.0
        headroom_kwh = round(soc_kwh - reserve_kwh, 2)

        action = summarize_hour_actions_debug(slots, h, cfg, epsilon=epsilon)
        smart_by_hour[h] = {
            "grid_used": grid_used,
            "grid_export": grid_export,
            "bat_charge": bat_charge,
            "bat_discharge": bat_discharge,
            "soc": soc_pct,
            "soc_kwh": soc_kwh,
            "reserve_kwh": reserve_kwh,
            "headroom_kwh": headroom_kwh,
        }
        action_by_hour[h] = action
        rce_q15_by_hour[h] = rce_q[h * Q15_PER_HOUR:(h + 1) * Q15_PER_HOUR]

    for row in rows:
        h = row.get("hour")
        if h is None or row.get("skipped"):
            continue
        hi = int(h)
        row["action"] = action_by_hour.get(hi, "")
        row["smart"] = smart_by_hour.get(hi)
        slots = q15_by_hour.get(hi) or []
        smart = smart_by_hour.get(hi) or {}
        row["timer_schedule"] = build_hour_timer_schedule(
            hi,
            slots,
            cfg,
            epsilon=epsilon,
            action=action_by_hour.get(hi, ""),
            grid_export=float(smart.get("grid_export") or 0),
        )
        row["rce_q15"] = rce_q15_by_hour.get(hi, [None] * Q15_PER_HOUR)

    day["smart_end_soc_kwh"] = plan["end_soc_kwh"]
    return plan["end_soc_kwh"]


def apply_smart_plan_day1(
    day: dict[str, Any],
    day2_rows: list[dict[str, Any]],
    date_str: str,
    cfg: dict | None = None,
    rce_quarters: list[float | None] | None = None,
) -> float | None:
    """Backward-compatible wrapper for day-1 smart plan."""
    return apply_smart_plan_for_day(
        day, day2_rows, date_str, cfg=cfg, rce_quarters=rce_quarters,
    )
