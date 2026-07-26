"""Ranked hourly-RCE export assignment and multi-hour window spans."""

from __future__ import annotations

from datetime import datetime

from src.grid_config import merge_grid_defaults
from src.plan_optimizer import (
    HourControl,
    assign_ranked_battery_export,
    export_span_candidates,
    export_window_roles,
    hour_rce_rating,
    hourly_avg_rce,
    optimize_horizon,
    pick_next_export_hour,
    rank_hours_by_avg_rce,
)
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
)


def _cfg(**timer_kw) -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 40.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "epsilon_kwh": 0.01,
            "losses_pct": {
                "grid_to_battery": 0.0,
                "battery_to_load_or_grid": 0.0,
                "pv_to_grid": 0.0,
                "pv_to_load": 0.0,
                "pv_to_battery": 0.0,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
        },
        "timer_schedule": {
            "min_block_minutes": 15,
            "min_hourly_transfer_kwh": 0.0,
            **timer_kw,
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def test_hourly_avg_rce_and_rank():
    rce = [0.4, 0.4, 0.4, 0.4, 0.9, 0.9, 0.9, 0.9, 0.7, 0.7, 0.7, 0.7]
    assert hourly_avg_rce(rce, 0) == 0.4
    assert hourly_avg_rce(rce, 1) == 0.9
    assert rank_hours_by_avg_rce([0, 1, 2], rce, 0.5) == [1, 2]


def test_hour_rce_rating_rounds_to_hundredths():
    # 0.901 / 0.904 → same 0.90 rating; 0.906 → 0.91
    rce = (
        [0.901] * 4
        + [0.904] * 4
        + [0.906] * 4
    )
    assert hour_rce_rating(rce, 0) == 0.90
    assert hour_rce_rating(rce, 1) == 0.90
    assert hour_rce_rating(rce, 2) == 0.91
    assert rank_hours_by_avg_rce([0, 1, 2], rce, 0.5) == [2, 0, 1]


def test_pick_next_export_hour_prefers_proximity_on_tie():
    ratings = {18: 1.0, 20: 1.0, 21: 1.0, 17: 0.8}
    # First pick among top rating: earliest
    assert pick_next_export_hour([18, 20, 21, 17], ratings, last_hour=None) == 18
    # After 18: among remaining top (20,21) pick closer → 20
    assert pick_next_export_hour([20, 21, 17], ratings, last_hour=18) == 20
    # After 20: 21 still tops 17
    assert pick_next_export_hour([21, 17], ratings, last_hour=20) == 21
    assert pick_next_export_hour([17], ratings, last_hour=21) == 17
    # After rich hour 21: equal 0.90 at 18 and 20 → prefer 20 (closer)
    ratings2 = {21: 1.0, 18: 0.9, 20: 0.9}
    assert pick_next_export_hour([18, 20], ratings2, last_hour=21) == 20


def test_equal_rating_after_rich_hour_prefers_neighbor():
    """Allocator tries the nearer equal-rating hour before a distant one.

    Static (-rating, hour) would try 16 before 20 after rich 19; proximity tries 20 first.
    """
    import src.plan_optimizer as po

    orig = po.pick_next_export_hour
    tried: list[int] = []

    def _wrap(remaining, ratings, *, last_hour):
        h = orig(remaining, ratings, last_hour=last_hour)
        tried.append(h)
        return h

    po.pick_next_export_hour = _wrap
    try:
        offset = 16 * 4
        steps = 20  # 16..20
        rce = (
            [None] * offset
            + [0.90] * 4  # 16
            + [0.40] * 4  # 17 below floor
            + [0.40] * 4  # 18 below floor
            + [1.00] * 4  # 19
            + [0.90] * 4  # 20
        )
        assert hour_rce_rating(rce, 16) == hour_rce_rating(rce, 20) == 0.90
        assert hour_rce_rating(rce, 19) == 1.0
        base = [HourControl(0.0, 0.0) for _ in range(steps)]
        assign_ranked_battery_export(
            base,
            steps=steps,
            pv_series=[0.0] * steps,
            load_series=[0.05] * steps,
            rce_series=rce,
            rce_step_offset=offset,
            step_scale=0.25,
            initial_soc_kwh=20.0,
            battery_cap=40.0,
            min_kwh=6.4,
            discharge_ac_step=8.0,
            eta_grid=1.0,
            eta_out=1.0,
            eta_pv_load=1.0,
            eta_pv_grid=1.0,
            eta_pv_battery=1.0,
            eps_step=0.01,
            reserves=[6.4] * steps,
            export_floor=0.5,
            min_hourly_kwh=0.5,
        )
    finally:
        po.pick_next_export_hour = orig

    assert tried[0] == 19
    assert tried.index(20) < tried.index(16)


def test_export_window_roles_and_spans():
    assert export_window_roles({5}) == {5: "single"}
    assert export_window_roles({5, 6, 7}) == {5: "first", 6: "middle", 7: "last"}
    assert export_span_candidates("middle") == [(0, 4)]
    assert export_span_candidates("first")[0] == (0, 4)
    assert export_span_candidates("last") == [(0, 4), (0, 3), (0, 2)]
    # Single: longest first includes full hour
    assert export_span_candidates("single")[0] == (0, 4)


def test_richer_hour_gets_export_before_cheaper():
    """With limited SOC, rank-1 hour exports more than the cheaper following hour."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    # Two hours: 18 avg 1.0, 19 avg 0.6; threshold 0.5.
    offset = 18 * 4
    steps = 8
    rce = [None] * offset + [1.0] * 4 + [0.6] * 4
    pv = [0.0] * steps
    load = [0.2] * steps
    buy = [0.5] * steps
    end = datetime(2026, 7, 18, 20, 0)
    controls = optimize_horizon(
        steps=steps,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=14.0,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.2] * 24, "pv_total": 0.0, "load_total": 4.8},
            "tomorrow": {"pv": [3.0] * 24, "load": [0.2] * 24, "pv_total": 72.0, "load_total": 4.8},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp18 = sum(c.battery_export_kwh for c in controls[:4])
    exp19 = sum(c.battery_export_kwh for c in controls[4:])
    assert exp18 > 1.0
    assert exp18 >= exp19


def test_later_richer_hour_keeps_soc_over_earlier_cheaper():
    """Cheaper hour 18 must not drain SOC needed for richer hour 19."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    offset = 18 * 4
    steps = 8
    # Hour 18 cheap, 19 expensive — limited SOC should prefer 19.
    rce = [None] * offset + [0.55] * 4 + [1.2] * 4
    end = datetime(2026, 7, 18, 20, 0)
    controls = optimize_horizon(
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.15] * steps,
        buy_prices=[0.5] * steps,
        rce_series=rce,
        initial_soc_kwh=12.0,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.15] * 24, "pv_total": 0.0, "load_total": 3.6},
            "tomorrow": {"pv": [3.0] * 24, "load": [0.15] * 24, "pv_total": 72.0, "load_total": 3.6},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp18 = sum(c.battery_export_kwh for c in controls[:4])
    exp19 = sum(c.battery_export_kwh for c in controls[4:])
    assert exp19 > 1.0
    assert exp19 >= exp18


def test_avg_below_floor_no_export():
    cfg = _cfg()
    cfg["grid"]["grid_export_threshold_pln_kwh"] = 0.62
    params = get_simulation_params(cfg)
    offset = 18 * 4
    # avg = 0.60 < 0.62
    rce = [None] * offset + [0.50, 0.70, 0.70, 0.50]
    controls = optimize_horizon(
        steps=4,
        pv_series=[0.0] * 4,
        load_series=[0.2] * 4,
        buy_prices=[0.5] * 4,
        rce_series=rce,
        initial_soc_kwh=18.0,
        cfg=cfg,
        params=params,
        end_dt=datetime(2026, 7, 18, 19, 0),
        today_date=datetime(2026, 7, 18).date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.0] * 24, "pv_total": 0.0, "load_total": 0.0},
            "tomorrow": {"pv": [0.0] * 24, "load": [0.0] * 24, "pv_total": 0.0, "load_total": 0.0},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)


def test_rank_order_middle_hour_gets_leftover_after_richer():
    """Rank 1=21, rank 2=19, rank 3=20: hour 20 is planned after 21 and 19 claims."""
    cfg = _cfg(min_hourly_transfer_kwh=0.5)
    params = get_simulation_params(cfg)
    offset = 19 * 4
    steps = 12  # hours 19,20,21
    rce = [None] * offset + [0.70] * 4 + [0.60] * 4 + [1.0] * 4
    end = datetime(2026, 7, 18, 22, 0)
    controls = optimize_horizon(
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.1] * steps,
        buy_prices=[0.5] * steps,
        rce_series=rce,
        initial_soc_kwh=16.0,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.1] * 24, "pv_total": 0.0, "load_total": 2.4},
            "tomorrow": {"pv": [4.0] * 24, "load": [0.1] * 24, "pv_total": 96.0, "load_total": 2.4},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp19 = sum(c.battery_export_kwh for c in controls[0:4])
    exp20 = sum(c.battery_export_kwh for c in controls[4:8])
    exp21 = sum(c.battery_export_kwh for c in controls[8:12])
    assert exp21 > 1.0
    assert exp21 >= exp19
    assert exp21 >= exp20
    assert exp19 + exp20 + exp21 > 2.0


def test_floor_ignores_load_only_quarters_outside_export_span():
    """Bat Discharge floor is measured only inside the Dis span.

    Repro of hour 23: ~0.5 kWh export in q0 plus ~1.1 kWh load-only discharge
    in q1–q3 must NOT count as meeting min_hourly_transfer — otherwise the plan
    shows Feed-in/+PLN without a Dis timer.
    """
    from src.plan_optimizer import _plan_hour_export_claim

    claim = _plan_hour_export_claim(
        hour=23,
        role="last",
        soc0=8.0,  # ~18.6% of 43 — only a thin slice above min+reserve
        hold_soc_kwh=0.0,
        pv_q=[0.0, 0.0, 0.0, 0.0],
        load_q=[0.25, 0.25, 0.25, 0.25],
        reserve_q=[6.9, 6.9, 6.9, 6.9],  # overnight reserve ≈ 16%
        base_charge_q=[0.0, 0.0, 0.0, 0.0],
        battery_cap=43.0,
        min_kwh=6.88,
        discharge_ac_step=1.85,
        eta_grid=0.925,
        eta_out=0.925,
        eta_pv_load=0.925,
        eta_pv_grid=0.925,
        eta_pv_battery=0.925,
        eps_step=0.01,
        min_hourly_kwh=2.0,
    )
    assert claim is None


def test_plan_rows_never_show_orphan_export_without_dis_timer():
    """Battery feed-in above the hourly floor requires a Dis timer_schedule."""
    from src.debug_smart_plan import run_day_smart_q15_plan, timer_schedule_by_hour
    from src.grid_config import merge_grid_defaults
    from src.simulation_config import merge_simulation_defaults
    from tests.test_discharge_power_invariants import (
        DATE, LOAD_TODAY, PV_TODAY, RCE_Q,
    )

    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {},
    }
    merge_grid_defaults(cfg)
    cfg = merge_simulation_defaults(cfg)
    res = run_day_smart_q15_plan(
        date_str=DATE,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        tomorrow_pv=PV_TODAY,
        tomorrow_load=LOAD_TODAY,
        cfg=cfg,
        rce_quarters=list(RCE_Q),
        initial_soc_kwh=0.85 * 43.0,
        from_hour=0,
    )
    assert res is not None
    timers = timer_schedule_by_hour(res["q15_by_hour"], cfg, res["epsilon"])
    eps = float(res["epsilon"])
    for h, slots in res["q15_by_hour"].items():
        exp = sum(float(s.get("battery_export_kwh") or 0) for s in slots)
        if exp <= eps:
            continue
        txt = timers.get(h) or ""
        assert txt.startswith("Dis"), (
            f"hour {h}: battery export {exp:.3f} kWh but timer={txt!r} "
            f"(orphan feed-in without Dis — the 23:00 +0.38 PLN case)"
        )


def test_middle_hour_is_full_four_quarters():
    base = [HourControl(0.0, 0.0) for _ in range(12)]
    selected = {10, 11, 12}
    roles = export_window_roles(selected)
    assert roles[11] == "middle"
    spans = {10: (0, 4), 11: (0, 4), 12: (0, 4)}
    # Direct span check via candidates
    assert export_span_candidates(roles[11]) == [(0, 4)]
    offset = 10 * 4
    controls = assign_ranked_battery_export(
        base,
        steps=12,
        pv_series=[0.0] * 12,
        load_series=[0.1] * 12,
        rce_series=[0.9] * (13 * 4),
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=35.0,
        battery_cap=40.0,
        min_kwh=6.4,
        discharge_ac_step=2.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=0.01,
        reserves=[6.4] * 12,
        export_floor=0.5,
        min_hourly_kwh=0.0,
    )
    # Middle hour steps 4..7 should all export when SOC allows
    mid = controls[4:8]
    assert all(c.battery_export_kwh > 0.05 for c in mid)
