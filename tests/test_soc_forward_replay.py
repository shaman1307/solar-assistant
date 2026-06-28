"""Forward SOC replay from blended current-hour anchor."""

from src.plan_hourly_actuals import replay_forward_soc_on_rows


def _minimal_cfg() -> dict:
    return {
        "battery": {"capacity_kwh": 10.0, "max_discharge_power_kw": 8.0},
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


def _q15_bat_sums(q15: list[dict]) -> tuple[float, float]:
    charge = round(sum(max(0.0, float(s.get("battery") or 0)) for s in q15), 3)
    discharge = round(sum(max(0.0, -float(s.get("battery") or 0)) for s in q15), 3)
    return charge, discharge


def _assert_row_battery_matches_q15(row: dict) -> None:
    q15 = row["q15"]
    q15_charge, q15_discharge = _q15_bat_sums(q15)
    assert row["bat_charge"] == q15_charge
    assert row["bat_discharge"] == q15_discharge
    assert row["battery"] == round(
        sum(float(s.get("battery") or 0) for s in q15), 3
    )


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
    # PV charges battery: 1 kWh per q15, no load, no export control
    pv_q15 = [0.0] * 96
    for q in range(4):
        pv_q15[9 * 4 + q] = 1.0
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
    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=5.0,
        q15_plan_by_date={date: {9: opt_slots}},
        pv_q15_by_date={date: pv_q15},
        load_q15_by_date={date: [0.0] * 96},
        pv_hourly_by_date={date: [0.0] * 9 + [4.0] + [0.0] * 14},
        load_hourly_by_date={date: [0.0] * 24},
        cfg=_minimal_cfg(),
    )
    assert len(rows[0]["q15"]) == 4
    assert rows[0]["soc"] == rows[0]["q15"][-1]["soc"]
    assert rows[0]["soc"] > 50.0
    assert rows[0]["bat_charge"] > 0.0
    assert rows[0]["bat_discharge"] == 0.0
    _assert_row_battery_matches_q15(rows[0])


def test_replay_bat_discharge_sums_q15_negative_deltas():
    """Future hour bat_discharge equals sum of negative q15 battery deltas."""
    date = "2026-06-28"
    rows = [
        {
            "hour": 20,
            "plan_date": date,
            "production": 0.0,
            "consumption": 2.0,
            "soc": 80.0,
        },
    ]
    load_q15 = [0.0] * 96
    for q in range(4):
        load_q15[20 * 4 + q] = 0.5

    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=8.0,
        q15_plan_by_date={date: {20: [{"quarter": q} for q in range(4)]}},
        pv_q15_by_date={date: [0.0] * 96},
        load_q15_by_date={date: load_q15},
        pv_hourly_by_date={date: [0.0] * 24},
        load_hourly_by_date={date: [0.0] * 20 + [2.0] + [0.0] * 3},
        cfg=_minimal_cfg(),
    )

    assert rows[0]["bat_discharge"] > 0.0
    assert rows[0]["bat_charge"] == 0.0
    assert all(float(s.get("battery") or 0) <= 0 for s in rows[0]["q15"])
    _assert_row_battery_matches_q15(rows[0])


def test_replay_bat_charge_and_discharge_chain_across_hours():
    """Multi-hour replay: each row bat_charge/bat_discharge matches q15 sums."""
    date = "2026-06-28"
    rows = [
        {"hour": 20, "plan_date": date, "production": 0.0, "consumption": 1.0, "soc": 70.0},
        {"hour": 21, "plan_date": date, "production": 4.0, "consumption": 0.5, "soc": 60.0},
    ]
    load_q15 = [0.0] * 96
    pv_q15 = [0.0] * 96
    for q in range(4):
        load_q15[20 * 4 + q] = 0.25
        pv_q15[21 * 4 + q] = 1.0
        load_q15[21 * 4 + q] = 0.125

    replay_forward_soc_on_rows(
        rows,
        anchor_soc_kwh=7.0,
        q15_plan_by_date={
            date: {
                20: [{"quarter": q} for q in range(4)],
                21: [{"quarter": q} for q in range(4)],
            },
        },
        pv_q15_by_date={date: pv_q15},
        load_q15_by_date={date: load_q15},
        pv_hourly_by_date={date: [0.0] * 21 + [4.0] + [0.0] * 2},
        load_hourly_by_date={date: [0.0] * 20 + [1.0, 0.5] + [0.0] * 2},
        cfg=_minimal_cfg(),
    )

    assert rows[0]["bat_discharge"] > 0.0
    assert rows[0]["bat_charge"] == 0.0
    assert rows[1]["bat_charge"] > 0.0
    assert rows[1]["bat_discharge"] == 0.0
    assert rows[1]["soc"] > rows[0]["soc"]
    for row in rows:
        _assert_row_battery_matches_q15(row)


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
        pv_hourly_by_date={date: [0.0] * 10 + [2.0] + [0.0] * 13},
        load_hourly_by_date={date: [0.0] * 10 + [1.0] + [0.0] * 13},
        cfg=_minimal_cfg(),
    )
    assert rows[0]["q15"][0]["production"] == 0.5
    assert rows[0]["q15"][0]["consumption"] == 0.25
    assert rows[0]["production"] == 2.0
    assert rows[0]["consumption"] == 1.0
    _assert_row_battery_matches_q15(rows[0])
