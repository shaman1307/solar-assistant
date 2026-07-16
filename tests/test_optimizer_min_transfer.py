"""Optimizer plan respects min_hourly_transfer_kwh for grid battery flows."""

from src.debug_smart_plan import (
    _hour_battery_grid_export_kwh,
    run_day_smart_q15_plan,
)


def _cfg(**timer_schedule) -> dict:
    return {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 24,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
        },
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 2.0,
            **timer_schedule,
        },
    }


def _rce_peak_afternoon() -> list[float]:
    q = [0.4] * 96
    for h in range(16, 19):
        for qi in range(4):
            q[h * 4 + qi] = 0.55 + (h - 16) * 0.15 + qi * 0.05
    return q


def test_plan_zeros_sub_threshold_bat_discharge():
    """Hours with <2 kWh planned battery grid export/discharge must be cleared."""
    pv = [0.0] * 24
    load = [0.7] * 24
    pv[16] = 4.61
    pv[17] = 3.04
    load[16] = 0.69
    load[17] = 0.82

    cfg_hi = _cfg(min_hourly_transfer_kwh=2.0)
    cfg_lo = _cfg(min_hourly_transfer_kwh=0.0)

    common = dict(
        date_str="2026-07-16",
        pv_hourly=pv,
        load_hourly=load,
        tomorrow_pv=[0.5] * 24,
        tomorrow_load=[1.0] * 24,
        rce_quarters=_rce_peak_afternoon(),
        initial_soc_kwh=42.0,
        from_hour=0,
    )

    plan_hi = run_day_smart_q15_plan(**common, cfg=cfg_hi)
    plan_lo = run_day_smart_q15_plan(**common, cfg=cfg_lo)
    assert plan_hi and plan_lo

    eps = 0.05
    micro_hours = []
    for h in range(24):
        slots_lo = (plan_lo.get("q15_by_hour") or {}).get(h) or []
        bat_dis = _hour_battery_grid_export_kwh(slots_lo)
        if bat_dis <= eps:
            bat_dis = sum(max(0.0, -float(s.get("battery_delta") or 0)) for s in slots_lo)
        has_export = bat_dis > eps or any(
            float(s.get("ctrl_battery_export_kwh") or 0) > eps for s in slots_lo
        )
        batt_grid = sum(max(0.0, float(s.get("battery_export_kwh") or 0)) for s in slots_lo)
        volume = batt_grid if batt_grid > eps else bat_dis
        if has_export and eps < volume < 2.0:
            micro_hours.append(h)

    assert micro_hours, "fixture should include sub-threshold battery export hours"
    for h in micro_hours:
        slots_hi = (plan_hi.get("q15_by_hour") or {}).get(h) or []
        batt_grid_hi = sum(max(0.0, float(s.get("battery_export_kwh") or 0)) for s in slots_hi)
        assert batt_grid_hi < 0.01, f"hour {h}: expected zero battery grid export, got {batt_grid_hi}"
        assert not any(float(s.get("ctrl_battery_export_kwh") or 0) > 0.001 for s in slots_hi)
