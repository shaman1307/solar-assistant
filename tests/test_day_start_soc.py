"""Seed solid day-plan SOC from last available yesterday reading (never 50%)."""

from __future__ import annotations

import pytest

from src.plan_hourly_actuals import last_available_soc_pct, resolve_day_start_soc_kwh
from src.simulation import ea_today_end_soc_pct, rebuild_tomorrow_plan_soc_from_ea_end


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


def test_day_start_uses_last_hourly_when_h23_missing():
    """At 23:59 H23 may be empty — use H22 (or earlier) instead of 50%."""
    battery_cap = 48.0
    soc = [None] * 24
    soc[22] = 31.5
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=0.50 * battery_cap,
        today_hourly=None,
        prev_day_hourly={"soc": soc},
    )
    assert got == pytest.approx(0.315 * battery_cap)


def test_day_start_prefers_10min_over_hourly():
    battery_cap = 48.0
    series = {"soc": [None] * 143 + [27.4]}
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=0.50 * battery_cap,
        today_hourly=None,
        prev_day_hourly={"soc": [None] * 23 + [40.0]},
        prev_day_series_10min=series,
    )
    assert got == pytest.approx(0.274 * battery_cap)


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


def test_day_start_last_resort_is_min_soc_not_fifty():
    battery_cap = 48.0
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=16.0,
        live_soc_kwh=None,
        today_hourly=None,
        prev_day_hourly=None,
    )
    assert got == pytest.approx(0.16 * battery_cap)
    assert got != pytest.approx(0.50 * battery_cap)


def test_last_available_soc_pct_walks_back():
    assert last_available_soc_pct({"soc": [10.0, None, 20.0] + [None] * 21}) == 20.0
    assert last_available_soc_pct(
        {"soc": [None] * 24},
        {"soc": [None, 12.0, None]},
    ) == 12.0


def test_rebuild_tomorrow_seeds_from_ea_end():
    """Tomorrow chart starts from EA end-of-today (~26%), not solid plan end."""
    today = [None] * 95 + [30.1]
    tomorrow_wrong = [39.8] + [None] * 95
    cfg = {
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
            "min_soc_pct": 16.0,
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.8,
                "offpeak_energy_only_pln_kwh": 0.4,
                "peak_hours": [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
            }
        },
        "simulation": {},
    }
    forecast = {
        "tomorrow": {
            "pv": [0.0] * 6 + [1.0] * 12 + [0.0] * 6,
            "load": [0.5] * 24,
        }
    }
    out = rebuild_tomorrow_plan_soc_from_ea_end(
        {"today": today, "tomorrow": tomorrow_wrong},
        forecast=forecast,
        cfg=cfg,
        today_date="2026-07-24",
        ea_end_soc_pct=26.1,
    )
    assert out["today"][-1] == 30.1
    assert out["tomorrow"][0] is not None
    # Seeded at 26.1%; first q15 is end-of-slot so it may drop slightly with load.
    assert abs(float(out["tomorrow"][0]) - 39.8) > 2.0
    assert abs(float(out["tomorrow"][0]) - 30.1) > 2.0
    assert 16.0 <= float(out["tomorrow"][0]) <= 27.0


def test_ea_today_end_soc_pct_prefers_latest_today_hour():
    plan = {
        "today_date": "2026-07-25",
        "history_rows": [
            {"plan_date": "2026-07-25", "hour": 21, "soc": 30.0},
        ],
        "rows": [
            {"plan_date": "2026-07-25", "hour": 22, "soc": 28.6},
            {
                "plan_date": "2026-07-25",
                "hour": 23,
                "soc": 26.1,
                "q15": [{"soc": 27.0}, {"soc": 26.1}],
            },
            {"plan_date": "2026-07-26", "hour": 0, "soc": 24.0},
        ],
    }
    assert ea_today_end_soc_pct(plan) == 26.1
