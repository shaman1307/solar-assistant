"""Meter/inverter SOC is stored as reported, including below the plan min floor."""

from src.plan_hourly_actuals import (
    _bound_soc_pct,
    _clamp_soc_pct,
    _soc_kwh_after_battery_delta,
    meter_soc_pct_for_q15,
    overlay_meter_soc_on_rows,
    resolve_day_start_soc_kwh,
)


def test_clamp_soc_does_not_lift_to_plan_min():
    assert _clamp_soc_pct(16.0, 18.0) == 16.0
    assert _bound_soc_pct(16.4) == 16.4
    assert _bound_soc_pct(-1.0) == 0.0
    assert _bound_soc_pct(101.0) == 100.0


def test_battery_delta_does_not_lift_to_min_kwh():
    cap = 48.0
    min_kwh = 0.18 * cap
    start = 0.17 * cap
    got = _soc_kwh_after_battery_delta(
        start, -0.2, min_kwh=min_kwh, battery_cap=cap,
    )
    assert got < min_kwh
    assert got == start - 0.2


def test_meter_soc_pct_for_q15_uses_10min_bucket():
    series = {"soc": [None] * 144}
    # H05 q3 ends at :60 → 10-min index 5 of that hour.
    series["soc"][5 * 6 + 5] = 16.2
    assert meter_soc_pct_for_q15(series, 5, 3) == 16.2


def test_overlay_replaces_clamped_history_soc():
    rows = [{
        "plan_date": "2026-08-17",
        "hour": 5,
        "history_hour": True,
        "soc": 18.0,
        "q15": [
            {"quarter": 0, "soc": 18.0},
            {"quarter": 1, "soc": 18.0},
            {"quarter": 2, "soc": 18.0},
            {"quarter": 3, "soc": 18.0},
        ],
    }]
    series = {"soc": [None] * 144}
    for i in range(6):
        series["soc"][5 * 6 + i] = 16.0
    overlay_meter_soc_on_rows(
        rows,
        today_str="2026-08-17",
        today_hourly={"soc": [None] * 5 + [16.0] + [None] * 18},
        series_10min=series,
        current_hour=22,
    )
    assert rows[0]["soc"] == 16.0
    assert all(s["soc"] == 16.0 for s in rows[0]["q15"])


def test_day_start_keeps_yesterday_below_plan_min():
    battery_cap = 48.0
    got = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=18.0,
        live_soc_kwh=0.50 * battery_cap,
        today_hourly=None,
        prev_day_hourly={"soc": [None] * 23 + [16.0]},
    )
    assert got == 0.16 * battery_cap
