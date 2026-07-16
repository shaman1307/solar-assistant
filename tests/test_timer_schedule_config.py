"""Timer Schedule config thresholds (optimizer + min slot duration)."""

from src.simulation_config import (
    get_timer_schedule_params,
    merge_timer_schedule_defaults,
    plan_timer_min_block_minutes,
    plan_timer_min_hourly_transfer_kwh,
)
from src.timer_plan import (
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
    build_hour_timer_schedule,
)


def _cfg(**timer_schedule) -> dict:
    return {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "timer_schedule": timer_schedule,
    }


def test_timer_schedule_defaults():
    cfg = merge_timer_schedule_defaults({})
    params = get_timer_schedule_params(cfg)
    assert params["min_block_minutes"] == 30
    assert params["min_hourly_transfer_kwh"] == 2.0


def test_custom_timer_schedule_params():
    cfg = _cfg(min_block_minutes=45, min_hourly_transfer_kwh=3.5)
    assert plan_timer_min_block_minutes(cfg) == 45
    assert plan_timer_min_hourly_transfer_kwh(cfg) == 3.5


def test_timer_follows_optimizer_bat_discharge_without_extra_floor():
    """Timer text is derived from q15 slots; no second min-kWh gate in timer_plan."""
    cfg = _cfg(min_hourly_transfer_kwh=2.0)
    slots = [
        {
            "action": ACTION_DISCHARGE_GRID,
            "battery_delta": -0.5,
            "grid_export": 0.5,
            "battery_export_kwh": 0.5,
            "grid_import": 0.0,
            "pv": 0.0,
            "load": 0.0,
        }
        for _ in range(4)
    ]
    txt = build_hour_timer_schedule(
        10, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=2.0,
    )
    assert txt != ""


def test_timer_skips_when_optimizer_left_zero_bat_discharge():
    cfg = _cfg()
    slots = [
        {
            "action": ACTION_DISCHARGE_GRID,
            "battery_delta": 0.0,
            "grid_export": 3.0,
            "battery_export_kwh": 0.0,
            "grid_import": 0.0,
            "pv": 3.0,
            "load": 0.0,
        }
        for _ in range(4)
    ]
    txt = build_hour_timer_schedule(
        12, slots, cfg, action="Idle - PV to Load. On-Grid", grid_export=12.0,
    )
    assert txt == ""
