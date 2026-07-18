"""Reserve SOC looks through midnight to morning PV coverage."""

import pytest

from src.plan_optimizer import (
    _allow_grid_charge,
    _grid_charge_target_soc_kwh_from_step,
    _reserve_soc_kwh_from_step,
)


def test_reserve_not_cut_by_single_sunny_q15():
    """One bright 15-min slot must not end the night reserve early."""
    # Evening of day 0 (hour 20) through night into tomorrow morning.
    # global offset = 20*4 so series index 0 is hour 20 today.
    pv = (
        [0.0] * 4  # hour 20 dark
        + [0.5, 0.0, 0.0, 0.0]  # hour 21 — one sunny q15, hour still dark overall
        + [0.0] * 8  # hours 22–23
        + [0.0] * 8  # tomorrow 00–01 still dark
        + [0.5, 0.5, 0.5, 0.5]  # tomorrow hour 02 covers → stop
    )
    load = [0.12] * len(pv)
    reserve = _reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=1.5,
        eta_out=0.9,
        eta_pv_load=0.95,
        epsilon=0.01,
        global_step_offset=20 * 4,
    )
    assert reserve > 2.0


def test_evening_sun_does_not_stop_before_midnight():
    """Same-day evening PV ≥ load must not end reserve; cross midnight to morning."""
    # Hour 18 today: still sunny. Hours 19–23 night. Tomorrow 00 dark, 01 covers.
    pv = (
        [0.4] * 4  # hour 18 sun
        + [0.0] * 20  # hours 19–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers → stop
    )
    load = [0.2] * len(pv)
    floor = 1.5
    reserve = _reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        global_step_offset=18 * 4,
    )
    # From after hour 18: 5h today (19–23) + 1h tomorrow 00 = 24 slots × 0.2
    assert reserve == pytest.approx(floor + 24 * 0.2)


def test_afternoon_gap_does_not_stop_before_tonight():
    """Midday cloud then afternoon sun must not end reserve; wait for next morning."""
    pv = (
        [0.0] * 4  # hour 12 cloudy
        + [0.5] * 4  # hour 13 sun returns (afternoon — ignore)
        + [0.0] * 40  # hours 14–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers → stop
    )
    load = [0.2] * len(pv)
    floor = 1.0
    reserve = _reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        global_step_offset=12 * 4,
    )
    # After hour 12: hour 13 no deficit; 14–23 + tomorrow 00 = 11h × 0.8 = 8.8
    assert reserve == pytest.approx(floor + 8.8)


def test_post_midnight_stops_on_same_morning_not_next_day():
    """From 00:00, reserve is only until today's morning PV — not tomorrow."""
    # Day 1 hour 0: night load, morning covers at hour 6. Extra day-2 sun must not inflate.
    pv = (
        [0.0] * 24  # hours 0–5 dark
        + [0.5] * 4  # hour 6 covers → stop
        + [0.0] * 68  # rest of day + next night (must be ignored)
        + [0.5] * 4  # next-day morning (must be ignored)
    )
    load = [0.2] * len(pv)
    floor = 1.5
    reserve = _reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        global_step_offset=0,  # already on the overnight day
    )
    # step=3 is last q15 of hour 0; j starts at hour 1.
    # Hours 1–5 dark (5h × 0.8) then hour 6 covers → 4.0 + floor.
    assert reserve == pytest.approx(floor + 5 * 0.8)
    # Must stay far below a full extra day of load (~20+).
    assert reserve < floor + 8.0


def test_morning_hour_does_not_reserve_through_next_night():
    """From hour 06 with PV covering, reserve stays near floor (not Monday night)."""
    pv = [0.5] * 4  # hour 6 covers
    pv += [0.0] * 68  # rest of day + next night
    pv += [0.5] * 4  # next morning
    load = [0.2] * len(pv)
    floor = 1.5
    reserve = _reserve_soc_kwh_from_step(
        0, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        global_step_offset=6 * 4,
    )
    assert reserve == pytest.approx(floor)


def test_weekend_grid_charge_target_ignores_overnight_offpeak_load():
    """All-offpeak buy prices → charge target is only the min floor."""
    # Night 00–05 then morning covers — large load, but every slot is offpeak.
    pv = [0.0] * 24 + [0.5] * 4
    load = [0.2] * len(pv)
    buy = [0.62] * len(pv)  # offpeak everywhere (weekend)
    floor = 1.5
    target = _grid_charge_target_soc_kwh_from_step(
        3, pv, load, buy, floor, 1.0, 1.0, 0.01, offpeak_buy=0.62,
        global_step_offset=0,
    )
    assert target == pytest.approx(floor)
    assert not _allow_grid_charge(10.0, 0.62, 0.62, target, 0.01)


def test_weekday_grid_charge_target_includes_morning_peak():
    """Peak-priced morning deficits raise the grid-charge target above floor."""
    # Hours 0–5 dark offpeak, 6–7 peak with load, hour 8 PV covers.
    pv = [0.0] * 32 + [0.5] * 4
    load = [0.2] * len(pv)
    buy = [0.62] * 24 + [1.24] * 8 + [1.24] * 4  # 0–5 off, 6–8 peak
    # trim buy to len
    buy = buy[: len(pv)]
    floor = 1.5
    target = _grid_charge_target_soc_kwh_from_step(
        3, pv, load, buy, floor, 1.0, 1.0, 0.01, offpeak_buy=0.62,
        global_step_offset=0,
    )
    # Peak hours 6–7 only (hour 8 covers and stops): 2h × 0.8 = 1.6
    assert target == pytest.approx(floor + 1.6)
    assert _allow_grid_charge(floor + 0.5, 0.62, 0.62, target, 0.01)
    assert not _allow_grid_charge(target + 0.1, 0.62, 0.62, target, 0.01)
