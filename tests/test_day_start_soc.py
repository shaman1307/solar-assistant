"""Seed solid day-plan SOC from calendar midnight (yesterday H23 / live)."""

from __future__ import annotations

import pytest

from src.plan_hourly_actuals import resolve_day_start_soc_kwh


def test_day_start_prefers_yesterday_h23():
    battery_cap = 48.0
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=0.50 * battery_cap,
        today_hourly={"soc": [40.0] + [None] * 23, "bat_charge": [0.0] * 24, "bat_discharge": [2.0] * 24},
        prev_day_hourly={"soc": [None] * 23 + [29.0]},
    )
    assert got == pytest.approx(0.29 * battery_cap)


def test_day_start_falls_back_to_live():
    battery_cap = 48.0
    live = 0.22 * battery_cap
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=live,
        today_hourly={"soc": [None] * 24},
        prev_day_hourly={"soc": [None] * 24},
    )
    assert got == live


def test_day_start_backtracks_hour0_when_no_yesterday_or_live():
    battery_cap = 48.0
    # end SOC 28%, discharge 0.689 kWh → start ≈ 14.129 kWh ≈ 29.435%
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=None,
        today_hourly={
            "soc": [28.0] + [None] * 23,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.689] + [0.0] * 23,
        },
        prev_day_hourly=None,
    )
    assert got == pytest.approx(0.28 * battery_cap + 0.689)


def test_day_start_last_resort_is_min_soc():
    battery_cap = 48.0
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=None,
        today_hourly=None,
        prev_day_hourly=None,
    )
    assert got == pytest.approx(0.16 * battery_cap)
