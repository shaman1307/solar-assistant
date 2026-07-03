"""Baseline load-priority costs for monthly history."""

from src.plan_baseline import (
    attach_baseline_savings,
    build_baseline_history_rows,
    summarize_baseline_rows,
)
from src.simulation_config import get_simulation_params


def _cfg() -> dict:
    return {
        "battery": {"capacity_kwh": 10.0},
        "inverter": {"ac_capacity_kw": 5.0},
        "simulation": {"min_soc_pct": 15, "epsilon_kwh": 0.05},
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


def _hourly_day() -> dict[str, list[float | None]]:
    pv = [0.0] * 24
    load = [1.0] * 24
    pv[12] = 5.0
    return {
        "pv": pv,
        "load": load,
        "soc": [50.0] * 24,
        "bat_charge": [0.0] * 24,
        "bat_discharge": [0.0] * 24,
        "grid_buy": [0.0] * 24,
        "grid_sell": [0.0] * 24,
    }


def _rce_quarters() -> list[float | None]:
    return [0.5] * 96


def test_attach_baseline_savings_math():
    summary = attach_baseline_savings(
        {"energy_cost": 3.0, "service_cost": 1.0},
        [
            {"energy_cost": 2.0, "service_cost": 0.5},
            {"energy_cost": 1.0, "service_cost": 0.25},
        ],
    )
    assert summary["baseline_cost"] == 3.75
    assert summary["savings_pln"] == -0.25


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
    assert all("energy_cost" in r for r in rows)
    totals = summarize_baseline_rows(rows)
    assert totals["baseline_cost"] == round(
        totals["baseline_energy_cost"] + totals["baseline_service_cost"],
        2,
    )