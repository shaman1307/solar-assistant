"""
PV generation forecast (Open-Meteo) and Load forecast (weekday cache).

Load/PV profiles for today and tomorrow come from the nightly file cache
(see forecast_cache.py). PV past hours today use Influx actuals.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any

import requests

from . import forecast_cache as fc
from .influxdb import now_warsaw

log = logging.getLogger(__name__)

CACHE_TTL_S = 3600
_pv_cache: dict[str, Any] = {}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEZONE = "Europe/Warsaw"
OM_TIMEOUT_S = 20

_archive_cache: dict[tuple, dict[str, Any]] = {}
ARCHIVE_CACHE_TTL_S = 86400


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_forecast(
    cfg: dict,
    *,
    today_pv_actual: list[float | None] | None = None,
) -> dict[str, Any]:
    """Return hourly PV + load for today and tomorrow (cache + PV actuals)."""
    return await asyncio.to_thread(_assemble_forecast, cfg, today_pv_actual)


async def get_pv_profiles(cfg: dict) -> dict[str, list[float]]:
    """Cached today/tomorrow PV hourly kWh from day cache (effective)."""
    return await asyncio.to_thread(_pv_from_day_cache, cfg)


async def run_nightly_forecast_cache(cfg: dict) -> dict[str, Any]:
    """23:59 job: base Load+PV for D+1/D+2, reset overrides."""
    return await asyncio.to_thread(fc.build_nightly_cache, cfg)


async def apply_overrides_to_cache(cfg: dict) -> dict[str, Any]:
    return await asyncio.to_thread(fc.apply_overrides_to_cache, cfg)


def _pv_from_day_cache(cfg: dict) -> dict[str, list[float]]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    fc.ensure_cache_days(cfg, [today_str, tomorrow_str])
    return {
        "today": fc.effective_hourly(cfg, today_str, "pv"),
        "tomorrow": fc.effective_hourly(cfg, tomorrow_str, "pv"),
    }


# ---------------------------------------------------------------------------
# PV — Open-Meteo (used for nightly cache build and horizon dates)
# ---------------------------------------------------------------------------

def _pv_cache_key(cfg: dict) -> tuple:
    solar = cfg["solar"]
    blocks = tuple(
        (b["power_kwp"], b["tilt"]) for b in solar.get("blocks", [])
    )
    return (
        round(cfg["location"]["latitude"], 4),
        round(cfg["location"]["longitude"], 4),
        round(solar["azimuth"], 2),
        round(solar["system_loss_factor"], 4),
        blocks,
    )


def _get_pv_profiles_sync(cfg: dict) -> dict[str, list[float]]:
    """Legacy Open-Meteo rolling cache (horizon / fallback only)."""
    global _pv_cache
    key = _pv_cache_key(cfg)
    now = time.time()
    cached = _pv_cache.get(key)
    if cached and cached.get("ts", 0) > now - CACHE_TTL_S:
        return cached["data"]

    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    today_pv = [0.0] * 24
    tomorrow_pv = [0.0] * 24
    fetch_ok = False

    try:
        hourly = _fetch_irradiance(cfg["location"]["latitude"], cfg["location"]["longitude"])
        times: list[str] = hourly["time"]
        direct: list[float] = hourly["direct_radiation"]
        diffuse: list[float] = hourly["diffuse_radiation"]
        today_pv = _fill_pv_for_date(cfg, times, direct, diffuse, today_str)
        tomorrow_pv = _fill_pv_for_date(cfg, times, direct, diffuse, tomorrow_str)
        fetch_ok = True
    except Exception as exc:
        log.warning("Open-Meteo PV fetch failed: %s", exc)
        if cached:
            return cached["data"]

    data = {"today": today_pv, "tomorrow": tomorrow_pv}
    if fetch_ok:
        _pv_cache[key] = {"ts": now, "data": data}
    return data


def get_pv_hourly_for_date_sync(cfg: dict, date_str: str) -> tuple[list[float], str]:
    """Open-Meteo PV hourly kWh for any calendar date."""
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    today = now_warsaw().date()
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    offset = (target - today).days

    if offset < 0:
        cache_key = (_pv_cache_key(cfg), date_str)
        now = time.time()
        cached = _archive_cache.get(cache_key)
        if cached and cached.get("ts", 0) > now - ARCHIVE_CACHE_TTL_S:
            return list(cached["data"]), "open-meteo-archive"
        try:
            hourly = _fetch_irradiance_archive(lat, lon, date_str)
            pv = _fill_pv_for_date(
                cfg,
                hourly["time"],
                hourly["direct_radiation"],
                hourly["diffuse_radiation"],
                date_str,
            )
            _archive_cache[cache_key] = {"ts": now, "data": pv}
            return pv, "open-meteo-archive"
        except Exception as exc:
            log.warning("Open-Meteo archive PV for %s failed: %s", date_str, exc)
            return [0.0] * 24, "open-meteo-archive"

    try:
        hourly = _fetch_irradiance(lat, lon, forecast_days=max(offset + 1, 2))
        pv = _fill_pv_for_date(
            cfg,
            hourly["time"],
            hourly["direct_radiation"],
            hourly["diffuse_radiation"],
            date_str,
        )
        return pv, "forecast"
    except Exception as exc:
        log.warning("Open-Meteo PV for %s failed: %s", date_str, exc)
        return [0.0] * 24, "forecast"


def _fetch_irradiance_archive(lat: float, lon: float, date_str: str) -> dict[str, list]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": "direct_radiation,diffuse_radiation",
        "timezone": TIMEZONE,
    }
    resp = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=OM_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()["hourly"]


def _fetch_irradiance(lat: float, lon: float, *, forecast_days: int = 2) -> dict[str, list]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "direct_radiation,diffuse_radiation",
        "forecast_days": max(2, min(int(forecast_days), 16)),
        "timezone": TIMEZONE,
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=OM_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()["hourly"]


def _fill_pv_for_date(
    cfg: dict,
    times: list[str],
    direct: list[float],
    diffuse: list[float],
    date_str: str,
) -> list[float]:
    pv = [0.0] * 24
    lat = cfg["location"]["latitude"]
    azimuth = cfg["solar"]["azimuth"]
    loss = cfg["solar"]["system_loss_factor"]
    blocks = cfg["solar"]["blocks"]

    for t, d, df in zip(times, direct, diffuse):
        if t[:10] != date_str:
            continue
        hour = int(t[11:13])
        dt = datetime.strptime(t[:16], "%Y-%m-%dT%H:%M")
        sun_alt, sun_az = _solar_position(lat, dt)
        kwh = 0.0
        for block in blocks:
            kwh += _block_hour_kwh(
                d, df,
                block["power_kwp"],
                block["tilt"],
                azimuth,
                sun_alt,
                sun_az,
                loss,
            )
        pv[hour] += kwh
    return pv


def _solar_position(lat_deg: float, dt: datetime) -> tuple[float, float]:
    day_of_year = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60.0
    declination = math.radians(23.45 * math.sin(math.radians(360 / 365 * (day_of_year + 284))))
    hour_angle = math.radians(15 * (hour - 12))
    lat_rad = math.radians(lat_deg)
    altitude = math.asin(
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    denom = math.cos(altitude) * math.cos(lat_rad)
    cos_az = (
        (math.sin(declination) - math.sin(altitude) * math.sin(lat_rad)) / denom
        if abs(denom) > 1e-9
        else 0.0
    )
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az) if hour_angle < 0 else 2 * math.pi - math.acos(cos_az)
    return altitude, az


def _block_hour_kwh(
    direct: float,
    diffuse: float,
    power_kwp: float,
    tilt_deg: float,
    azimuth_compass: float,
    sun_alt: float,
    sun_az: float,
    loss: float,
) -> float:
    if sun_alt <= 0.05:
        return 0.0
    t_r = math.radians(tilt_deg)
    az_r = math.radians(azimuth_compass)
    cos_i = (
        math.sin(sun_alt) * math.cos(t_r)
        + math.cos(sun_alt) * math.sin(t_r) * math.cos(sun_az - az_r)
    )
    proj = max(0.0, cos_i / math.sin(sun_alt))
    irr_wh_m2 = (direct or 0) * proj + (diffuse or 0) * (1 + math.cos(t_r)) / 2
    return irr_wh_m2 / 1000.0 * power_kwp * loss


# ---------------------------------------------------------------------------
# Assembly (today / tomorrow from file cache)
# ---------------------------------------------------------------------------

def _assemble_forecast(
    cfg: dict,
    today_pv_actual_raw: list[float | None] | None = None,
) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    fc.ensure_cache_days(cfg, [today_str, tomorrow_str])

    today_pv_cached = fc.effective_hourly(cfg, today_str, "pv")
    tomorrow_pv = fc.effective_hourly(cfg, tomorrow_str, "pv")
    today_load = fc.effective_hourly(cfg, today_str, "load")
    tomorrow_load = fc.effective_hourly(cfg, tomorrow_str, "load")

    today_pv_actual_raw = today_pv_actual_raw or [None] * 24
    today_pv_actual = [
        round(float(v), 3) if v is not None else None
        for v in today_pv_actual_raw[:24]
    ]
    while len(today_pv_actual) < 24:
        today_pv_actual.append(None)

    today_pv = _today_pv_profile(today_pv_actual_raw, today_pv_cached)

    cache = fc.load_day_cache()
    day_t = cache.get("days", {}).get(today_str, {})
    day_tm = cache.get("days", {}).get(tomorrow_str, {})
    base_load_today = list((day_t.get("load") or {}).get("base_hourly") or today_load)
    base_load_tomorrow = list((day_tm.get("load") or {}).get("base_hourly") or tomorrow_load)

    base_today = _make_day(today_pv, today_load)
    base_today["pv_forecast"] = [round(v, 3) for v in today_pv_cached]
    base_today["pv_actual"] = today_pv_actual
    base_today["load_forecast"] = [round(v, 3) for v in today_load]

    base_tomorrow = _make_day(tomorrow_pv, tomorrow_load)
    base_tomorrow["pv_forecast"] = list(tomorrow_pv)
    base_tomorrow["pv_actual"] = [None] * 24
    base_tomorrow["load_forecast"] = [round(v, 3) for v in tomorrow_load]

    pv_actual_h = sum(1 for h in range(24) if today_pv_actual[h] is not None)
    baseline = fc.baseline_totals(cfg)

    result: dict[str, Any] = {
        "today": dict(base_today),
        "tomorrow": dict(base_tomorrow),
        "meta": {
            "today_pv_actual_hours": pv_actual_h,
            "today_pv_forecast_hours": 24 - pv_actual_h,
            "cache_computed_at": cache.get("computed_at"),
        },
        "baseline": baseline,
        "overrides_saved": {
            k: cfg.get("overrides", {}).get(k)
            for k in (
                "today_pv_kwh", "today_load_kwh",
                "tomorrow_pv_kwh", "tomorrow_load_kwh",
            )
        },
    }
    return result


def _today_pv_profile(
    actual: list[float | None] | None,
    forecast: list[float],
) -> list[float]:
    actual = actual or [None] * 24
    result: list[float] = []
    for h in range(24):
        v = actual[h] if h < len(actual) else None
        if v is not None:
            result.append(float(v))
        else:
            result.append(float(forecast[h]) if h < len(forecast) else 0.0)
    return result


def _make_day(pv_hourly: list[float], load_hourly: list[float]) -> dict:
    return {
        "pv": [round(v, 3) for v in pv_hourly],
        "load": [round(v, 3) for v in load_hourly],
        "pv_total": round(sum(pv_hourly), 2),
        "load_total": round(sum(load_hourly), 2),
    }


async def get_horizon_day_profile(date_str: str, cfg: dict) -> dict[str, Any]:
    """PV/load for a calendar date (cache for today/tomorrow, else compute)."""
    from . import influxdb as influxdb_mod

    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    offset = (datetime.strptime(date_str, "%Y-%m-%d").date() - now_warsaw().date()).days

    if date_str in (today_str, tomorrow_str):
        fc.ensure_cache_days(cfg, [date_str])
        pv = fc.effective_hourly(cfg, date_str, "pv")
        load = fc.effective_hourly(cfg, date_str, "load")
        source = "day-cache"
    else:
        pv, pv_source = await asyncio.to_thread(get_pv_hourly_for_date_sync, cfg, date_str)
        if offset < 0:
            accruals = await influxdb_mod.get_accruals_for_date(date_str)
            load_raw = accruals.get("hourly", {}).get("load") or []
            if any(v is not None for v in load_raw):
                load = [float(v) if v is not None else 0.0 for v in load_raw[:24]]
                source = f"{pv_source}+influx"
            else:
                _, load = fc.compute_weekday_load_profile(date_str, cfg)
                source = f"{pv_source}+weekday"
        else:
            _, load = await asyncio.to_thread(fc.compute_weekday_load_profile, date_str, cfg)
            source = f"{pv_source}+weekday"

    while len(pv) < 24:
        pv.append(0.0)
    while len(load) < 24:
        load.append(0.0)

    return {
        "date": date_str,
        "pv": [round(v, 3) for v in pv[:24]],
        "load": [round(v, 3) for v in load[:24]],
        "pv_total": round(sum(pv[:24]), 2),
        "load_total": round(sum(load[:24]), 2),
        "source": source,
    }


def invalidate_cache() -> None:
    """Clear Open-Meteo in-memory caches (not the daily file cache)."""
    _pv_cache.clear()
    _archive_cache.clear()
