"""Cached deposit total and daily month_history refresh."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from src.plan_monthly_refresh import (
    deposit_total_needs_refresh,
    ensure_deposit_total_current,
    maybe_run_daily_month_history,
)
from src.sqlite_store import (
    read_cached_deposit_total,
    read_month_history_daily_date,
    reset_connection_for_tests,
    write_cached_deposit_total,
    write_month_history_daily_date,
)


def test_deposit_total_needs_refresh_on_month_change(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    write_cached_deposit_total(100.0, "2026-06")
    assert deposit_total_needs_refresh(date(2026, 6, 15)) is False
    assert deposit_total_needs_refresh(date(2026, 7, 1)) is True


def test_deposit_total_needs_refresh_when_cache_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    assert deposit_total_needs_refresh(date(2026, 7, 1)) is True


def test_maybe_run_daily_month_history_once_per_day(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    async def _refresh(*_args, **_kwargs):
        write_month_history_daily_date(today.isoformat())
        return {"ok": True, "deposit_total": 50.0}

    refresh = AsyncMock(side_effect=_refresh)
    monkeypatch.setattr("src.plan_monthly_refresh.refresh_open_month_history", refresh)

    cfg = {}
    today = date(2026, 7, 20)

    async def _run() -> None:
        await maybe_run_daily_month_history(cfg, today=today)
        await maybe_run_daily_month_history(cfg, today=today)

    asyncio.run(_run())

    refresh.assert_awaited_once()
    assert read_month_history_daily_date() == "2026-07-20"


def test_ensure_deposit_total_returns_cached_without_refresh(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    write_cached_deposit_total(123.45, "2026-07")
    write_month_history_daily_date("2026-07-20")

    refresh = AsyncMock()
    monkeypatch.setattr("src.plan_monthly_refresh.refresh_open_month_history", refresh)

    total = asyncio.run(
        ensure_deposit_total_current({}, today=date(2026, 7, 20)),
    )
    assert total == 123.45
    refresh.assert_not_awaited()
    cached = read_cached_deposit_total()
    assert cached is not None
    assert cached["deposit_total"] == 123.45
