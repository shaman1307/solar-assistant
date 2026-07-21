"""Optimizer / timer charge at needed kW, floored by min_hourly_transfer."""

from __future__ import annotations

from src.plan_optimizer import (
    HourControl,
    _front_load_charge_step_ac,
    _front_load_offpeak_grid_charge,
    _grid_charge_ac_kw,
)
from src.timer_plan import _infer_charge_timer_power_kw


OFF = 0.5
PEAK = 1.2


def test_grid_charge_ac_clips_to_remaining_need():
    """DP step takes only AC still needed for target, not always the cap."""
    ac = _grid_charge_ac_kw(
        10.0,
        buy_p=OFF,
        offpeak_buy=OFF,
        charge_target_soc_kwh=10.5,
        head_room_kwh=30.0,
        charge_ac_cap_kw=1.5,
        eta_grid=1.0,
        epsilon=0.01,
    )
    assert ac == 0.5


def test_front_load_spreads_small_budget_over_min_block():
    """2.7 kWh AC over q15 → 2 steps at 1.35, not one step at max 1.6225."""
    rate = _front_load_charge_step_ac(
        2.7,
        charge_ac_step=1.6225,
        step_scale=0.25,
        min_block_minutes=30,
        eps_step=0.001,
    )
    assert rate == 1.35


def test_front_load_keeps_max_when_budget_needs_it():
    rate = _front_load_charge_step_ac(
        3.245,
        charge_ac_step=1.6225,
        step_scale=0.25,
        min_block_minutes=30,
        eps_step=0.001,
    )
    assert rate == 1.6225


def test_front_load_offpeak_q15_uses_needed_step_rate():
    """q15: 2.7 kWh budget → two 1.35 steps (30 min), not max 1.6225."""
    min_kwh = 7.68
    # 8 q15 steps before peak; skip first hour (4 slots).
    controls = [HourControl(0.0, 0.0, False) for _ in range(8)]
    controls[4] = HourControl(1.6225, 0.0, False)
    controls[5] = HourControl(1.0775, 0.0, False)
    buy = [OFF] * 8
    out = _front_load_offpeak_grid_charge(
        controls,
        pv_series=[0.0] * 8,
        load_series=[0.1] * 8,
        buy_prices=buy,
        offpeak_buy=OFF,
        charge_targets=[0.0] * 8,
        initial_soc_kwh=min_kwh + 1.0,
        battery_cap=48.0,
        min_kwh=min_kwh,
        charge_ac_step=1.6225,
        discharge_ac_step=2.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=0.001,
        reserves=[min_kwh] * 8,
        step_scale=0.25,
        skip_leading_slots=4,
        min_block_minutes=30,
    )
    assert out[4].grid_charge_kw == 1.35
    assert out[5].grid_charge_kw == 1.35
    assert out[6].grid_charge_kw == 0.0
    assert sum(c.grid_charge_kw for c in out) == 2.7


def test_infer_charge_power_floors_at_min_hourly():
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {"capacity_kwh": 48.0, "max_charge_power_kw": 6.0},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
    }
    # 0.6 kWh in 30 min → raw 1.2 kW, floor 4 kW.
    assert _infer_charge_timer_power_kw(0.6, 30, cfg) == 4.0


def test_infer_charge_power_needed_above_floor():
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {"capacity_kwh": 48.0, "max_charge_power_kw": 6.0},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
    }
    # 2.5 kWh / 0.5 h = 5 kW.
    assert _infer_charge_timer_power_kw(2.5, 30, cfg) == 5.0
