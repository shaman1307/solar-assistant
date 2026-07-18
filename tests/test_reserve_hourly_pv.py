"""Reserve SOC looks through midnight to next-day PV coverage."""

import pytest

from src.plan_optimizer import _reserve_soc_kwh_from_step


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


def test_stops_on_next_day_morning_pv_not_same_day_sun():
    """Midday sun on the same day does not end the window; next-day morning does."""
    # Noon-ish today (hour 12): cloudy then sun returns same day — keep going.
    # Night + tomorrow morning covers → stop.
    pv = (
        [0.0] * 4  # hour 12 cloudy
        + [0.5] * 4  # hour 13 sun returns (same day — ignore)
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
    # After hour 12: hours 13–23 (11h, hour 13 has no deficit) + tomorrow 00
    # = 10 dark hours today (14–23) + 1 tomorrow = 11 * 4 * 0.2 = 8.8
    # Hour 13: PV covers, deficit 0. Hours 14-23: 10*0.8=8.0. Tomorrow 00: 0.8. Total 8.8
    assert reserve == pytest.approx(floor + 8.8)
