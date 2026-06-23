"""Daily Load/PV forecast cache (weekday Load + Open-Meteo PV)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import BASE_DIR, load_config, save_config
from .influxdb import get_load_kwh_10min_for_date_sync, now_warsaw

log = logging.getLogger(__name__)

Q15_PER_DAY = 96
HOURS_PER_DAY = 24
WEEKDAY_SAMPLES = 4
TEN_MIN_PER_DAY = 144

_CACHE_PATH = BASE_DIR / "data" / "forecast_day_cache.json"


def cache_path() -> Path:
    return _CACHE_PATH


def _empty_day_metric() -> dict[str, Any]:
    return {
        "base_q15": [0.0] * Q15_PER_DAY,
        "base_hourly": [0.0] * HOURS_PER_DAY,
        "effective_q15": [0.0] * Q15_PER_DAY,
        "effective_hourly": [0.0] * HOURS_PER_DAY,
        "total_base": 0.0,
        "total_effective": 0.0,
    }


def _empty_day() -> dict[str, Any]:
    return {"load": _empty_day_metric(), "pv": _empty_day_metric()}


def load_day_cache() -> dict[str, Any]:
    if not _CACHE_PATH.is_file():
        return {"computed_at": None, "days": {}}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("forecast_day_cache read failed: %s", exc)
        return {"computed_at": None, "days": {}}


def save_day_cache(data: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def invalidate_day_cache_file() -> None:
    if _CACHE_PATH.is_file():
        _CACHE_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 10-min → 15-min overlap redistribution (energy-preserving)
# ---------------------------------------------------------------------------

def aggregate_10min_to_q15(energy_10min: list[float]) -> list[float]:
    """Redistribute 10-min kWh buckets into 15-min windows by overlap weight."""
    out: list[float] = []
    for j in range(Q15_PER_DAY):
        j_start = j * 15
        j_end = j_start + 15
        e_j = 0.0
        for i in range(TEN_MIN_PER_DAY):
            if i >= len(energy_10min):
                break
            i_start = i * 10
            i_end = i_start + 10
            overlap = max(0, min(i_end, j_end) - max(i_start, j_start))
            if overlap > 0:
                e_j += float(energy_10min[i]) * overlap / 10.0
        out.append(round(e_j, 6))
    return out


def q15_to_hourly(q15: list[float]) -> list[float]:
    hourly: list[float] = []
    for h in range(HOURS_PER_DAY):
        chunk = q15[h * 4:(h + 1) * 4]
        hourly.append(round(sum(chunk), 6))
    return hourly


def hourly_to_q15_equal(hourly: list[float]) -> list[float]:
    q15: list[float] = []
    for h in range(HOURS_PER_DAY):
        v = float(hourly[h] if h < len(hourly) else 0.0) / 4.0
        q15.extend([round(v, 6)] * 4)
    return q15


def cfg_load_hourly(cfg: dict) -> list[float]:
    fallback = cfg.get("load", {}).get("hourly_profile_kwh") or []
    out = [float(v or 0.0) for v in fallback[:HOURS_PER_DAY]]
    while len(out) < HOURS_PER_DAY:
        out.append(out[-1] if out else 0.0)
    return out


def cfg_hourly_to_10min(hourly: list[float]) -> list[float]:
    ten: list[float] = []
    for h in range(HOURS_PER_DAY):
        per = float(hourly[h] if h < len(hourly) else 0.0) / 6.0
        ten.extend([per] * 6)
    return ten


def _weekday_sample_dates(target: datetime.date, count: int = WEEKDAY_SAMPLES) -> list[str]:
    return [
        (target - timedelta(days=7 * k)).strftime("%Y-%m-%d")
        for k in range(1, count + 1)
    ]


def _day_has_load_data(ten_min: list[float] | None) -> bool:
    if not ten_min:
        return False
    return sum(float(v or 0.0) for v in ten_min) > 0.01


def compute_weekday_load_profile(target_date_str: str, cfg: dict) -> tuple[list[float], list[float]]:
    """Average q15/hourly load from four prior same weekdays (10-min Influx → q15)."""
    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    cfg_hourly = cfg_load_hourly(cfg)
    cfg_ten = cfg_hourly_to_10min(cfg_hourly)

    q15_samples: list[list[float]] = []
    for date_str in _weekday_sample_dates(target):
        ten_min = get_load_kwh_10min_for_date_sync(date_str)
        if not _day_has_load_data(ten_min):
            ten_min = list(cfg_ten)
        else:
            ten_min = [float(v or 0.0) for v in ten_min]
        q15_samples.append(aggregate_10min_to_q15(ten_min))

    avg_q15: list[float] = []
    for slot in range(Q15_PER_DAY):
        vals = [s[slot] for s in q15_samples]
        avg_q15.append(round(sum(vals) / len(vals), 6))
    return avg_q15, q15_to_hourly(avg_q15)


def _set_metric_effective(metric: dict[str, Any], hourly: list[float], q15: list[float]) -> None:
    metric["base_q15"] = [round(v, 6) for v in q15]
    metric["base_hourly"] = [round(v, 3) for v in hourly]
    metric["effective_q15"] = list(metric["base_q15"])
    metric["effective_hourly"] = list(metric["base_hourly"])
    total = round(sum(hourly), 2)
    metric["total_base"] = total
    metric["total_effective"] = total


def _scale_metric(metric: dict[str, Any], target_total: float | None) -> None:
    base_total = float(metric.get("total_base") or 0.0)
    if target_total is None or base_total <= 0.0:
        metric["effective_q15"] = list(metric.get("base_q15") or [])
        metric["effective_hourly"] = list(metric.get("base_hourly") or [])
        metric["total_effective"] = base_total
        return
    scale = float(target_total) / base_total
    metric["effective_q15"] = [round(v * scale, 6) for v in metric["base_q15"]]
    metric["effective_hourly"] = [round(v * scale, 3) for v in metric["base_hourly"]]
    metric["total_effective"] = round(target_total, 2)


def build_day_forecast(date_str: str, cfg: dict, pv_hourly: list[float]) -> dict[str, Any]:
    load_q15, load_hourly = compute_weekday_load_profile(date_str, cfg)
    day = _empty_day()
    _set_metric_effective(day["load"], load_hourly, load_q15)
    pv_q15 = hourly_to_q15_equal(pv_hourly)
    _set_metric_effective(day["pv"], pv_hourly, pv_q15)
    return day


def compute_pv_hourly_for_date(date_str: str, cfg: dict) -> list[float]:
    from .forecast import get_pv_hourly_for_date_sync

    pv, _src = get_pv_hourly_for_date_sync(cfg, date_str)
    while len(pv) < HOURS_PER_DAY:
        pv.append(0.0)
    return [round(float(v), 3) for v in pv[:HOURS_PER_DAY]]


def build_nightly_cache(cfg: dict, *, now: datetime | None = None) -> dict[str, Any]:
    """Compute base Load+PV for tomorrow and day-after; reset overrides in config."""
    now = now or now_warsaw()
    d1 = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    cache: dict[str, Any] = {
        "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "anchor_date": now.strftime("%Y-%m-%d"),
        "days": {},
    }
    for date_str in (d1, d2):
        pv_h = compute_pv_hourly_for_date(date_str, cfg)
        cache["days"][date_str] = build_day_forecast(date_str, cfg, pv_h)

    save_day_cache(cache)

    overrides = cfg.setdefault("overrides", {})
    for key in ("today_pv_kwh", "today_load_kwh", "tomorrow_pv_kwh", "tomorrow_load_kwh"):
        overrides[key] = None
    save_config(cfg)
    log.info("Nightly forecast cache saved for %s, %s", d1, d2)
    return cache


def ensure_cache_days(cfg: dict, dates: list[str]) -> dict[str, Any]:
    """Return cache, building any missing dates on demand (dev / first boot)."""
    cache = load_day_cache()
    days: dict[str, Any] = cache.setdefault("days", {})
    changed = False
    for date_str in dates:
        if date_str in days and days[date_str].get("load", {}).get("base_hourly"):
            continue
        pv_h = compute_pv_hourly_for_date(date_str, cfg)
        days[date_str] = build_day_forecast(date_str, cfg, pv_h)
        changed = True
    if changed:
        cache["computed_at"] = now_warsaw().strftime("%Y-%m-%d %H:%M:%S")
        save_day_cache(cache)
    return cache


def apply_overrides_to_cache(cfg: dict) -> dict[str, Any]:
    """Scale effective Load/PV in file cache from sa-config overrides."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    cache = ensure_cache_days(cfg, [today_str, tomorrow_str])
    days = cache["days"]
    overrides: dict = cfg.get("overrides", {})

    pairs = (
        (today_str, "today_load_kwh", "load"),
        (today_str, "today_pv_kwh", "pv"),
        (tomorrow_str, "tomorrow_load_kwh", "load"),
        (tomorrow_str, "tomorrow_pv_kwh", "pv"),
    )
    for date_str, key, metric_name in pairs:
        if date_str not in days:
            continue
        metric = days[date_str][metric_name]
        val = overrides.get(key)
        target = float(val) if val is not None else None
        _scale_metric(metric, target)

    cache["computed_at"] = now_warsaw().strftime("%Y-%m-%d %H:%M:%S")
    save_day_cache(cache)
    return cache


def effective_hourly(cfg: dict, date_str: str, metric: str) -> list[float]:
    cache = ensure_cache_days(cfg, [date_str])
    day = cache["days"].get(date_str) or _empty_day()
    m = day.get(metric) or _empty_day_metric()
    hourly = list(m.get("effective_hourly") or [0.0] * HOURS_PER_DAY)
    while len(hourly) < HOURS_PER_DAY:
        hourly.append(0.0)
    return hourly[:HOURS_PER_DAY]


def baseline_totals(cfg: dict) -> dict[str, dict[str, float]]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    cache = ensure_cache_days(cfg, [today_str, tomorrow_str])
    out: dict[str, dict[str, float]] = {}
    for label, date_str in (("today", today_str), ("tomorrow", tomorrow_str)):
        day = cache["days"].get(date_str) or _empty_day()
        out[label] = {
            "pv_kwh": float(day["pv"].get("total_base") or 0.0),
            "load_kwh": float(day["load"].get("total_base") or 0.0),
        }
    return out
