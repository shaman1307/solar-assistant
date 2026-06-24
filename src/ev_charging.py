"""EV charging sessions — per-date plans, history for load de-trending, q15 profiles."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .influxdb import now_warsaw

log = logging.getLogger(__name__)

Q15_PER_DAY = 96
HOURS_PER_DAY = 24
HISTORY_RETENTION_DAYS = 35

_STORE_PATH = BASE_DIR / "data" / "ev_charging.json"

DEFAULT_SLOT = {
    "enabled": False,
    "start": "11:00",
    "end": "16:00",
    "power_kw": 5.0,
}

DEFAULT_NIGHT_SLOT = {
    "enabled": False,
    "start": "02:00",
    "end": "07:00",
    "power_kw": 8.0,
}


def store_path() -> Path:
    return _STORE_PATH


def default_session() -> dict[str, Any]:
    return {
        "day": {**DEFAULT_SLOT},
        "night": {**DEFAULT_NIGHT_SLOT},
    }


def load_store() -> dict[str, Any]:
    if not _STORE_PATH.is_file():
        return {"sessions": {}}
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        data.setdefault("sessions", {})
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("ev_charging.json read failed: %s", exc)
        return {"sessions": {}}


def save_store(data: dict[str, Any]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def max_power_kw(cfg: dict) -> float:
    return max(0.0, float((cfg.get("ev") or {}).get("max_power_kw") or 11.0))


def clamp_power_kw(value: float, cfg: dict) -> float:
    cap = max_power_kw(cfg)
    if cap <= 0:
        return 0.0
    return round(max(0.0, min(float(value), cap)), 2)


def parse_hhmm(value: str) -> int | None:
    try:
        parts = str(value).strip().split(":")
        if len(parts) < 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
        return h * 60 + m
    except (ValueError, TypeError):
        return None


def _normalize_slot(raw: dict | None, *, night: bool, cfg: dict) -> dict[str, Any]:
    defaults = DEFAULT_NIGHT_SLOT if night else DEFAULT_SLOT
    src = raw if isinstance(raw, dict) else {}
    start = str(src.get("start") or defaults["start"])
    end = str(src.get("end") or defaults["end"])
    if parse_hhmm(start) is None:
        start = defaults["start"]
    if parse_hhmm(end) is None:
        end = defaults["end"]
    power = clamp_power_kw(float(src.get("power_kw", defaults["power_kw"]) or 0), cfg)
    enabled = bool(src.get("enabled", False)) and power > 0
    if parse_hhmm(start) is not None and parse_hhmm(end) is not None:
        if parse_hhmm(start) >= parse_hhmm(end):
            enabled = False
    return {
        "enabled": enabled,
        "start": start,
        "end": end,
        "power_kw": power if enabled else clamp_power_kw(float(src.get("power_kw", defaults["power_kw"]) or 0), cfg),
    }


def normalize_session(raw: dict | None, cfg: dict) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    day = _normalize_slot(src.get("day"), night=False, cfg=cfg)
    night = _normalize_slot(src.get("night"), night=True, cfg=cfg)
    return {"day": day, "night": night}


def session_has_charging(session: dict[str, Any] | None) -> bool:
    if not session:
        return False
    for key in ("day", "night"):
        slot = session.get(key) or {}
        if slot.get("enabled") and float(slot.get("power_kw") or 0) > 0:
            return True
    return False


def get_session(date_str: str) -> dict[str, Any] | None:
    return load_store().get("sessions", {}).get(date_str)


def set_session(date_str: str, session: dict[str, Any], cfg: dict) -> dict[str, Any]:
    normalized = normalize_session(session, cfg)
    data = load_store()
    sessions: dict[str, Any] = data.setdefault("sessions", {})
    if session_has_charging(normalized):
        sessions[date_str] = normalized
    else:
        sessions.pop(date_str, None)
    save_store(data)
    prune_old_sessions()
    return normalized


def get_session_for_ui(date_str: str, cfg: dict) -> dict[str, Any]:
    """UI state: stored plan for date, or disabled defaults."""
    stored = get_session(date_str)
    if stored and session_has_charging(stored):
        return normalize_session(stored, cfg)
    return default_session()


def is_plannable_date(date_str: str, today_str: str, tomorrow_str: str) -> bool:
    return date_str in (today_str, tomorrow_str)


def is_history_date(date_str: str, today_str: str) -> bool:
    return date_str < today_str


def session_for_load_add(
    date_str: str,
    today_str: str,
    tomorrow_str: str,
    cfg: dict,
) -> dict[str, Any] | None:
    if not is_plannable_date(date_str, today_str, tomorrow_str):
        return None
    session = get_session(date_str)
    if not session_has_charging(session):
        return None
    return normalize_session(session, cfg)


def session_for_history_subtract(date_str: str, today_str: str, cfg: dict) -> dict[str, Any] | None:
    if not is_history_date(date_str, today_str):
        return None
    session = get_session(date_str)
    if not session_has_charging(session):
        return None
    return normalize_session(session, cfg)


def _slot_q15_energy(
    start_min: int,
    end_min: int,
    power_kw: float,
) -> list[float]:
    out = [0.0] * Q15_PER_DAY
    if power_kw <= 0 or end_min <= start_min:
        return out
    for j in range(Q15_PER_DAY):
        slot_start = j * 15
        slot_end = slot_start + 15
        overlap = max(0, min(end_min, slot_end) - max(start_min, slot_start))
        if overlap > 0:
            out[j] = round(power_kw * overlap / 60.0, 6)
    return out


def build_ev_q15(session: dict[str, Any] | None) -> list[float]:
    if not session:
        return [0.0] * Q15_PER_DAY
    total = [0.0] * Q15_PER_DAY
    for key, default in (("day", DEFAULT_SLOT), ("night", DEFAULT_NIGHT_SLOT)):
        slot = session.get(key) or {}
        if not slot.get("enabled"):
            continue
        power = float(slot.get("power_kw") or 0)
        if power <= 0:
            continue
        start = parse_hhmm(str(slot.get("start") or default["start"]))
        end = parse_hhmm(str(slot.get("end") or default["end"]))
        if start is None or end is None or end <= start:
            continue
        slot_q15 = _slot_q15_energy(start, end, power)
        total = [round(a + b, 6) for a, b in zip(total, slot_q15)]
    return total


def q15_to_hourly(q15: list[float]) -> list[float]:
    hourly: list[float] = []
    for h in range(HOURS_PER_DAY):
        chunk = q15[h * 4:(h + 1) * 4]
        hourly.append(round(sum(chunk), 3))
    return hourly


def build_ev_hourly(session: dict[str, Any] | None) -> list[float]:
    return q15_to_hourly(build_ev_q15(session))


def ev_total_kwh(session: dict[str, Any] | None) -> float:
    return round(sum(build_ev_hourly(session)), 2)


def prune_old_sessions() -> None:
    today = now_warsaw().date()
    cutoff = (today - timedelta(days=HISTORY_RETENTION_DAYS)).strftime("%Y-%m-%d")
    data = load_store()
    sessions = data.get("sessions") or {}
    kept = {d: s for d, s in sessions.items() if d >= cutoff}
    if len(kept) != len(sessions):
        data["sessions"] = kept
        save_store(data)


def nightly_reset_tomorrow(cfg: dict, *, now: datetime | None = None) -> str:
    """At 23:59: reset tomorrow's EV plan to defaults; keep today and past sessions."""
    now = now or now_warsaw()
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    data = load_store()
    sessions = data.setdefault("sessions", {})
    sessions[tomorrow_str] = default_session()
    save_store(data)
    prune_old_sessions()
    log.info("EV charging plan reset to defaults for %s", tomorrow_str)
    return tomorrow_str


def api_payload(cfg: dict) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "max_power_kw": max_power_kw(cfg),
        "defaults": default_session(),
        "today": {
            "date": today_str,
            "session": get_session_for_ui(today_str, cfg),
        },
        "tomorrow": {
            "date": tomorrow_str,
            "session": get_session_for_ui(tomorrow_str, cfg),
        },
    }


def apply_session_update(
    *,
    date_str: str,
    day: dict | None,
    night: dict | None,
    cfg: dict,
) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str not in (today_str, tomorrow_str):
        raise ValueError("EV charging can only be set for today or tomorrow")
    current = get_session_for_ui(date_str, cfg)
    merged = deepcopy(current)
    if day is not None:
        merged["day"] = {**merged["day"], **day}
    if night is not None:
        merged["night"] = {**merged["night"], **night}
    return set_session(date_str, merged, cfg)
