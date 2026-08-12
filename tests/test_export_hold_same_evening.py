"""Evening export must not hold SOC for a later cheaper evening."""

from __future__ import annotations

from src.plan_optimizer import (
    HourControl,
    _BatteryGridExportHourClaim,
    _hold_soc_for_later_battery_grid_export_claims,
    plan_battery_grid_export,
)
from tests.test_ranked_export import _cfg
from src.simulation_config import get_simulation_params


def test_hold_ignores_later_cheaper_and_next_evening():
    """Hold only richer hours in the same run, not tomorrow evening."""
    # Tonight H19 + H20, tomorrow evening H44 (= next day H20)
    claims = {
        20: _BatteryGridExportHourClaim(20, (0, 4), (2.0, 2.0, 2.0, 2.0), 8.0),
        44: _BatteryGridExportHourClaim(44, (0, 4), (2.0, 2.0, 2.0, 2.0), 8.0),
    }
    ratings = {19: 1.73, 20: 1.78, 21: 1.30, 44: 1.67}
    # Claiming H19: hold for richer same-run H20 only (~8 kWh AC)
    hold = _hold_soc_for_later_battery_grid_export_claims(
        claims, from_hour=19, eta_out=1.0, ratings=ratings,
    )
    assert abs(hold - 8.0) < 1e-6
    # Claiming H20 (richest tonight): no hold for cheaper H44 next evening
    hold20 = _hold_soc_for_later_battery_grid_export_claims(
        claims, from_hour=20, eta_out=1.0, ratings=ratings,
    )
    assert hold20 == 0.0


def test_richest_tonight_not_starved_by_next_evening_draft():
    """Pass-2 must still export peak H20 when tomorrow evening is also selected."""
    cfg = _cfg(min_hourly_transfer_kwh=2.0)
    get_simulation_params(cfg)
    slots = 4
    start_h = 19
    # H19..H23 today + H0..H23 tomorrow → absolute hours 19..42
    hours = list(range(start_h, start_h + 24))
    offset = start_h * slots
    steps = len(hours) * slots

    today_rce = {19: 1.73, 20: 1.78, 21: 1.30, 22: 1.01, 23: 0.92}
    tom_rce = {17: 0.76, 18: 1.03, 19: 1.52, 20: 1.67, 21: 1.29}
    rce: list[float | None] = [None] * (offset + steps)
    for abs_h in hours:
        clock = abs_h % 24
        is_tom = abs_h >= 24
        price = tom_rce.get(clock, 0.4) if is_tom else today_rce.get(clock, 0.4)
        for q in range(slots):
            rce[abs_h * slots + q] = price

    base = [HourControl(0.0, 0.0, False) for _ in range(steps)]
    # Limited SOC so stacking tonight+tomorrow holds would starve H20
    initial = 22.0
    min_kwh = 8.6
    reserves = [min_kwh + 2.0] * steps

    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.25] * steps,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=initial,
        battery_cap=48.0,
        min_kwh=min_kwh,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        reserves=reserves,
        export_floor=0.5,
        min_hourly_kwh=2.0,
    )

    def hour_export(abs_hour: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // slots == abs_hour
        )

    exp20 = hour_export(20)
    exp21 = hour_export(21)
    assert exp20 >= 2.0, f"peak H20 must export, got {exp20}"
    # No gap: H21 without H20
    if exp21 >= 2.0:
        assert exp20 >= 2.0
