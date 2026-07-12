"""run_hour_boundary_start reads timer_schedule from SQLite and writes to SA."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import hour_boundary_scheduler as hbs
from src.sqlite_store import write_plan


def _plan_with_timer(hour: int, timer: str, today: str = "2026-07-07") -> dict:
    return {
        "today_date": today,
        "plan_from_hour": hour,
        "history_rows": [],
        "rows": [
            {
                "hour": hour,
                "plan_date": today,
                "start": f"07-07-2026 {hour + 1:02d}:00",
                "timer_schedule": timer,
                "action": "Discharging to Grid and Load",
                "hour_labels_locked": True,
                "production": 1.0,
                "consumption": 0.5,
                "battery": -2.0,
                "bat_charge": 0.0,
                "bat_discharge": 2.0,
                "grid_import": 0.0,
                "grid_export": 1.5,
                "soc": 50.0,
                "buy_price": 1.0,
                "g12_zone": "peak",
                "q15": [],
            }
        ],
    }


def _cfg():
    return {
        "smart_mode_enabled": True,
        "battery": {"capacity_kwh": 20.0},
        "simulation": {"min_soc_pct": 16},
        "inverter": {"ac_capacity_kw": 8.0},
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


@pytest.fixture(autouse=True)
def _clear_plan(tmp_path):
    from src import sqlite_store
    orig = sqlite_store._DB_PATH
    sqlite_store._DB_PATH = tmp_path / "test.db"
    sqlite_store._conn = None
    yield
    sqlite_store._DB_PATH = orig
    sqlite_store._conn = None


def test_plan_rows_reads_from_sqlite():
    """_plan_rows returns rows from SQLite without calling build_plan_simulation."""
    plan = _plan_with_timer(8, "Dis 08:00-08:45 8.0kW cap16%")
    write_plan(plan)

    rows = asyncio.run(hbs._plan_rows(_cfg()))

    assert len(rows) == 1
    assert rows[0]["timer_schedule"] == "Dis 08:00-08:45 8.0kW cap16%"


def test_plan_rows_returns_empty_when_sqlite_has_no_plan():
    """When SQLite is empty, _plan_rows returns [] — no fallback to simulation."""
    rows = asyncio.run(hbs._plan_rows(_cfg()))
    assert rows == []


def test_sync_timer_reads_timer_from_sqlite_rows():
    """_sync_timer_from_hour_row uses timer_schedule from rows (read from SQLite upstream)."""
    rows = _plan_with_timer(8, "Dis 08:00-08:45 8.0kW cap16%")["rows"]
    cfg = _cfg()

    sa_rules = {
        "timed_discharge_enabled": False,
        "timed_charge_enabled": False,
        "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00",
                              "capacity_pct": 16, "power_kw": 8.0}],
        "charge_slots": [{"slot": 1, "from": "00:00", "to": "00:00", "power_kw": 0}],
    }
    apply_mock = AsyncMock(return_value=True)
    get_rules_mock = AsyncMock(return_value=sa_rules)

    with (
        patch.object(hbs.sa_client, "get_rules", get_rules_mock),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", apply_mock),
    ):
        status = asyncio.run(hbs._sync_timer_from_hour_row(cfg, rows, 8))

    assert status["ok"] is True
    assert status["skipped"] is False
    assert status["timer_schedule"] == "Dis 08:00-08:45 8.0kW cap16%"
    apply_mock.assert_awaited_once()
    schedule_sent = apply_mock.call_args[0][1]
    assert schedule_sent["timed_discharge_enabled"] is True
    dis = schedule_sent["discharge_slots"][0]
    assert dis["from"] == "08:00"
    assert dis["to"] == "08:45"


def test_sync_timer_skips_when_empty_timer_schedule():
    rows = _plan_with_timer(8, "")["rows"]

    status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), rows, 8))

    assert status["skipped"] is True
    assert status["skip_reason"] == "empty_timer_schedule"
    assert status["ok"] is True


def test_sync_timer_skips_when_no_row_for_hour():
    rows = _plan_with_timer(9, "Dis 09:00-09:45 8.0kW cap16%")["rows"]

    status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), rows, 8))

    assert status["skipped"] is True
    assert status["ok"] is True
