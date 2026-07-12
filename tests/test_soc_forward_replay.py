"""Forward SOC replay from blended current-hour anchor."""

from src.plan_hourly_actuals import replay_forward_soc_on_rows


def _minimal_cfg() -> dict:
    return {
        "battery": {"capacity_kwh": 10.0, "max_discharge_power_kw": 8.0, "max_charge_power_kw": 5.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 15,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "offpeak_price_pln_kwh": 0.62,
                "offpeak_energy_only_pln_kwh": 0.45,
                "peak_price_pln_kwh": 0.9,
                "peak_energy_only_pln_kwh": 0.7,
            },
            "feed_in_price_pln": 0.3,
        },
    }


def test_replay_chains_soc_from_anchor():
    """Future hour SOC comes from simulate_hour play, not rebased deltas."""
    date = "2026-06-28"
    rows = [
        {
            "hour": 9,
            "plan_date": date,
            "production": 1.0,
            "consumption": 0.5,
            "soc": 50.0,
            "q15": [],
        },
    ]
    opt_slots = [
        {
            "quarter": q,
            "pv": 1.0,
            "load": 0.0,
            "grid_charge_kw": 0.0,
            "ctrl_battery_export_kwh": 0.0,
            "reserve_kwh": 1.5,
        }
        for q in range(4)
    ]
    pv_q15 = [0.0] * 96
    for q in range(4):
        pv_q15[9 * 4 + q] = 1.0
    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=5.0,
        q15_plan_by_date={date: {9: opt_slots}},
        pv_q15_by_date={date: pv_q15},
        load_q15_by_date={date: [0.0] * 96},
        cfg=_minimal_cfg(),
    )
    assert len(rows[0]["q15"]) == 4
    assert rows[0]["soc"] == rows[0]["q15"][-1]["soc"]
    assert rows[0]["soc"] > 50.0


def test_replay_stores_q15_pv_load():
    date = "2026-06-28"
    rows = [{"hour": 10, "plan_date": date, "production": 2.0, "consumption": 1.0, "soc": 40.0}]
    pv_q15 = [0.0] * 96
    for q in range(4):
        pv_q15[10 * 4 + q] = 0.5
    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=4.0,
        q15_plan_by_date={date: {10: [{"quarter": q} for q in range(4)]}},
        pv_q15_by_date={date: pv_q15},
        load_q15_by_date={date: [0.25] * 96},
        cfg=_minimal_cfg(),
    )
    assert rows[0]["q15"][0]["production"] == 0.5
    assert rows[0]["q15"][0]["consumption"] == 0.25
    assert rows[0]["production"] == 2.0
    assert rows[0]["consumption"] == 1.0


def test_replay_refreshes_cost_when_grid_import_zeroed():
    """Stale optimizer import_cost must clear when replay sets grid_import to 0."""
    date = "2026-06-30"
    rows = [
        {
            "hour": 5,
            "plan_date": date,
            "production": 0.092,
            "consumption": 0.496,
            "soc": 16.8,
            "buy_price": 0.6229,
            "g12_zone": "offpeak",
            "grid_import": 0.08,
            "grid_export": 0.0,
            "import_cost": 0.0501,
            "energy_cost": 0.04,
            "service_cost": 0.0125,
            "cost": 0.0501,
        },
    ]
    opt_slots = [
        {
            "quarter": q,
            "pv": 0.023,
            "load": 0.124,
            "grid_charge_kw": 0.0,
            "ctrl_battery_export_kwh": 0.0,
            "reserve_kwh": 1.5,
        }
        for q in range(4)
    ]
    pv_q15 = [0.0] * 96
    load_q15 = [0.0] * 96
    for q in range(4):
        pv_q15[5 * 4 + q] = 0.023
        load_q15[5 * 4 + q] = 0.124
    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=3.0,
        q15_plan_by_date={date: {5: opt_slots}},
        pv_q15_by_date={date: pv_q15},
        load_q15_by_date={date: load_q15},
        cfg=_minimal_cfg(),
    )
    assert rows[0]["grid_import"] == 0.0
    assert rows[0]["import_cost"] == 0.0
    assert rows[0]["energy_cost"] == 0.0
    assert rows[0]["service_cost"] == 0.0
    assert rows[0]["cost"] == 0.0


def test_replay_clears_stale_timer_when_reserve_blocks_export():
    """Optimizer timer must clear when replay SOC chain blocks battery export."""
    date = "2026-07-12"
    rows = [
        {
            "hour": 1,
            "plan_date": date,
            "production": 0.0,
            "consumption": 0.707,
            "soc": 18.0,
            "buy_price": 0.6229,
            "g12_zone": "offpeak",
            "rce_price": 0.6968,
            "grid_import": 0.0,
            "grid_export": 2.0,
            "timer_schedule": "Dis 01:00-01:30 8.0kW cap16%",
            "action": "Discharging to Grid and Load",
        },
    ]
    opt_slots = [
        {
            "quarter": q,
            "pv": 0.0,
            "load": 0.177,
            "grid_charge_kw": 0.0,
            "ctrl_battery_export_kwh": 2.0 if q < 2 else 0.0,
            "reserve_kwh": 4.0,
        }
        for q in range(4)
    ]
    load_q15 = [0.0] * 96
    for q in range(4):
        load_q15[1 * 4 + q] = 0.177
    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=2.0,
        q15_plan_by_date={date: {1: opt_slots}},
        pv_q15_by_date={date: [0.0] * 96},
        load_q15_by_date={date: load_q15},
        cfg=_minimal_cfg(),
    )
    assert rows[0]["grid_export"] == 0.0
    assert rows[0]["timer_schedule"] == ""
    assert rows[0]["action"] == "Discharging to Load"
    assert rows[0]["export_planned"] is False
