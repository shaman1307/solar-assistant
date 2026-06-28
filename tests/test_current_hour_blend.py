"""Current-hour blended PV/load + unified sim chain for Energy arbitrage."""

from datetime import datetime

import yaml

from src.plan_hourly_actuals import (
    PARTIAL_Q15_SCALE,
    TEN_MIN_KWH_PER_KW,
    blend_current_hour_end,
    blended_q15_pv_load_slots,
    build_blended_current_hour_q15,
    simulate_q15_slots,
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


def _minimal_cfg() -> dict:
    with open("sa-config.yaml.example") as f:
        return merge_simulation_defaults(yaml.safe_load(f))


def test_at_hour_start_uses_full_forecast():
    now = datetime(2026, 6, 26, 18, 0)
    pv_q = _q15_hour(18, 0.25)
    load_q = _q15_hour(18, 0.5)
    pv, load = blend_current_hour_end(
        18,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        forecast_pv_hourly=1.0,
        forecast_load_hourly=2.0,
        series_10min=None,
    )
    assert pv == 1.0
    assert load == 2.0


def test_at_15_blends_first_10m_scaled_and_last_q15():
    now = datetime(2026, 6, 26, 18, 15)
    hour = 18
    kw = 6.0
    series = _flat_10m_kw(hour, kw)
    pv_q = _q15_hour(hour, 0.1)
    load_q = _q15_hour(hour, 0.2)

    actual_pv = kw * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE
    forecast_pv = 0.1 * 3
    pv, load = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        forecast_pv_hourly=99.0,
        forecast_load_hourly=99.0,
        series_10min=series,
    )
    assert pv == round(actual_pv + forecast_pv, 3)
    assert load == round(actual_pv + 0.2 * 3, 3)

    slots = blended_q15_pv_load_slots(
        hour, now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        series_10min=series,
        pv_hourly=99.0,
        load_hourly=99.0,
    )
    assert slots[0][0] == round(actual_pv, 4)
    assert slots[1][0] == 0.1
    assert round(sum(s[0] for s in slots), 3) == pv


def test_at_30_blends_first_three_10m_and_last_two_q15():
    now = datetime(2026, 6, 26, 18, 30)
    hour = 18
    kw = 3.0
    series = _flat_10m_kw(hour, kw)
    per_q = 0.15
    pv_q = _q15_hour(hour, per_q)

    actual = kw * TEN_MIN_KWH_PER_KW * 3
    forecast = per_q * 2
    pv, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=series,
    )
    assert pv == round(actual + forecast, 3)

    slots = blended_q15_pv_load_slots(
        hour, now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        series_10min=series,
    )
    assert slots[0][0] == round(kw * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE, 4)
    assert slots[1][0] == round(kw * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE, 4)
    assert slots[2][0] == 0.15
    assert slots[3][0] == 0.15


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
    pv, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=s10,
    )
    assert pv == round(first + partial + forecast, 3)


def test_sim_chain_soc_accumulates_from_start():
    """SOC at each q15 = previous + sim delta (not Influx / optimizer override)."""
    cfg = _minimal_cfg()
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    soc_start_kwh = 5.0  # 50% of 10 kWh
    pv_by_q = [0.5, 0.5, 0.5, 0.5]
    load_by_q = [0.3, 0.3, 0.3, 0.3]
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4

    q15, soc_end = simulate_q15_slots(
        soc_start_kwh, 10, pv_by_q, load_by_q, opt, cfg,
    )
    assert len(q15) == 4
    assert q15[0]["soc"] >= 50.0
    assert q15[-1]["soc"] == round((soc_end / battery_cap) * 100.0, 1)
    assert soc_end > soc_start_kwh


def test_build_blended_uses_sim_not_influx_battery():
    cfg = _minimal_cfg()
    hour = 17
    now = datetime(2026, 6, 28, 17, 20)
    series = {
        "pv": [None] * 144,
        "load": [None] * 144,
        "grid_buy": [None] * 144,
        "grid_sell": [None] * 144,
    }
    base = hour * 6
    series["pv"][base] = 3.0
    series["load"][base] = 0.6

    opt_slots = [
        {
            "quarter": q,
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
        soc_start_kwh=7.0,
        opt_slots=opt_slots,
        cfg=cfg,
    )

    assert q15[0]["production"] == round(3.0 * TEN_MIN_KWH_PER_KW * PARTIAL_Q15_SCALE, 4)
    assert q15[0]["battery"] != 1.5
    assert q15[0]["soc"] != 0.0


def test_sync_blended_current_hour_row_matches_q15_sums():
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
