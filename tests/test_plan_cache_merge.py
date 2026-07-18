"""Tests for SQLite-backed incremental Energy arbitrage plan merge."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.plan_cache_merge import (
    _apply_actual_quarter_if_needed,
    _ensure_q15_length,
    _merge_current_hour_q15,
    last_completed_quarter_tick,
    merge_incremental_plan,
    plan_needs_full_rebuild,
)


def _cfg():
    return {
        "battery": {"capacity_kwh": 20.0},
        "simulation": {"min_soc_pct": 16},
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


def _row(hour: int, *, timer: str = "", action: str = "Idle", locked: bool = False):
    return {
        "hour": hour,
        "plan_date": "2026-07-07",
        "start": f"07-07-2026 {hour + 1:02d}:00",
        "timer_schedule": timer,
        "action": action,
        "hour_labels_locked": locked,
        "production": 1.0,
        "consumption": 0.5,
        "battery": 0.0,
        "bat_charge": 0.0,
        "bat_discharge": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "soc": 50.0,
        "buy_price": 1.0,
        "g12_zone": "offpeak",
        "q15": [
            {"quarter": q, "production": 0.25, "consumption": 0.1, "soc": 50.0,
             "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0,
             "from_actual": False}
            for q in range(4)
        ],
    }


def test_last_completed_quarter_tick():
    tz = ZoneInfo("Europe/Warsaw")
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 0, tzinfo=tz)) == (-1, 3)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 5, tzinfo=tz)) == (0, -1)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 15, tzinfo=tz)) == (0, 0)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 30, tzinfo=tz)) == (0, 1)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 45, tzinfo=tz)) == (0, 2)


def test_plan_needs_full_rebuild_on_new_day():
    cached = {"today_date": "2026-07-06"}
    now = datetime(2026, 7, 7, 8, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert plan_needs_full_rebuild(cached, now) is True
    assert plan_needs_full_rebuild(None, now) is True


def test_merge_keeps_locked_timer_on_current_hour():
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [_row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(8, timer="Dis 08:15-08:45", action="Idle", locked=False)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    cur = merged["rows"][0]
    # Unparseable legacy text (no kW/cap) is left unchanged by clip.
    assert cur["timer_schedule"] == "Dis 08:00-08:45"
    assert cur["action"] == "Discharging to Grid"


def test_merge_updates_future_hour_timer_from_fresh():
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [
            _row(8, timer="Dis 08:00-08:45", locked=True),
            _row(9, timer="", action="Idle"),
        ],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [
            _row(8, timer="Dis 08:15-08:45"),
            _row(9, timer="Dis 09:00-09:45", action="Discharging to Grid"),
        ],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    future = next(r for r in merged["rows"] if r["hour"] == 9)
    assert future["timer_schedule"] == "Dis 09:00-09:45"
    assert future["action"] == "Discharging to Grid"


def test_merge_preserves_history_rows():
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    hist = _row(7, timer="Dis 07:00-07:45", locked=True)
    hist["history_hour"] = True
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [hist],
        "rows": [_row(8, locked=True)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [_row(7, timer="CHANGED")],
        "rows": [_row(8), _row(9, timer="NEW")],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    assert merged["history_rows"][0]["timer_schedule"] == "Dis 07:00-07:45"


# ---------------------------------------------------------------------------
# _apply_actual_quarter_if_needed
# ---------------------------------------------------------------------------

def _make_series_10min(
    *,
    pv_kwh_per_q: float = 0.5,
    load_kwh_per_q: float = 0.2,
    bat_kwh_per_q: float = 0.0,
    grid_export_kwh_per_q: float = 0.0,
    grid_import_kwh_per_q: float = 0.0,
    hour: int = 8,
) -> dict:
    """Minimal series_10min for the given hour (3 × 10-min slots = 1 q15)."""
    size = 24 * 6  # 144 slots total, 6 per hour
    pv = [0.0] * size
    load = [0.0] * size
    bat_charge = [0.0] * size
    bat_discharge = [0.0] * size
    grid_sell = [0.0] * size
    grid_buy = [0.0] * size
    base = hour * 6
    # spread q0 over first 3 10-min slots
    for i in range(3):
        pv[base + i] = pv_kwh_per_q / 3
        load[base + i] = load_kwh_per_q / 3
        bat_charge[base + i] = max(0.0, bat_kwh_per_q) / 3
        bat_discharge[base + i] = max(0.0, -bat_kwh_per_q) / 3
        grid_sell[base + i] = grid_export_kwh_per_q / 3
        grid_buy[base + i] = -grid_import_kwh_per_q / 3
    return {
        "pv": pv, "load": load,
        "bat_charge": bat_charge, "bat_discharge": bat_discharge,
        "grid_sell": grid_sell, "grid_buy": grid_buy,
    }


def test_apply_actual_quarter_writes_from_actual_true():
    """_apply_actual_quarter_if_needed writes q15[0] with from_actual=True."""
    cfg = _cfg()
    row = _row(8)
    series = _make_series_10min(pv_kwh_per_q=0.5, load_kwh_per_q=0.2, hour=8)
    changed = _apply_actual_quarter_if_needed(
        row, 8, 0,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
    )
    assert changed is True
    q15 = row["q15"]
    assert q15[0]["from_actual"] is True
    assert q15[0]["quarter"] == 0
    assert q15[1]["from_actual"] is False


def test_apply_actual_quarter_idempotent():
    """Second call for same quarter does nothing (slot already from_actual)."""
    cfg = _cfg()
    row = _row(8)
    series = _make_series_10min(hour=8)
    _apply_actual_quarter_if_needed(row, 8, 0, series_10min=series,
                                    today_hourly=None, cfg=cfg, battery_cap=20.0)
    first_val = row["q15"][0]["production"]
    # Modify series — second call must NOT change anything
    series["pv"] = [999.0] * len(series["pv"])
    changed = _apply_actual_quarter_if_needed(row, 8, 0, series_10min=series,
                                              today_hourly=None, cfg=cfg, battery_cap=20.0)
    assert changed is False
    assert row["q15"][0]["production"] == first_val


def test_apply_actual_quarter_sequential():
    """At :30 q15[0] already actual; q15[1] is written; q15[2,3] untouched."""
    cfg = _cfg()
    row = _row(8)
    series = _make_series_10min(pv_kwh_per_q=0.4, load_kwh_per_q=0.1, hour=8)

    # Simulate :15 tick — write q0
    _apply_actual_quarter_if_needed(row, 8, 0, series_10min=series,
                                    today_hourly=None, cfg=cfg, battery_cap=20.0)
    assert row["q15"][0]["from_actual"] is True

    # Simulate :30 tick — write q1
    _apply_actual_quarter_if_needed(row, 8, 1, series_10min=series,
                                    today_hourly=None, cfg=cfg, battery_cap=20.0)
    assert row["q15"][1]["from_actual"] is True
    assert row["q15"][2]["from_actual"] is False
    assert row["q15"][3]["from_actual"] is False


# ---------------------------------------------------------------------------
# _merge_current_hour_q15
# ---------------------------------------------------------------------------

def _fresh_q15_row(hour: int, *, soc: float = 55.0) -> dict:
    q15 = [
        {"quarter": q, "production": 0.3, "consumption": 0.1, "soc": soc,
         "battery": -0.1, "grid_import": 0.0, "grid_export": 0.1, "from_actual": False}
        for q in range(4)
    ]
    row = _row(hour)
    row["q15"] = q15
    return row


def test_merge_q15_keeps_actual_and_takes_fresh_for_future():
    """At :30: q0 already actual → stays; q1 gets actual; q2,q3 from fresh sim."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))

    existing = _row(8)
    # q0 was written at :15
    existing["q15"][0] = {
        "quarter": 0, "production": 0.5, "consumption": 0.2, "soc": 50.0,
        "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0, "from_actual": True,
    }
    series = _make_series_10min(pv_kwh_per_q=0.6, load_kwh_per_q=0.25, hour=8)
    fresh_row = _fresh_q15_row(8, soc=52.0)

    _merge_current_hour_q15(
        existing,
        now=now, hour=8,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
    )

    q15 = existing["q15"]
    # q0 — unchanged actual
    assert q15[0]["from_actual"] is True
    assert q15[0]["production"] == 0.5
    # q1 — written from Influx at :30
    assert q15[1]["from_actual"] is True
    # q2, q3 — untouched from SQLite (:00 values), NOT from fresh optimizer
    assert q15[2]["from_actual"] is False
    assert q15[3]["from_actual"] is False


def test_merge_q15_at_hour_start_no_actuals():
    """:05 (before first tick) — no Influx data yet, all from SQLite (:00 values)."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 5, tzinfo=ZoneInfo("Europe/Warsaw"))

    existing = _row(8)
    series = _make_series_10min(hour=8)

    _merge_current_hour_q15(
        existing,
        now=now, hour=8,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
    )
    q15 = existing["q15"]
    assert all(not s["from_actual"] for s in q15)


# ---------------------------------------------------------------------------
# End-to-end merge_incremental_plan with actuals
# ---------------------------------------------------------------------------

def test_merge_incremental_at_30_quarter_pattern():
    """At :30: existing has q0 actual; merge writes q1 actual; q2,q3 from fresh."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))

    cur_row = _row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)
    cur_row["q15"][0] = {
        "quarter": 0, "production": 0.5, "consumption": 0.2, "soc": 50.0,
        "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0, "from_actual": True,
    }
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [cur_row, _row(9)],
    }
    fresh_cur = _row(8, timer="OPTIMIZER_NEW", action="Idle")
    fresh_cur["q15"] = [
        {"quarter": q, "production": 0.3, "consumption": 0.1, "soc": 49.0,
         "battery": -0.05, "grid_import": 0.0, "grid_export": 0.0, "from_actual": False}
        for q in range(4)
    ]
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur, _row(9, timer="FUTURE_9")],
    }
    series = _make_series_10min(pv_kwh_per_q=0.45, load_kwh_per_q=0.18, hour=8)
    metrics = {"series_10min": series}

    merged = merge_incremental_plan(existing, fresh, now=now, metrics=metrics, cfg=cfg)
    cur = next(r for r in merged["rows"] if r["hour"] == 8)

    # timer/action locked — not overwritten by optimizer
    assert cur["timer_schedule"] == "Dis 08:00-08:45"
    assert cur["action"] == "Discharging to Grid"
    # q0 — still the original actual
    assert cur["q15"][0]["from_actual"] is True
    assert cur["q15"][0]["production"] == 0.5
    # q1 — written from Influx at :30
    assert cur["q15"][1]["from_actual"] is True
    # q2, q3 — from SQLite (:00 values), NOT touched by fresh optimizer
    assert cur["q15"][2]["from_actual"] is False
    assert cur["q15"][3]["from_actual"] is False
    # future hour gets optimizer value
    future = next(r for r in merged["rows"] if r["hour"] == 9)
    assert future["timer_schedule"] == "FUTURE_9"

