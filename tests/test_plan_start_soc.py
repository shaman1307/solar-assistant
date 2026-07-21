"""Plan-start SOC must not use the 50% midnight placeholder."""

from __future__ import annotations

import pytest

from src.simulation import _plan_start_soc_kwh


def test_plan_start_hour0_uses_live_not_half_capacity_placeholder():
    battery_cap = 48.0
    live = 0.22 * battery_cap
    placeholder = 0.5 * battery_cap
    got = _plan_start_soc_kwh(
        0,
        {"soc": [None] * 24},
        battery_cap,
        16.0,
        placeholder,
        live,
    )
    assert got == live


def test_plan_start_later_hour_uses_prior_influx_end():
    battery_cap = 48.0
    live = 0.22 * battery_cap
    hourly = {"soc": [None] * 24}
    hourly["soc"][2] = 30.0  # end of hour 2
    got = _plan_start_soc_kwh(
        3,
        hourly,
        battery_cap,
        16.0,
        0.5 * battery_cap,
        live,
    )
    assert got == pytest.approx(0.30 * battery_cap)
