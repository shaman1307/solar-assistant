"""Post-discharge survive reserve: load after last Dis until next-day PV cover + min."""

import pytest

from src.plan_optimizer import (
    post_discharge_reserve_soc_kwh,
    _pv_cover_ends_overnight_need,
)
from src.timer_plan import (
    ACTION_DISCHARGE_GRID,
    _blocks_q15_to_slots,
    _discharge_cap_pct_from_row,
    _merge_blocks_q15,
)

OFF = 0.62
PEAK = 1.24


def _q15_buys(hour_prices: list[float]) -> list[float]:
    out: list[float] = []
    for p in hour_prices:
        out.extend([p] * 4)
    return out


def _weekday_day_buys() -> list[float]:
    return [
        OFF if not (6 <= h < 13 or 15 <= h < 22) else PEAK
        for h in range(24)
    ]


def test_evening_walk_requires_next_day_pv_cover():
    assert _pv_cover_ends_overnight_need(
        local_hour=18,
        start_local_hour=18,
        crossed_midnight=False,
        seen_insufficient=True,
        cover_bound=13,
    ) is False
    assert _pv_cover_ends_overnight_need(
        local_hour=7,
        start_local_hour=18,
        crossed_midnight=True,
        seen_insufficient=True,
        cover_bound=13,
    ) is True


def test_post_discharge_reserve_from_hour_after_last_dis():
    """After Dis hour 21: count 22–23 + tomorrow until PV cover + floor."""
    # Hours 21..23 today + 00..02 tomorrow in series; cover at tomorrow hour 01.
    pv = (
        [0.0] * 4  # 21
        + [0.0] * 8  # 22–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers
    )
    load = [0.2] * len(pv)
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[21:] + day1[:2])
    floor = 1.5
    # last Dis hour 21, offset at hour 21 start
    reserve = post_discharge_reserve_soc_kwh(
        21, pv, load, floor, 1.0, 1.0, 0.01,
        buy_series=buy, offpeak_buy=OFF,
        slots_per_hour=4, global_step_offset=21 * 4,
    )
    # From hour 22: 22,23,00 = 12 slots × 0.2, stop at tomorrow 01 cover
    assert reserve == pytest.approx(floor + 12 * 0.2)


def test_discharge_cap_uses_reserve_soc_pct_not_min():
    row = {"reserve_soc_pct": 34.2, "soc": 40.0}
    assert _discharge_cap_pct_from_row(row, 16) == 34


def test_dis_slots_keep_reserve_capacity_pct():
    rows = [
        {
            "start": "25-07-2026 21:00",
            "hour": 21,
            "action": ACTION_DISCHARGE_GRID,
            "soc": 40.0,
            "grid_export": 1.5,
            "reserve_soc_pct": 34.0,
        },
        {
            "start": "25-07-2026 21:15",
            "hour": 21,
            "action": ACTION_DISCHARGE_GRID,
            "soc": 36.0,
            "grid_export": 1.5,
            "reserve_soc_pct": 34.0,
        },
        {
            "start": "25-07-2026 21:30",
            "hour": 21,
            "action": ACTION_DISCHARGE_GRID,
            "soc": 34.0,
            "grid_export": 1.5,
            "reserve_soc_pct": 34.0,
        },
    ]
    blocks = _merge_blocks_q15(rows, ACTION_DISCHARGE_GRID)
    assert len(blocks) == 1
    assert blocks[0]["capacity_pct"] == 34
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
    }
    slots = _blocks_q15_to_slots(blocks, "discharge", [], cfg)
    assert slots[0]["capacity_pct"] == 34
    assert slots[0]["capacity_pct"] != 16
