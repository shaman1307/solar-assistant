"""Tests for apply_current_hour_blend and build_blended_current_hour_q15 (unified sim)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.plan_hourly_actuals import apply_current_hour_blend, build_blended_current_hour_q15

WARSAW = ZoneInfo("Europe/Warsaw")

_CFG = {
    "battery": {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 8.0,
    },
    "inverter": {"ac_capacity_kw": 8.0},
    "simulation": {
        "min_soc_pct": 15,
        "epsilon_kwh": 0.05,
        "losses_pct": {
            "grid_to_battery": 7.5,
            "battery_to_load_or_grid": 7.5,
            "pv_to_grid": 7.5,
            "pv_to_load": 7.5,
        },
    },
}

Q15_KEYS = {"quarter", "production", "consumption", "soc", "battery", "grid_import", "grid_export"}


def test_apply_blend_out_of_bounds_hour_returns_original():
    pv = [1.0, 2.0]
    load = [3.0, 4.0]
    pv_out, load_out = apply_current_hour_blend(
        pv, load, hour=5,
        now=datetime(2026, 6, 28, 10, 0, tzinfo=WARSAW),
        forecast_pv_q15=None,
        forecast_load_q15=None,
        series_10min=None,
    )
    assert pv_out is pv
    assert load_out is load


def test_apply_blend_at_hour_start_patches_hour_slot():
    pv_hourly = [5.0, 2.0, 1.0]
    load_hourly = [3.0, 0.8, 0.5]
    hour = 1
    now = datetime(2026, 6, 28, 1, 0, tzinfo=WARSAW)

    pv_out, load_out = apply_current_hour_blend(
        pv_hourly, load_hourly, hour=hour,
        now=now,
        forecast_pv_q15=None,
        forecast_load_q15=None,
        series_10min=None,
    )

    assert pv_out[0] == 5.0
    assert pv_out[2] == 1.0
    assert pv_out[hour] == 2.0
    assert load_out[hour] == 0.8
    assert pv_out is not pv_hourly


def test_blended_q15_returns_four_slots_with_sim_chain():
    hour = 10
    now = datetime(2026, 6, 28, 10, 0, tzinfo=WARSAW)

    q15 = build_blended_current_hour_q15(
        hour, now,
        forecast_pv_q15=[0.5] * 96,
        forecast_load_q15=[0.3] * 96,
        series_10min=None,
        soc_start_kwh=5.0,
        opt_slots=[{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4,
        cfg=dict(_CFG),
        pv_hourly=2.0,
        load_hourly=1.2,
    )

    assert len(q15) == 4
    for slot in q15:
        assert Q15_KEYS.issubset(slot.keys())
    assert q15[-1]["soc"] > q15[0]["soc"] or q15[-1]["production"] == 0.0
    assert sum(s["production"] for s in q15) == 2.0


def test_blended_q15_soc_chain_not_influx():
    hour = 12
    now = datetime(2026, 6, 28, 12, 30, tzinfo=WARSAW)
    soc_series: list[float | None] = [None] * 100
    soc_series[73] = 61.0
    soc_series[75] = 58.0

    q15 = build_blended_current_hour_q15(
        hour, now,
        forecast_pv_q15=[0.0] * 96,
        forecast_load_q15=[0.0] * 96,
        series_10min={"soc": soc_series},
        soc_start_kwh=6.3,
        opt_slots=[{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4,
        cfg=dict(_CFG),
    )

    assert q15[0]["soc"] != 61.0
    assert q15[1]["soc"] != 58.0


def test_blended_q15_quarter_numbers_are_sequential():
    hour = 6
    now = datetime(2026, 6, 28, 6, 0, tzinfo=WARSAW)

    q15 = build_blended_current_hour_q15(
        hour, now,
        forecast_pv_q15=None,
        forecast_load_q15=None,
        series_10min=None,
        soc_start_kwh=4.0,
        opt_slots=[],
        cfg=dict(_CFG),
    )

    assert [s["quarter"] for s in q15] == [0, 1, 2, 3]
