"""Blended current hour respects live SA discharge timer on tail q15 slots."""

from datetime import datetime

from src.plan_hourly_actuals import simulate_blended_current_hour_q15
from src.timer_plan import sa_discharge_slot_active_at, sa_discharge_timer_for_hour


def _cfg():
    return {
        "battery": {"capacity_kwh": 20.0, "max_discharge_power_kw": 8.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 24,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.4,
            },
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.62,
        },
    }


def test_sa_timer_projects_export_on_tail_quarter():
    rules = {
        "timed_discharge_enabled": True,
        "discharge_slots": [{
            "from": "07:00", "to": "08:00", "power_kw": 8.0, "capacity_pct": 16,
        }],
    }
    sa_timer = sa_discharge_timer_for_hour(rules, 7)
    now = datetime(2026, 7, 7, 7, 47)
    opt_slots = [{"grid_export": 0.0, "battery_delta": -0.1} for _ in range(4)]
    q15, _ = simulate_blended_current_hour_q15(
        soc_start_kwh=10.0,
        hour=7,
        now=now,
        pv_by_q=[0.03, 0.03, 0.03, 0.03],
        load_by_q=[0.14, 0.14, 0.14, 0.14],
        opt_slots=opt_slots,
        series_10min=None,
        cfg=_cfg(),
        sa_timer_txt=sa_timer,
    )
    assert q15[3]["from_actual"] is False
    assert float(q15[3]["grid_export"]) > 0.0


def test_sa_discharge_timer_for_hour_skips_inactive_slot():
    rules = {
        "timed_discharge_enabled": False,
        "discharge_slots": [
            {"from": "07:00", "to": "08:00", "power_kw": 8.0, "capacity_pct": 16},
            {"from": "00:00", "to": "00:00", "power_kw": 0.0, "capacity_pct": 1},
        ],
    }
    assert sa_discharge_timer_for_hour(rules, 7) == "Dis 07:00-08:00 8kW cap16%"
    assert sa_discharge_timer_for_hour(rules, 9) == ""


def test_sa_discharge_timer_uses_min_soc_from_cfg_when_cap_missing():
    rules = {
        "discharge_slots": [{"from": "07:00", "to": "08:00", "power_kw": 8.0}],
    }
    cfg = {"simulation": {"min_soc_pct": 16}}
    assert sa_discharge_timer_for_hour(rules, 7, cfg=cfg) == "Dis 07:00-08:00 8kW cap16%"


def test_sa_discharge_slot_active_at():
    rules = {
        "discharge_slots": [{"from": "07:00", "to": "08:00", "power_kw": 8.0, "capacity_pct": 16}],
    }
    from datetime import datetime
    from zoneinfo import ZoneInfo

    active = datetime(2026, 7, 7, 7, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    ended = datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert sa_discharge_slot_active_at(rules, active) is True
    assert sa_discharge_slot_active_at(rules, ended) is False
