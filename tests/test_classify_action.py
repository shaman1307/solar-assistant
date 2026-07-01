"""Action labels and history timer schedule for grid-to-load hours."""

from src.timer_plan import (
    ACTION_CHARGE_GRID,
    ACTION_IDLE_GRID,
    ACTION_IDLE_PV,
    build_hour_timer_schedule,
    classify_action,
)

_CFG = {
    "inverter": {"ac_capacity_kw": 8.0},
    "battery": {
        "capacity_kwh": 43.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 8.0,
    },
    "simulation": {"min_soc_pct": 17, "epsilon_kwh": 0.05},
}


def test_grid_to_load_without_battery_charge_is_idle():
    act = classify_action(
        bat_charge=0.0,
        bat_discharge=0.0,
        grid_import=1.8,
        grid_export=0.0,
        production=0.0,
        epsilon=0.05,
    )
    assert act == ACTION_IDLE_GRID


def test_tiny_battery_noise_does_not_trigger_grid_charge():
    act = classify_action(
        bat_charge=0.02,
        bat_discharge=0.0,
        grid_import=1.5,
        grid_export=0.0,
        production=0.0,
        epsilon=0.05,
    )
    assert act == ACTION_IDLE_GRID


def test_pv_covers_load_is_idle_pv_not_charge():
    act = classify_action(
        bat_charge=0.0,
        bat_discharge=0.0,
        grid_import=0.0,
        grid_export=0.0,
        production=0.4,
        epsilon=0.05,
    )
    assert act == ACTION_IDLE_PV


def test_real_grid_charge_still_labeled():
    act = classify_action(
        bat_charge=0.8,
        bat_discharge=0.0,
        grid_import=1.2,
        grid_export=0.0,
        production=0.0,
        epsilon=0.05,
    )
    assert act == ACTION_CHARGE_GRID


def test_charge_timer_skipped_when_actual_bat_charge_zero():
    """Plan slots may show grid import; actual hour had no battery charge."""
    slots = [{
        "action": ACTION_CHARGE_GRID,
        "battery_delta": 0.0,
        "grid_import": 1.5,
        "grid_export": 0.0,
        "pv": 0.0,
    } for _ in range(4)]
    txt = build_hour_timer_schedule(
        3,
        slots,
        _CFG,
        action=ACTION_CHARGE_GRID,
        bat_charge=0.0,
        epsilon=0.05,
    )
    assert txt == ""
