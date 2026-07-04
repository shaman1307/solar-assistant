"""Plan in-memory cache: stale detection, hit, invalidate, force refresh."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src import plan_simulation as ps


@pytest.fixture(autouse=True)
def _clear_plan_cache():
    ps.invalidate_plan_cache()
    yield
    ps.invalidate_plan_cache()


def _warsaw_now(*, hour: int = 14, minute: int = 0) -> datetime:
    return datetime(2026, 7, 3, hour, minute, 0)


def _fresh_cache_entry(now: datetime) -> dict:
    return {
        "today_date": now.strftime("%Y-%m-%d"),
        "plan_from_hour": now.hour,
        "computed_at": "2026-07-03 12:00:00",
        "rows": [{"hour": now.hour + 1}],
        "history_rows": [],
        "delta_kwh": 0.0,
        "forecast": {"meta": {}},
        "rce": {"current_price_pln_kwh": 0.42},
        "buy_tariff": {"rows": []},
    }


def test_plan_cache_stale_when_hour_differs(monkeypatch):
    now = _warsaw_now(hour=14)
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    cached = _fresh_cache_entry(now)
    cached["plan_from_hour"] = 13
    assert ps._plan_cache_stale(cached) is True


def test_plan_cache_stale_when_date_differs(monkeypatch):
    now = _warsaw_now(hour=14)
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    cached = _fresh_cache_entry(now)
    cached["today_date"] = "2026-07-02"
    assert ps._plan_cache_stale(cached) is True


def test_plan_cache_fresh_when_window_matches(monkeypatch):
    now = _warsaw_now(hour=14, minute=45)
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    cached = _fresh_cache_entry(now.replace(minute=0, second=0, microsecond=0))
    assert ps._plan_cache_stale(cached) is False


def test_invalidate_clears_cache():
    ps._cache = _fresh_cache_entry(_warsaw_now())
    ps.invalidate_plan_cache()
    assert ps.get_cached_plan() is None
    assert ps.get_cached_forecast() is None
    assert ps.get_cached_rce() is None
    assert ps.get_cached_buy_tariff() is None


def test_get_cached_plan_exposes_nested_payload():
    entry = _fresh_cache_entry(_warsaw_now())
    ps._cache = entry
    assert ps.get_cached_plan() is entry
    assert ps.get_cached_forecast() == entry["forecast"]
    assert ps.get_cached_rce() == entry["rce"]
    assert ps.get_cached_buy_tariff() == entry["buy_tariff"]


async def _immediate_to_thread(fn, /, *args, **kwargs):
    return fn(*args, **kwargs)


def _patch_build_deps(monkeypatch, *, now: datetime, fetch: AsyncMock, sim_result: dict):
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    monkeypatch.setattr(ps, "fetch_plan_inputs", fetch)
    monkeypatch.setattr(asyncio, "to_thread", _immediate_to_thread)
    monkeypatch.setattr(ps, "run_simulation", lambda *a, **k: sim_result)
    monkeypatch.setattr(ps, "build_hourly_schedule", lambda *a, **k: {})
    monkeypatch.setattr(ps, "build_buy_tariff_payload", lambda *a, **k: {"rows": []})
    monkeypatch.setattr(ps, "extract_plan_soc_q15", lambda sim: {"today": [None] * 96, "tomorrow": [None] * 96})


def test_build_plan_cache_hit_skips_fetch_and_sim(monkeypatch):
    now = _warsaw_now(hour=14)
    ps._cache = _fresh_cache_entry(now)
    fetch = AsyncMock()
    _patch_build_deps(
        monkeypatch,
        now=now,
        fetch=fetch,
        sim_result={"rows": [], "today_date": now.strftime("%Y-%m-%d"), "plan_from_hour": now.hour},
    )

    result = asyncio.run(ps.build_plan_simulation({}))

    fetch.assert_not_called()
    assert result["computed_at"] == "2026-07-03 12:00:00"
    assert result["rows"] == [{"hour": 15}]


def test_build_plan_stale_cache_triggers_rebuild(monkeypatch):
    now = _warsaw_now(hour=14)
    stale = _fresh_cache_entry(now)
    stale["plan_from_hour"] = 10
    ps._cache = stale

    sim = {
        "rows": [{"hour": 15, "rebuilt": True}],
        "history_rows": [],
        "delta_kwh": 1.0,
        "today_date": now.strftime("%Y-%m-%d"),
        "plan_from_hour": now.hour,
    }
    fetch = AsyncMock(
        return_value=({"meta": {}}, {}, {}, {"current_price_pln_kwh": 0.5}),
    )
    _patch_build_deps(monkeypatch, now=now, fetch=fetch, sim_result=sim)

    result = asyncio.run(ps.build_plan_simulation({}))

    fetch.assert_awaited_once()
    assert result["rows"][0].get("rebuilt") is True
    assert ps.get_cached_plan()["rows"][0].get("rebuilt") is True


def test_build_plan_force_refresh_bypasses_fresh_cache(monkeypatch):
    now = _warsaw_now(hour=14)
    ps._cache = _fresh_cache_entry(now)

    sim = {
        "rows": [{"hour": 15, "forced": True}],
        "history_rows": [],
        "delta_kwh": 0.0,
        "today_date": now.strftime("%Y-%m-%d"),
        "plan_from_hour": now.hour,
    }
    fetch = AsyncMock(
        return_value=({"meta": {}}, {}, {}, {"current_price_pln_kwh": 0.5}),
    )
    _patch_build_deps(monkeypatch, now=now, fetch=fetch, sim_result=sim)

    result = asyncio.run(
        ps.build_plan_simulation({}, force_refresh=True, invalidate_inputs=False),
    )

    fetch.assert_awaited_once()
    assert result["rows"][0].get("forced") is True


def test_invalidate_all_caches_clears_plan(monkeypatch):
    from src.cache_registry import invalidate_all_caches

    ps._cache = _fresh_cache_entry(_warsaw_now())
    invalidate_all_caches()
    assert ps.get_cached_plan() is None
