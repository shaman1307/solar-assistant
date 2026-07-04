"""10-min battery charge/discharge in series_10min (Influx or energy-balance fallback)."""

import yaml
from datetime import datetime

from src.influxdb import SLOTS_CHART, _derive_battery_chart_kw, _split_battery_power_chart_kw
from src.plan_hourly_actuals import (
    SLOTS_PER_HOUR_10M,
    simulate_blended_current_hour_q15,
)
from src.simulation_config import merge_simulation_defaults


def _minimal_cfg() -> dict:
    with open("sa-config.yaml.example") as f:
        return merge_simulation_defaults(yaml.safe_load(f))


def test_split_battery_power_charge_and_discharge():
    bat = [None, 4.5, -2.0, 0.0]
    charge, discharge = _split_battery_power_chart_kw(bat)
    assert charge == [None, 4.5, 0.0, 0.0]
    assert discharge == [None, 0.0, 2.0, 0.0]


def test_derive_battery_from_pv_load_grid_surplus():
    """PV surplus with no grid flow → battery charge."""
    charge, discharge = _derive_battery_chart_kw(
        [6.0], [0.5], [0.0], [0.0],
    )
    assert charge == [5.5]
    assert discharge == [0.0]


def test_derive_battery_from_pv_load_grid_deficit():
    """Load exceeds PV → battery discharge."""
    charge, discharge = _derive_battery_chart_kw(
        [1.0], [3.0], [0.0], [0.0],
    )
    assert charge == [0.0]
    assert discharge == [2.0]


def test_derive_battery_respects_grid_import():
    charge, discharge = _derive_battery_chart_kw(
        [2.0], [3.0], [-1.5], [0.0],
    )
    assert charge == [0.5]
    assert discharge == [0.0]


def test_blended_soc_moves_with_populated_bat_series():
    """series_10min with derived bat_charge → SOC chains on completed q15 slots."""
    cfg = _minimal_cfg()
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    hour = 14
    now = datetime(2026, 7, 4, 14, 59)

    pv = [None] * SLOTS_CHART
    load = [None] * SLOTS_CHART
    grid_buy = [None] * SLOTS_CHART
    grid_sell = [None] * SLOTS_CHART
    base = hour * SLOTS_PER_HOUR_10M
    for i in range(6):
        pv[base + i] = 6.0
        load[base + i] = 0.5
        grid_buy[base + i] = 0.0
        grid_sell[base + i] = 0.0

    bat_charge, bat_discharge = _derive_battery_chart_kw(pv, load, grid_buy, grid_sell)
    series = {
        "pv": pv,
        "load": load,
        "grid_buy": grid_buy,
        "grid_sell": grid_sell,
        "bat_charge": bat_charge,
        "bat_discharge": bat_discharge,
    }

    soc_start = battery_cap * 0.79
    pv_by_q = [1.5, 1.5, 1.5, 1.5]
    load_by_q = [0.625, 0.625, 0.625, 0.625]
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4

    q15, soc_end = simulate_blended_current_hour_q15(
        soc_start, hour, now, pv_by_q, load_by_q, opt, series, cfg,
    )

    assert q15[0]["from_actual"] is True
    assert q15[0]["battery"] > 0
    assert q15[2]["soc"] > q15[0]["soc"]
    assert soc_end > soc_start
    expected_pct_gain = (soc_end - soc_start) / battery_cap * 100.0
    assert expected_pct_gain > 5.0


def test_blended_soc_derives_battery_from_energy_balance_when_bat_series_missing():
    """Without bat_charge/bat_discharge in series_10min, q15 SOC uses PV/load/grid balance."""
    from src.influxdb import SLOTS_CHART

    cfg = _minimal_cfg()
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    hour = 14
    now = datetime(2026, 7, 4, 14, 59)

    pv = [None] * SLOTS_CHART
    load = [None] * SLOTS_CHART
    grid_buy = [None] * SLOTS_CHART
    grid_sell = [None] * SLOTS_CHART
    base = hour * SLOTS_PER_HOUR_10M
    for i in range(6):
        pv[base + i] = 6.0
        load[base + i] = 0.5
        grid_buy[base + i] = 0.0
        grid_sell[base + i] = 0.0

    series = {
        "pv": pv,
        "load": load,
        "grid_buy": grid_buy,
        "grid_sell": grid_sell,
    }

    soc_start = battery_cap * 0.79
    pv_by_q = [1.5, 1.5, 1.5, 1.5]
    load_by_q = [0.625, 0.625, 0.625, 0.625]
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4

    q15, soc_end = simulate_blended_current_hour_q15(
        soc_start, hour, now, pv_by_q, load_by_q, opt, series, cfg,
    )

    assert q15[0]["from_actual"] is True
    assert q15[0]["battery"] > 0
    assert q15[2]["soc"] > q15[0]["soc"]
    assert soc_end > soc_start
    assert (soc_end - soc_start) / battery_cap * 100.0 > 5.0
