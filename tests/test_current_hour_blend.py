"""Current-hour actual+forecast blending for Energy arbitrage."""

from datetime import datetime

from src.plan_hourly_actuals import (
    PARTIAL_Q15_SCALE,
    TEN_MIN_KWH_PER_KW,
    blend_current_hour_end,
)


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
