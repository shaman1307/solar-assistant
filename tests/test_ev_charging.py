"""EV charging session normalization and persistence."""

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


def test_get_session_for_ui_returns_defaults_when_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr("src.ev_charging.now_warsaw", lambda: __import__("datetime").datetime(2026, 6, 30, 12, 0))
    defaults = default_session()
    ui = get_session_for_ui("2026-06-30", _cfg())
    assert ui["day"]["start"] == defaults["day"]["start"]
