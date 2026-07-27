"""EV charging session normalization and persistence."""

import pytest

from src.ev_charging import (
    default_session,
    get_session_for_ui,
    normalize_session,
    set_session,
)


def _cfg() -> dict:
    return {"ev": {"max_power_kw": 11.0}}


def test_normalize_extends_end_when_before_start():
    session = normalize_session({
        "day": {"enabled": True, "start": "17:00", "end": "16:00", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, _cfg())
    assert session["day"]["start"] == "17:00"
    assert session["day"]["end"] == "17:15"
    assert session["day"]["enabled"] is True


def test_normalize_keeps_valid_range():
    session = normalize_session({
        "day": {"enabled": True, "start": "17:00", "end": "23:45", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, _cfg())
    assert session["day"]["end"] == "23:45"
    assert session["day"]["enabled"] is True


def test_set_session_persists_disabled_slot_times(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr("src.ev_charging.now_warsaw", lambda: __import__("datetime").datetime(2026, 6, 30, 12, 0))
    cfg = _cfg()
    date_str = "2026-06-30"
    set_session(date_str, {
        "day": {"enabled": False, "start": "17:00", "end": "23:45", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, cfg)
    ui = get_session_for_ui(date_str, cfg)
    assert ui["day"]["start"] == "17:00"
    assert ui["day"]["end"] == "23:45"
    assert ui["day"]["enabled"] is False


def test_set_session_persists_enabled_day_slot(tmp_path, monkeypatch):
    """Enabled Day must stick — regression for UI checkbox not saving."""
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr(
        "src.ev_charging.now_warsaw",
        lambda: __import__("datetime").datetime(2026, 7, 27, 15, 0),
    )
    cfg = _cfg()
    date_str = "2026-07-27"
    set_session(date_str, {
        "day": {"enabled": True, "start": "14:15", "end": "16:00", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, cfg)
    ui = get_session_for_ui(date_str, cfg)
    assert ui["day"]["enabled"] is True
    assert ui["day"]["start"] == "14:15"
    assert ui["day"]["end"] == "16:00"
    assert ui["day"]["power_kw"] == 5.0


def test_concurrent_set_session_last_write_wins(tmp_path, monkeypatch):
    """Serialized RMW: parallel enables must not leave a corrupt/empty store."""
    import threading

    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr(
        "src.ev_charging.now_warsaw",
        lambda: __import__("datetime").datetime(2026, 7, 27, 15, 0),
    )
    cfg = _cfg()
    date_str = "2026-07-27"
    errors: list[BaseException] = []

    def _write(enabled: bool, start: str) -> None:
        try:
            set_session(date_str, {
                "day": {"enabled": enabled, "start": start, "end": "16:00", "power_kw": 5},
                "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
            }, cfg)
        except BaseException as exc:  # noqa: BLE001 — collect worker errors
            errors.append(exc)

    threads = [
        threading.Thread(target=_write, args=(True, "14:15")),
        threading.Thread(target=_write, args=(True, "14:30")),
        threading.Thread(target=_write, args=(False, "14:00")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    ui = get_session_for_ui(date_str, cfg)
    assert ui["day"]["start"] in ("14:15", "14:30", "14:00")
    assert ui["day"]["end"] == "16:00"


def test_get_session_for_ui_returns_defaults_when_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr("src.ev_charging.now_warsaw", lambda: __import__("datetime").datetime(2026, 6, 30, 12, 0))
    defaults = default_session()
    ui = get_session_for_ui("2026-06-30", _cfg())
    assert ui["day"]["start"] == defaults["day"]["start"]


def test_api_save_returns_enabled_session_without_plan_refresh(tmp_path, monkeypatch):
    """POST saves EV + forecast cache only; client triggers plan rebuild separately."""
    import asyncio

    from src.routes import ev as ev_routes

    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr(
        "src.ev_charging.now_warsaw",
        lambda: __import__("datetime").datetime(2026, 7, 27, 15, 0),
    )
    monkeypatch.setattr(ev_routes, "load_config", _cfg)

    async def _cache(_cfg):
        return {}

    monkeypatch.setattr(ev_routes.forecast_mod, "apply_overrides_to_cache", _cache)

    result = asyncio.run(
        ev_routes.api_save_ev_charging({
            "date": "2026-07-27",
            "day": {"enabled": True, "start": "14:15", "end": "16:00", "power_kw": 5},
            "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
        })
    )
    assert result["status"] == "saved"
    assert result["session"]["day"]["enabled"] is True
    assert result["session"]["day"]["start"] == "14:15"
