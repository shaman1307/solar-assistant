"""Pull vs freeze: open-tick Influx+forecast fill stays unfrozen until lag-ready."""

from __future__ import annotations

from datetime import datetime

import yaml

from src.plan_cache_merge import (
    _merge_current_hour_q15,
    datafix_completed_quarters_from_live,
)
from src.plan_hourly_actuals import (
    TEN_MIN_KWH_PER_KW,
    _freeze_through_index,
    _open_quarter_blend_kwh,
    _open_quarter_missing_frac,
    _open_quarter_partial_kwh,
    _refresh_slot_index,
    apply_open_pull_quarter_to_row,
    blended_q15_pv_load_slots,
    simulate_blended_current_hour_q15,
)
from src.simulation_config import merge_simulation_defaults


def _cfg() -> dict:
    with open("sa-config.yaml.example") as f:
        return merge_simulation_defaults(yaml.safe_load(f))


def _series(hour: int, kw: float, n_slots: int) -> dict[str, list[float | None]]:
    """First n_slots 10-min buckets of the hour; rest None (Influx lag)."""
    series: list[float | None] = [None] * 144
    base = hour * 6
    for i in range(n_slots):
        series[base + i] = kw
    return {
        "pv": list(series),
        "load": list(series),
        "bat_charge": list(series),
        "bat_discharge": [None] * 144,
        "grid_buy": [None] * 144,
        "grid_sell": [None] * 144,
        "soc": list(series),
    }


def _q15_forecast(hour: int, per_q: float) -> list[float]:
    out = [0.0] * 96
    for q in range(4):
        out[hour * 4 + q] = per_q
    return out


# ---------------------------------------------------------------------------
# Indexes: pull vs freeze must not share the same clock mapping
# ---------------------------------------------------------------------------


def test_pull_and_freeze_indexes_split_by_tick():
    hour = 8
    cases = [
        # (minute, pull, freeze_through)
        (0, -1, -1),
        (10, -1, -1),
        (15, 0, -1),
        (29, 0, -1),
        (30, 1, 1),
        (31, 1, 1),
        (44, 1, 1),
        (45, 2, 1),
        (59, 2, 1),
    ]
    for minute, pull, freeze in cases:
        now = datetime(2026, 8, 9, hour, minute)
        assert _refresh_slot_index(now, hour) == pull, minute
        assert _freeze_through_index(now, hour) == freeze, minute


def test_indexes_ignore_other_hour():
    now = datetime(2026, 8, 9, 8, 45)
    assert _refresh_slot_index(now, 9) == -1
    assert _freeze_through_index(now, 9) == -1


# ---------------------------------------------------------------------------
# Open-tick formula: influx_partial + forecast × (missing_min / 15)
# ---------------------------------------------------------------------------


def test_open_q0_at_15_uses_10min_fact_plus_forecast_third():
    """At :15 only slot0 (:00-:10) exists → missing 5 min → forecast/3."""
    hour = 10
    kw = 6.0
    series = _series(hour, kw, n_slots=1)["pv"]
    forecast = 0.3
    assert abs(_open_quarter_missing_frac(series, hour, 0) - (5.0 / 15.0)) < 1e-9
    partial = kw * TEN_MIN_KWH_PER_KW
    assert _open_quarter_partial_kwh(series, hour, 0) == partial
    expected = partial + forecast / 3.0
    assert _open_quarter_blend_kwh(series, hour, 0, forecast) == expected


def test_open_q1_at_30_uses_5min_fact_plus_forecast_two_thirds():
    """At :30 slots through :20 → q1 has half of slot1 only → missing 10/15."""
    hour = 10
    kw = 3.0
    series = _series(hour, kw, n_slots=2)["pv"]
    forecast = 0.45
    assert abs(_open_quarter_missing_frac(series, hour, 1) - (10.0 / 15.0)) < 1e-9
    partial = 0.5 * kw * TEN_MIN_KWH_PER_KW
    assert abs(_open_quarter_partial_kwh(series, hour, 1) - partial) < 1e-9
    expected = partial + forecast * (10.0 / 15.0)
    assert abs(_open_quarter_blend_kwh(series, hour, 1, forecast) - expected) < 1e-9


def test_open_q2_at_45_uses_10min_fact_plus_forecast_third():
    """At :45 slots through :40 → q2 = slot3 + missing :40-:45."""
    hour = 10
    kw = 2.0
    series = _series(hour, kw, n_slots=4)["pv"]
    forecast = 0.2
    assert abs(_open_quarter_missing_frac(series, hour, 2) - (5.0 / 15.0)) < 1e-9
    partial = kw * TEN_MIN_KWH_PER_KW
    expected = partial + forecast / 3.0
    assert abs(_open_quarter_blend_kwh(series, hour, 2, forecast) - expected) < 1e-9


def test_open_blend_full_window_equals_influx_no_forecast_fill():
    """When all weighted slots are present, missing=0 → pure Influx."""
    hour = 11
    kw = 4.0
    series = _series(hour, kw, n_slots=6)["pv"]
    assert _open_quarter_missing_frac(series, hour, 0) == 0.0
    # q0 = slot0 + 0.5·slot1
    expected = kw * TEN_MIN_KWH_PER_KW + 0.5 * kw * TEN_MIN_KWH_PER_KW
    assert abs(_open_quarter_blend_kwh(series, hour, 0, 99.0) - expected) < 1e-9


def test_open_blend_does_not_use_partial_q15_scale_invention():
    """Open tick must not invent energy via ×1.5; only fact + forecast fill."""
    hour = 12
    kw = 6.0
    series = _series(hour, kw, n_slots=1)["pv"]
    forecast = 0.0
    # With forecast=0, open = partial only (10 min), not scaled to 15.
    assert _open_quarter_blend_kwh(series, hour, 0, forecast) == kw * TEN_MIN_KWH_PER_KW


# ---------------------------------------------------------------------------
# Blended PV/load slots + simulate_blended flags
# ---------------------------------------------------------------------------


def test_blended_slots_at_45_freeze_q0_q1_open_q2_tail_q3():
    hour = 18
    now = datetime(2026, 8, 9, 18, 45)
    kw = 2.0
    series = _series(hour, kw, n_slots=4)
    per_q = 0.2
    fc = _q15_forecast(hour, per_q)

    slots = blended_q15_pv_load_slots(
        hour, now, forecast_pv_q15=fc, forecast_load_q15=fc, series_10min=series,
    )
    open_q2 = kw * TEN_MIN_KWH_PER_KW + per_q / 3.0
    assert abs(slots[2][0] - open_q2) < 1e-4
    assert slots[3][0] == per_q


def test_simulate_blended_from_actual_only_on_freeze_ready():
    """Frozen quarters get from_actual=True; open pull stays False."""
    cfg = _cfg()
    hour = 18
    now = datetime(2026, 8, 9, 18, 45)
    series = _series(hour, 2.0, n_slots=4)
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4
    q15, _ = simulate_blended_current_hour_q15(
        5.0, hour, now, [0.2] * 4, [0.1] * 4, opt, series, cfg,
    )
    assert q15[0]["from_actual"] is True
    assert q15[1]["from_actual"] is True
    assert q15[2]["from_actual"] is False  # open pull
    assert q15[3]["from_actual"] is False  # forecast tail


def test_simulate_blended_at_15_open_pull_not_frozen():
    cfg = _cfg()
    hour = 18
    now = datetime(2026, 8, 9, 18, 15)
    series = _series(hour, 3.0, n_slots=1)
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4
    q15, _ = simulate_blended_current_hour_q15(
        5.0, hour, now, [0.2] * 4, [0.1] * 4, opt, series, cfg,
    )
    assert all(s["from_actual"] is False for s in q15)
    # Open pull still moves SOC from Influx bat on q0 (partial + forecast fill).
    assert q15[0]["battery"] != 0.0 or q15[0]["consumption"] != 0.0


# ---------------------------------------------------------------------------
# Freeze-once + open pull in merge / datafix
# ---------------------------------------------------------------------------


def test_datafix_at_30_freezes_q0_and_q1():
    cfg = _cfg()
    hour = 8
    now = datetime(2026, 8, 9, 8, 30)
    series = _series(hour, 2.0, n_slots=3)
    row = {
        "hour": hour,
        "q15": [
            {
                "quarter": q,
                "production": 0.1,
                "consumption": 0.1,
                "soc": 50.0,
                "battery": 0.0,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": False,
            }
            for q in range(4)
        ],
        "buy_price": 0.5,
        "rce_price": 0.3,
        "g12_zone": "offpeak",
    }
    changed = datafix_completed_quarters_from_live(
        row,
        hour=hour,
        now=now,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
        live_soc_kwh=10.0,
    )
    assert changed is True
    assert row["q15"][0]["from_actual"] is True
    assert row["q15"][1]["from_actual"] is True
    assert row["q15"][2]["from_actual"] is False


def test_datafix_does_not_rewrite_already_frozen_q0():
    cfg = _cfg()
    hour = 8
    now = datetime(2026, 8, 9, 8, 45)
    series = _series(hour, 9.0, n_slots=4)
    frozen_pv = 1.23
    row = {
        "hour": hour,
        "q15": [
            {
                "quarter": 0,
                "production": frozen_pv,
                "consumption": 0.1,
                "soc": 51.0,
                "battery": 0.5,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": True,
            },
            {
                "quarter": 1,
                "production": 0.1,
                "consumption": 0.1,
                "soc": 52.0,
                "battery": 0.0,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": False,
            },
            {
                "quarter": 2,
                "production": 0.1,
                "consumption": 0.1,
                "soc": 52.0,
                "battery": 0.0,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": False,
            },
            {
                "quarter": 3,
                "production": 0.1,
                "consumption": 0.1,
                "soc": 52.0,
                "battery": 0.0,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": False,
            },
        ],
        "buy_price": 0.5,
        "rce_price": 0.3,
        "g12_zone": "offpeak",
    }
    datafix_completed_quarters_from_live(
        row,
        hour=hour,
        now=now,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
        live_soc_kwh=10.0,
    )
    assert row["q15"][0]["production"] == frozen_pv
    assert row["q15"][0]["from_actual"] is True
    assert row["q15"][1]["from_actual"] is True


def test_merge_at_45_keeps_frozen_and_copies_open_pull_from_fresh():
    cfg = _cfg()
    hour = 8
    now = datetime(2026, 8, 9, 8, 45)
    series = _series(hour, 2.0, n_slots=4)
    row = {
        "hour": hour,
        "timer_schedule": "Dis 08:00-09:00 7kW cap20%",
        "action": "Discharging to Grid",
        "hour_labels_locked": True,
        "buy_price": 0.5,
        "rce_price": 0.4,
        "g12_zone": "offpeak",
        "q15": [
            {
                "quarter": 0,
                "production": 0.5,
                "consumption": 0.1,
                "soc": 60.0,
                "battery": 0.2,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": True,
            },
            *[
                {
                    "quarter": q,
                    "production": 0.1,
                    "consumption": 0.1,
                    "soc": 60.0,
                    "battery": 0.0,
                    "grid_import": 0.0,
                    "grid_export": 0.0,
                    "from_actual": False,
                }
                for q in range(1, 4)
            ],
        ],
    }
    fresh = {
        "q15": [
            {
                "quarter": q,
                "production": 0.9 if q == 2 else 0.05,
                "consumption": 0.2,
                "soc": 55.0 + q,
                "battery": -0.1,
                "grid_import": 0.0,
                "grid_export": 0.3 if q == 2 else 0.0,
                "from_actual": False,
            }
            for q in range(4)
        ],
    }
    _merge_current_hour_q15(
        row,
        now=now,
        hour=hour,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
        fresh_row=fresh,
    )
    # q0 already frozen — untouched production
    assert row["q15"][0]["from_actual"] is True
    assert row["q15"][0]["production"] == 0.5
    # q1 newly freeze-ready
    assert row["q15"][1]["from_actual"] is True
    # open pull + tail from fresh, not frozen
    assert row["q15"][2]["from_actual"] is False
    assert row["q15"][2]["production"] == 0.9
    assert row["q15"][3]["from_actual"] is False


def test_apply_open_pull_prev_q3_stays_unfrozen():
    """Previous-hour q3 at :00 is open-pulled; from_actual stays False until :15."""
    cfg = _cfg()
    hour = 7
    series = _series(hour, 1.5, n_slots=6)
    row = {
        "hour": hour,
        "buy_price": 0.5,
        "rce_price": 0.3,
        "g12_zone": "offpeak",
        "q15": [
            {
                "quarter": q,
                "production": 0.1,
                "consumption": 0.2,
                "soc": 40.0 + q,
                "battery": -0.05,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": q < 3,
            }
            for q in range(4)
        ],
    }
    before = float(row["q15"][3]["production"])
    ok = apply_open_pull_quarter_to_row(
        row, hour, 3, series_10min=series, cfg=cfg,
    )
    assert ok is True
    assert row["q15"][3]["from_actual"] is False
    # Full window present → open = Influx q3, not the old forecast stub.
    assert row["q15"][3]["production"] != before
    # Second call still allowed (not frozen) — refreshes again.
    ok2 = apply_open_pull_quarter_to_row(
        row, hour, 3, series_10min=series, cfg=cfg,
    )
    assert ok2 is True
    assert row["q15"][3]["from_actual"] is False


def test_apply_open_pull_skips_already_frozen_slot():
    cfg = _cfg()
    hour = 7
    series = _series(hour, 2.0, n_slots=6)
    row = {
        "hour": hour,
        "buy_price": 0.5,
        "rce_price": 0.3,
        "g12_zone": "offpeak",
        "q15": [
            {
                "quarter": q,
                "production": 1.11,
                "consumption": 0.2,
                "soc": 50.0,
                "battery": 0.0,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": True,
            }
            for q in range(4)
        ],
    }
    assert apply_open_pull_quarter_to_row(
        row, hour, 3, series_10min=series, cfg=cfg,
    ) is False
    assert row["q15"][3]["production"] == 1.11
