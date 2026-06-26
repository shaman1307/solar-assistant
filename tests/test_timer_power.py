"""SA timer power uses full AC capacity (inverter splits load vs grid)."""

from src.simulation_config import (
    normalize_battery_power_limits,
    plan_timer_charge_grid_kw,
    plan_timer_charge_power_kw,
    plan_timer_discharge_ac_kw,
    plan_timer_discharge_power_kw,
)
from src.timer_plan import (
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
    build_hour_timer_schedule,
    derive_timer_schedule_q15,
)

_CFG = {
    "inverter": {"ac_capacity_kw": 8.0},
    "battery": {
        "capacity_kwh": 43.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 8.0,
    },
    "simulation": {"min_soc_pct": 17},
}


def _discharge_slot(grid_export_kwh: float = 1.5) -> dict:
    return {
        "action": ACTION_DISCHARGE_GRID,
        "battery_delta": -2.0,
        "grid_export": grid_export_kwh,
        "battery_export_kwh": grid_export_kwh,
        "grid_import": 0.0,
        "pv": 0.0,
    }


def test_hour_timer_discharge_uses_discharge_cap():
    slots = [_discharge_slot(1.5) for _ in range(4)]
    txt = build_hour_timer_schedule(
        22, slots, _CFG, action=ACTION_DISCHARGE_GRID, grid_export=6.0,
    )
    assert "8.0kW" in txt


def test_derive_timer_schedule_q15_discharge_power():
    rows = []
    for q in range(4):
        rows.append({
            "start": f"2026-06-26 22:{q * 15:02d}",
            "action": ACTION_DISCHARGE_GRID,
            "battery": 0,
            "grid_import": 0,
            "grid_export": 1.5,
            "soc": 55,
        })
    sched = derive_timer_schedule_q15(rows, _CFG)
    dis = sched["discharge_slots"][0]
    assert dis["power_kw"] == 8.0


def test_charge_timer_capped_at_battery_max():
    cfg = {
        **_CFG,
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {**_CFG["battery"], "max_charge_power_kw": 5.0},
    }
    slots = [{
        "action": ACTION_CHARGE_GRID,
        "battery_delta": 1.0,
        "grid_import": 0.5,
        "grid_export": 0.0,
        "pv": 0.0,
    } for _ in range(4)]
    txt = build_hour_timer_schedule(
        2, slots, cfg, action=ACTION_CHARGE_GRID,
    )
    assert "5.0kW" in txt
    assert "8.0kW" not in txt


def test_normalize_clamps_above_hardware_max():
    cfg = {
        "inverter": {"ac_capacity_kw": 10.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 9.0,
            "max_discharge_power_kw": 12.0,
        },
        "simulation": {"min_soc_pct": 17, "losses_pct": {
            "grid_to_battery": 7.5,
            "battery_to_load_or_grid": 7.5,
            "pv_to_grid": 7.5,
        }},
    }
    normalize_battery_power_limits(cfg)
    assert cfg["battery"]["max_charge_power_kw"] == 5.0
    assert cfg["battery"]["max_discharge_power_kw"] == 8.0
    assert plan_timer_charge_power_kw(cfg) == 5.0
    assert plan_timer_discharge_power_kw(cfg) == 8.0


def test_model_applies_config_losses_to_timer_caps():
    cfg = {
        **_CFG,
        "simulation": {
            "min_soc_pct": 17,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
            },
        },
    }
    assert plan_timer_discharge_power_kw(cfg) == 8.0
    assert plan_timer_discharge_ac_kw(cfg) == 7.4
    assert plan_timer_charge_power_kw(cfg) == 5.0
    assert plan_timer_charge_grid_kw(cfg) == 5.41
