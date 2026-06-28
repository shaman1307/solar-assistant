"""Transfer loss physics in plan simulation."""

import pytest

from src.plan_optimizer import HourControl, simulate_hour
from src.plan_spill import pv_load_energy_split
from src.simulation_config import get_simulation_params


def _cfg(losses: dict | None = None) -> dict:
    base_losses = {
        "grid_to_battery": 7.5,
        "battery_to_load_or_grid": 7.5,
        "pv_to_grid": 7.5,
        "pv_to_load": 7.5,
    }
    if losses:
        base_losses.update(losses)
    return {
        "battery": {"capacity_kwh": 43.0},
        "simulation": {"min_soc_pct": 15, "epsilon_kwh": 0.05, "losses_pct": base_losses},
    }


def test_pv_load_split_applies_dc_to_ac_loss():
    deficit, surplus = pv_load_energy_split(2.0, 2.0, eta_pv_load=0.925)
    assert round(deficit, 3) == 0.15
    assert round(surplus, 3) == 0.0


def test_pv_load_split_exact_cover():
    # 2.162 * 0.925 = 1.99985, leaving a sub-mWh deficit — no surplus expected
    deficit, surplus = pv_load_energy_split(2.162, 2.0, eta_pv_load=0.925)
    assert deficit == pytest.approx(0.0, abs=1e-3)
    assert surplus == 0.0


def test_simulate_hour_pv_to_load_increases_battery_withdraw():
    params = get_simulation_params(_cfg())
    eta_pv_load = float(params["eta_pv_load"])
    eta_out = float(params["eta_battery_out"])
    min_kwh = 43.0 * 0.15

    without_loss = simulate_hour(
        20.0, 2.0, 2.0, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        eta_grid=float(params["eta_grid_battery"]),
        eta_out=eta_out,
        eta_pv_load=1.0,
        eta_pv_grid=float(params["eta_pv_grid"]),
        epsilon=0.05,
    )
    with_loss = simulate_hour(
        20.0, 2.0, 2.0, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        eta_grid=float(params["eta_grid_battery"]),
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=float(params["eta_pv_grid"]),
        epsilon=0.05,
    )
    assert without_loss.grid_import == 0.0
    assert with_loss.grid_import == 0.0
    assert with_loss.battery_delta < without_loss.battery_delta


def test_simulate_hour_ev_as_ac_load_no_double_loss():
    """EV kWh are AC load; battery covers deficit once via eta_out."""
    params = get_simulation_params(_cfg())
    min_kwh = 43.0 * 0.15
    house_load = 1.0
    ev_load = 5.0
    total_load = house_load + ev_load
    pv = 0.0
    soc_start = 30.0

    phys = simulate_hour(
        soc_start, pv, total_load, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        eta_grid=float(params["eta_grid_battery"]),
        eta_out=float(params["eta_battery_out"]),
        eta_pv_load=float(params["eta_pv_load"]),
        eta_pv_grid=float(params["eta_pv_grid"]),
        epsilon=0.05,
    )
    expected_withdraw = total_load / float(params["eta_battery_out"])
    assert round(-phys.battery_delta, 3) == round(expected_withdraw, 3)
    assert phys.grid_import == 0.0


def test_get_simulation_params_includes_pv_to_load():
    params = get_simulation_params(_cfg())
    assert params["eta_pv_load"] == 0.925
