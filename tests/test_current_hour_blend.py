"""Current-hour actual+forecast blending for Energy arbitrage."""

from datetime import datetime

import yaml

from src.plan_hourly_actuals import (
    PARTIAL_Q15_SCALE,
    TEN_MIN_KWH_PER_KW,
    _actual_battery_flows_q15,
    blend_current_hour_end,
    build_blended_current_hour_q15,
    sync_blended_current_hour_row,
)
from src.simulation_config import merge_simulation_defaults


def _flat_10m_kw(hour: int, kw: float) -> dict[str, list[float | None]]:
    series: list[float | None] = [None] * 144
    base = hour * 6
    for i in range(6):
        series[base + i] = kw
    return {"pv": list(series), "load": list(series), "soc": list(series)}


def _q15_hour(hour: int, per_q: float) -> list[float]:
    q15 = [0.0] * 96
    for q in range(4):
        q15[hour * 4 + q] = per_q
    return q15


def test_at_hour_start_uses_full_forecast():
    now = datetime(2026, 6, 26, 18, 0)
    pv_q = _q15_hour(18, 0.25)
    load_q = _q15_hour(18, 0.5)
    slots = [{"soc_pct": 52.0 + i} for i in range(4)]  # q0..q3 → 55% end of hour
    pv, load, soc = blend_current_hour_end(
        18,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        forecast_pv_hourly=1.0,
        forecast_load_hourly=2.0,
        series_10min=None,
        soc_start_pct=50.0,
        forecast_q15_slots=slots,
        forecast_soc_hour_start_pct=50.0,
    )
    assert pv == 1.0
    assert load == 2.0
    # live 50% + plan Δ to end of hour (55 - 50)
    assert soc == 55.0


def test_at_hour_start_live_plus_forecast_delta():
    """Example from spec: live 82%, forecast path 85→90%, table shows 87%."""
    now = datetime(2026, 6, 26, 18, 0)
    slots = [
        {"soc_pct": 83.0},
        {"soc_pct": 84.5},
        {"soc_pct": 86.0},
        {"soc_pct": 87.0},
    ]
    _, _, soc = blend_current_hour_end(
        18,
        now,
        forecast_pv_q15=_q15_hour(18, 0.25),
        forecast_load_q15=_q15_hour(18, 0.5),
        forecast_pv_hourly=1.0,
        forecast_load_hourly=1.0,
        series_10min=None,
        soc_start_pct=82.0,
        forecast_q15_slots=slots,
        forecast_soc_hour_start_pct=85.0,
    )
    assert soc == 87.0


def test_at_15_soc_blends_actual_and_forecast_tail():
    now = datetime(2026, 6, 26, 18, 15)
    hour = 18
    soc_series: list[float | None] = [None] * 144
    base = hour * 6
    soc_series[base] = 50.0
    soc_series[base + 1] = 51.0
    s10 = {"pv": [None] * 144, "load": [None] * 144, "soc": soc_series}
    slots = [{"soc_pct": 52.0}, {"soc_pct": 54.0}, {"soc_pct": 56.0}, {"soc_pct": 58.0}]
    _, _, soc = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=_q15_hour(hour, 0.1),
        forecast_load_q15=_q15_hour(hour, 0.1),
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=s10,
        soc_start_pct=50.0,
        forecast_q15_slots=slots,
        forecast_soc_hour_start_pct=50.0,
    )
    # actual Δ 18:00–18:10 × 1.5 = 1.5; forecast Δ 18:15–19:00 = 58 - 52 = 6
    assert soc == 57.5


def test_at_15_blends_first_10m_scaled_and_last_q15():
    now = datetime(2026, 6, 26, 18, 15)
    hour = 18
    kw = 6.0  # 6 kW → 1 kWh per 10-min bucket
    series = _flat_10m_kw(hour, kw)
    pv_q = _q15_hour(hour, 0.1)
    load_q = _q15_hour(hour, 0.2)

    actual_pv = kw * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE
    forecast_pv = 0.1 * 3  # q1..q3 tail (HH:15–HH:00)
    pv, load, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        forecast_pv_hourly=99.0,
        forecast_load_hourly=99.0,
        series_10min=series,
        soc_start_pct=50.0,
        forecast_q15_slots=None,
    )
    actual_load = actual_pv
    assert pv == round(actual_pv + forecast_pv, 3)
    assert load == round(actual_load + 0.2 * 3, 3)


def test_at_30_blends_first_three_10m_and_last_two_q15():
    now = datetime(2026, 6, 26, 18, 30)
    hour = 18
    kw = 3.0
    series = _flat_10m_kw(hour, kw)
    per_q = 0.15
    pv_q = _q15_hour(hour, per_q)

    actual = kw * TEN_MIN_KWH_PER_KW * 3
    forecast = per_q * 2
    pv, _, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=series,
        soc_start_pct=40.0,
    )
    assert pv == round(actual + forecast, 3)


def test_at_45_blends_half_hour_plus_partial_10m():
    now = datetime(2026, 6, 26, 18, 45)
    hour = 18
    series: list[float | None] = [None] * 144
    base = hour * 6
    for i in range(4):
        series[base + i] = 2.0
    series[base + 3] = 4.0
    s10 = {"pv": list(series), "load": list(series), "soc": list(series)}
    per_q = 0.2
    pv_q = _q15_hour(hour, per_q)

    first = 2.0 * TEN_MIN_KWH_PER_KW * 3
    partial = 4.0 * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE
    forecast = per_q
    pv, _, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=s10,
        soc_start_pct=40.0,
    )
    assert pv == round(first + partial + forecast, 3)


def test_sync_blended_current_hour_row_matches_q15_sums():
    """EA current-hour row bat/grid must follow blended q15, not optimizer hour totals."""
    q15 = [
        {"quarter": 0, "production": 0.5, "consumption": 0.3, "soc": 70.0,
         "battery": 0.1, "grid_import": 0.0, "grid_export": 0.0},
        {"quarter": 1, "production": 0.2, "consumption": 0.4, "soc": 71.0,
         "battery": -0.05, "grid_import": 0.1, "grid_export": 0.0},
        {"quarter": 2, "production": 0.15, "consumption": 0.25, "soc": 71.5,
         "battery": 0.02, "grid_import": 0.05, "grid_export": 0.03},
        {"quarter": 3, "production": 0.1, "consumption": 0.2, "soc": 72.0,
         "battery": 0.0, "grid_import": 0.0, "grid_export": 0.02},
    ]
    row = {
        "production": 9.99,
        "consumption": 8.88,
        "battery": 0.4,
        "bat_charge": 0.4,
        "bat_discharge": 0.0,
        "grid_import": 0.2,
        "grid_export": 0.0,
        "soc": 75.0,
        "buy_price": 0.5,
        "rce_price": 0.3,
        "g12_zone": "offpeak",
    }
    cfg = {
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.4,
            },
            "feed_in_price_pln": 0.2,
        },
    }

    sync_blended_current_hour_row(
        row,
        q15,
        production=0.95,
        consumption=1.15,
        soc=72.0,
        cfg=cfg,
        epsilon=0.05,
    )

    assert row["production"] == 0.95
    assert row["consumption"] == 1.15
    assert row["soc"] == 72.0
    assert row["soc_blended"] is True
    assert row["bat_charge"] == 0.12
    assert row["bat_discharge"] == 0.05
    assert row["battery"] == 0.07
    assert row["grid_import"] == 0.15
    assert row["grid_export"] == 0.05
    assert row["export_planned"] is True
    assert row["battery"] != 0.4
    assert row["grid_import"] != 0.2


def test_actual_battery_q15_from_influx_balance():
    hour = 17
    series = {
        "pv": [None] * 144,
        "load": [None] * 144,
        "grid_buy": [None] * 144,
        "grid_sell": [None] * 144,
        "soc": [None] * 144,
    }
    base = hour * 6
    series["pv"][base] = 3.0
    series["load"][base] = 0.6
    pv_q0 = 3.0 * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE
    load_q0 = 0.6 * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE
    bat, g_imp, g_exp = _actual_battery_flows_q15(hour, 0, series)
    assert g_imp == 0.0
    assert g_exp == 0.0
    assert bat == round(pv_q0 - load_q0, 4)


def test_blended_q15_past_quarter_uses_actual_battery_not_optimizer():
    with open("sa-config.yaml.example") as f:
        cfg = merge_simulation_defaults(yaml.safe_load(f))

    hour = 17
    now = datetime(2026, 6, 28, 17, 20)
    series = {
        "pv": [None] * 144,
        "load": [None] * 144,
        "grid_buy": [None] * 144,
        "grid_sell": [None] * 144,
        "soc": [None] * 144,
    }
    base = hour * 6
    series["pv"][base] = 3.0
    series["load"][base] = 0.6
    series["soc"][base] = 70.0
    series["soc"][base + 1] = 71.0

    opt_slots = [
        {
            "quarter": q,
            "soc_pct": 72.0 + q,
            "pv": 0.25,
            "load": 0.4,
            "battery_delta": 1.5,
            "grid_import": 0.2,
            "grid_export": 0.0,
            "grid_charge_kw": 0.0,
            "ctrl_battery_export_kwh": 0.0,
        }
        for q in range(4)
    ]

    q15 = build_blended_current_hour_q15(
        hour,
        now,
        forecast_pv_q15=[0.25] * 96,
        forecast_load_q15=[0.4] * 96,
        series_10min=series,
        soc_start_pct=70.0,
        soc_end_pct=72.0,
        opt_slots=opt_slots,
        cfg=cfg,
    )

    expected_q0 = _actual_battery_flows_q15(hour, 0, series)[0]
    assert q15[0]["battery"] == expected_q0
    assert q15[0]["battery"] != 1.5
