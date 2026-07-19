"""Ranked hourly-RCE export assignment and multi-hour window spans."""

from __future__ import annotations

from datetime import datetime

from src.grid_config import merge_grid_defaults
from src.plan_optimizer import (
    HourControl,
    assign_ranked_battery_export,
    export_span_candidates,
    export_window_roles,
    hourly_avg_rce,
    optimize_horizon,
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
