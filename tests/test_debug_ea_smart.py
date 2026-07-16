"""Debug smart column uses the same EA plan rows as Rules."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.debug_plan import (
    ea_row_to_smart_view,
    ea_rows_by_hour_for_date,
    merge_ea_plan_into_debug_day,
)
from src.simulation import (
    apply_locked_hour_labels_from_plan,
    build_energy_arbitrage_plan,
    run_simulation,
)

WARSAW = ZoneInfo("Europe/Warsaw")


def _cfg() -> dict:
    return {
        "battery": {
            "capacity_kwh": 10.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 15,
            "epsilon_kwh": 0.05,
            "horizon_hours": 30,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
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


def _forecast() -> dict:
    pv = [0.0] * 24
    load = [1.0] * 24
    pv[12] = 4.0
    return {
        "today": {
            "pv": pv,
            "load": load,
            "pv_forecast": pv,
            "load_forecast": load,
            "pv_total": sum(pv),
            "load_total": sum(load),
        },
        "tomorrow": {
            "pv": [0.5] * 24,
            "load": [1.2] * 24,
            "pv_total": 12.0,
            "load_total": 28.8,
        },
        "meta": {},
    }


def _metrics() -> dict:
    return {
        "battery_soc": 55.0,
        "today_hourly": {
            "pv": [0.0] * 24,
            "load": [1.0] * 24,
            "soc": [55.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
    }


def test_run_simulation_delegates_to_builder():
    cfg = _cfg()
    forecast = _forecast()
    metrics = _metrics()
    rules: dict = {}
    direct = build_energy_arbitrage_plan(forecast, metrics, rules, cfg)
    wrapped = run_simulation(forecast, metrics, rules, cfg)
    assert direct.keys() == wrapped.keys()
    assert len(direct["rows"]) == len(wrapped["rows"])


def test_merge_ea_plan_attaches_smart_action_timer():
    today = "2026-07-16"
    ea_plan = {
        "today_date": today,
        "plan_from_hour": 2,
        "history_rows": [
            {
                "hour": 0,
                "plan_date": today,
                "grid_import": 0.5,
                "grid_export": 0.0,
                "bat_charge": 0.1,
                "bat_discharge": 0.0,
                "soc": 54.0,
                "action": "Charge",
                "timer_schedule": "00:00-00:15 Chg",
                "buy_price": 0.5,
                "rce_q15": [0.4, 0.4, 0.4, 0.4],
            },
        ],
        "rows": [
            {
                "hour": 10,
                "plan_date": today,
                "grid_import": 0.0,
                "grid_export": 1.2,
                "bat_charge": 0.0,
                "bat_discharge": 0.8,
                "soc": 62.0,
                "action": "Export",
                "timer_schedule": "10:00-10:30 Dis",
                "buy_price": 0.6,
                "rce_q15": [0.7, 0.7, 0.7, 0.7],
            },
            {
                "hour": 3,
                "plan_date": "2026-07-17",
                "grid_import": 0.2,
                "grid_export": 0.0,
                "bat_charge": 0.0,
                "bat_discharge": 0.0,
                "soc": 60.0,
                "action": "Hold",
                "timer_schedule": "",
            },
        ],
    }
    day = {
        "date": today,
        "rows": [
            {"hour": 0, "pv": 0.0, "load": 1.0, "actual": {}, "sim": {}},
            {"hour": 10, "pv": 4.0, "load": 1.0, "actual": {}, "sim": {}},
            {"hour": 11, "pv": 0.0, "load": 1.0, "actual": {}, "sim": {}},
        ],
    }
    merge_ea_plan_into_debug_day(day, ea_plan, today)

    h0 = day["rows"][0]
    assert h0["action"] == "Charge"
    assert h0["timer_schedule"] == "00:00-00:15 Chg"
    assert h0["smart"]["grid_used"] == 0.5
    assert h0["smart"]["soc"] == 54.0
    assert "cost" not in h0["smart"]

    h10 = day["rows"][1]
    assert h10["action"] == "Export"
    assert h10["smart"]["grid_export"] == 1.2

    h11 = day["rows"][2]
    assert "smart" not in h11 or h11.get("smart") is None


def test_ea_rows_by_hour_includes_history_and_plan():
    today = "2026-07-16"
    ea_plan = {
        "today_date": today,
        "history_rows": [{"hour": 1, "plan_date": today, "action": "A"}],
        "rows": [
            {"hour": 5, "plan_date": today, "action": "B"},
            {"hour": 2, "plan_date": "2026-07-17", "action": "C"},
        ],
    }
    by_hour = ea_rows_by_hour_for_date(ea_plan, today)
    assert by_hour[1]["action"] == "A"
    assert by_hour[5]["action"] == "B"
    assert 2 not in by_hour

    tomorrow = ea_rows_by_hour_for_date(ea_plan, "2026-07-17")
    assert tomorrow[2]["action"] == "C"


def test_apply_locked_hour_labels_restores_mid_hour():
    today = "2026-07-16"
    hour = 10
    existing = {
        "rows": [
            {
                "plan_date": today,
                "hour": hour,
                "hour_labels_locked": True,
                "timer_schedule": "10:00-10:45 Dis",
                "action": "Discharge",
            },
        ],
    }
    fresh = {
        "rows": [
            {
                "plan_date": today,
                "hour": hour,
                "timer_schedule": "10:00-10:15 Chg",
                "action": "Charge",
            },
        ],
    }
    now = datetime(2026, 7, 16, 10, 30, tzinfo=WARSAW)
    apply_locked_hour_labels_from_plan(fresh, existing, now)
    row = fresh["rows"][0]
    assert row["timer_schedule"] == "10:00-10:45 Dis"
    assert row["action"] == "Discharge"
    assert row["hour_labels_locked"] is True


def test_ea_row_to_smart_view_maps_grid_import():
    view = ea_row_to_smart_view({
        "grid_import": 1.5,
        "grid_export": 0.2,
        "bat_charge": 0.3,
        "bat_discharge": 0.4,
        "soc": 71.5,
        "cost": 9.99,
    })
    assert view["grid_used"] == 1.5
    assert view["soc"] == 71.5
    assert "cost" not in view
