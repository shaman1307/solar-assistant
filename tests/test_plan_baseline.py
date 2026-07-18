"""Baseline SA two-rule costs for monthly history."""

import pytest

from src.plan_baseline import (
    BASELINE_SA_HEADER_TITLE,
    attach_baseline_savings,
    build_baseline_history_rows,
    summarize_baseline_rows,
)
from src.simulation_config import get_simulation_params


def _cfg() -> dict:
    return {
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 15,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_battery": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
            "feed_in_price_pln": 0.4,
        },
    }


def _hourly_day(*, day_pv: float = 5.0, start_soc: float = 80.0) -> dict[str, list[float | None]]:
    pv = [0.0] * 24
    load = [0.5] * 24
    # Put day PV at noon so charge rule sees day_pv < 25
    pv[12] = day_pv
    return {
        "pv": pv,
        "load": load,
        "soc": [start_soc] * 24,
        "bat_charge": [0.0] * 24,
        "bat_discharge": [0.0] * 24,
        "grid_buy": [0.0] * 24,
        "grid_sell": [0.0] * 24,
    }


def _rce_quarters() -> list[float | None]:
    return [0.5] * 96


def test_attach_baseline_savings_math():
    summary = attach_baseline_savings(
        {
            "export_revenue": 10.0,
            "import_energy_cost": 3.0,
            "service_cost": 1.0,
        },
        [
            {"export_revenue": 8.0, "import_energy_cost": 2.0, "service_cost": 0.5},
            {"export_revenue": 4.0, "import_energy_cost": 1.0, "service_cost": 0.25},
        ],
    )
    # Baseline (1) = 12 − 3 = 9; actual (2) = 10 − 3 = 7; Saved = 7 − 9 = −2
    assert summary["baseline_export_revenue"] == 12.0
    assert summary["baseline_import_energy_cost"] == 3.0
    assert summary["baseline_cost"] == 9.0
    assert summary["savings_pln"] == -2.0


def test_summarize_baseline_import_zones():
    totals = summarize_baseline_rows([
        {
            "export_revenue": 1.0, "import_energy_cost": 0.5, "service_cost": 0.1,
            "g12_zone": "peak", "grid_import": 2.0,
        },
        {
            "export_revenue": 2.0, "import_energy_cost": 1.0, "service_cost": 0.2,
            "g12_zone": "offpeak", "grid_import": 3.0,
        },
    ])
    assert totals["baseline_grid_import_peak"] == 2.0
    assert totals["baseline_grid_import_offpeak"] == 3.0
    assert totals["baseline_grid_import"] == 5.0
    assert totals["baseline_cost"] == 1.5  # (1+2) − (0.5+1)


def test_baseline_replay_produces_hourly_costs():
    cfg = _cfg()
    params = get_simulation_params(cfg)
    hourly = _hourly_day()
    quarters = {"2026-06-01": _rce_quarters()}
    rows = build_baseline_history_rows(
        "2026-06-01",
        24,
        hourly,
        quarters,
        cfg,
        params,
    )
    assert len(rows) == 24
    assert all("export_revenue" in r for r in rows)
    totals = summarize_baseline_rows(rows)
    assert totals["baseline_cost"] == round(
        totals["baseline_export_revenue"] - totals["baseline_import_energy_cost"],
        4,
    )


def test_baseline_physics_only_no_grid_charge_or_export_dump():
    """No timer Chg/Dis: night import only if SOC hits min; no evening dump."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    hourly = _hourly_day(day_pv=10.0, start_soc=40.0)
    for h in range(0, 6):
        hourly["load"][h] = 1.0
    quarters = {"2026-06-01": _rce_quarters()}
    rows = build_baseline_history_rows(
        "2026-06-01", 24, hourly, quarters, cfg, params,
    )
    night_import = sum(float(rows[h]["grid_import"]) for h in (1, 2, 3))
    assert night_import < 0.2
    evening_export = sum(float(rows[h]["grid_export"]) for h in (19, 20, 21, 22))
    assert evening_export < 0.2


def test_baseline_overflow_exports_when_battery_full():
    """PV surplus with full battery exports to grid (overflow only)."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    hourly = _hourly_day(day_pv=0.0, start_soc=100.0)
    hourly["load"] = [0.0] * 24
    hourly["pv"][12] = 8.0
    hourly["load"][12] = 0.5
    quarters = {"2026-06-01": _rce_quarters()}
    rows = build_baseline_history_rows(
        "2026-06-01", 24, hourly, quarters, cfg, params,
    )
    assert float(rows[12]["grid_export"]) > 1.0
    assert float(rows[12]["grid_import"]) < 0.2


def test_simulate_hour_charge_keeps_load_on_battery():
    """Grid charge does not force house load onto the meter."""
    from src.plan_optimizer import HourControl, simulate_hour

    params = get_simulation_params(_cfg())
    min_kwh = 43.0 * 0.15
    phys = simulate_hour(
        20.0, 0.0, 2.0, HourControl(6.0, 0.0, load_from_grid=False),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        eta_grid=float(params["eta_grid_battery"]),
        eta_out=float(params["eta_battery_out"]),
        eta_pv_load=float(params["eta_pv_load"]),
        eta_pv_grid=float(params["eta_pv_grid"]),
        eta_pv_battery=float(params["eta_pv_battery"]),
        epsilon=0.05,
    )
    # Import only for battery charge (6 kWh AC); 2 kWh load from battery.
    assert phys.grid_import == pytest.approx(6.0, abs=0.01)
    assert phys.battery_delta == pytest.approx(6.0 * 0.925 - 2.0 / 0.925, abs=0.05)


def test_simulate_hour_charge_at_min_nets_charge_minus_house():
    """At min SOC, Chg still supplies house from the same-hour charge."""
    from src.plan_optimizer import HourControl, simulate_hour

    params = get_simulation_params(_cfg())
    min_kwh = 43.0 * 0.17
    start = min_kwh
    load = 1.0
    phys = simulate_hour(
        start, 0.0, load, HourControl(6.0, 0.0, load_from_grid=False),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        eta_grid=float(params["eta_grid_battery"]),
        eta_out=float(params["eta_battery_out"]),
        eta_pv_load=float(params["eta_pv_load"]),
        eta_pv_grid=float(params["eta_pv_grid"]),
        eta_pv_battery=float(params["eta_pv_battery"]),
        epsilon=0.05,
    )
    assert phys.grid_import == pytest.approx(6.0, abs=0.01)
    assert phys.battery_delta == pytest.approx(6.0 * 0.925 - load / 0.925, abs=0.05)
    assert phys.soc_end == pytest.approx(start + 6.0 * 0.925 - load / 0.925, abs=0.05)


def test_baseline_header_title_physics_only():
    assert "Physics baseline" in BASELINE_SA_HEADER_TITLE
    assert "PV only" in BASELINE_SA_HEADER_TITLE
    assert "overflow" in BASELINE_SA_HEADER_TITLE.lower()
