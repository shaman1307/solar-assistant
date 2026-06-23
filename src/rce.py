"""
RCE (Rynek Cen Energii) electricity price fetcher — PSE public API.

API endpoint: https://api.raporty.pse.pl/api/rce-pln
Resolution:   15-minute intervals
Unit:         PLN/MWh  →  divide by 1000 → PLN/kWh
Tomorrow:     published by PSE usually after 14:00.

Returned structure:
  {
    "current_price_pln_kwh": 0.312,          # price for the current 15-min slot
    "current_period": "2026-06-18 14:00",    # slot label
    "today":    [0.312, 0.298, ...],         # 24 hourly averages (PLN/kWh)
    "tomorrow": [0.280, 0.270, ...],         # 24 hourly averages (may be null if not yet published)
    "series_15min": [...],                   # 96 slots/day at 15-min resolution (PLN/kWh)
    "dates": {
        "today":    "2026-06-18",
        "tomorrow": "2026-06-19",
    }
  }
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import requests

from .influxdb import now_warsaw

log = logging.getLogger(__name__)

PSE_API_BASE = "https://api.raporty.pse.pl/api"
CACHE_TTL_S = 1800  # 30 minutes — matches PSE publish cadence
_cache: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------

async def get_rce_prices() -> dict[str, Any]:
    """Fetch and return RCE price data (async wrapper — runs sync call in thread)."""
    return await asyncio.to_thread(_get_prices_sync)


def get_rce_prices_sync() -> dict[str, Any]:
    """Synchronous version for use from non-async contexts."""
    return _get_prices_sync()


def invalidate_cache() -> None:
    _cache.clear()


def hourly_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    """Hourly RCE (PLN/kWh) for each YYYY-MM-DD date via PSE API."""
    unique = sorted({d for d in dates if d})
    if not unique:
        return {}
    raw = _fetch_rce(unique[0], unique[-1])
    buckets_60: dict[tuple[str, int], list[float]] = defaultdict(list)
    for rec in raw:
        dtime_str: str = rec.get("dtime", "")
        rce_mwh: float | None = rec.get("rce_pln")
        if not dtime_str or rce_mwh is None:
            continue
        try:
            dt = datetime.strptime(dtime_str[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        buckets_60[(dt.strftime("%Y-%m-%d"), dt.hour)].append(float(rce_mwh) / 1000.0)

    def _avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        d: [_avg(buckets_60.get((d, h), [])) for h in range(24)]
        for d in unique
    }


def hourly_mean_from_quarters(quarters: list[float | None]) -> list[float | None]:
    """Hourly mean from 96 real 15-min RCE slots (for display only)."""
    out: list[float | None] = []
    for h in range(24):
        chunk = quarters[h * 4:(h + 1) * 4]
        vals = [float(v) for v in chunk if v is not None]
        out.append(round(sum(vals) / len(vals), 4) if vals else None)
    return out


async def get_hourly_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    return await asyncio.to_thread(hourly_rce_for_dates, *dates)


def quarter_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    """96-slot RCE (PLN/kWh) per date — index = hour*4 + quarter."""
    unique = sorted({d for d in dates if d})
    if not unique:
        return {}
    raw = _fetch_rce(unique[0], unique[-1])
    buckets_15: dict[tuple[str, int, int], float] = {}
    for rec in raw:
        dtime_str: str = rec.get("dtime", "")
        rce_mwh: float | None = rec.get("rce_pln")
        if not dtime_str or rce_mwh is None:
            continue
        try:
            dt = datetime.strptime(dtime_str[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        date_key = dt.strftime("%Y-%m-%d")
        quarter = dt.minute // 15
        buckets_15[(date_key, dt.hour, quarter)] = round(float(rce_mwh) / 1000.0, 4)

    return {
        d: [
            buckets_15.get((d, h, q))
            for h in range(24)
            for q in range(4)
        ]
        for d in unique
    }


async def get_quarter_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    return await asyncio.to_thread(quarter_rce_for_dates, *dates)


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _current_period_end(now: datetime) -> tuple[str, int, int]:
    """PSE dtime is period end; return (date, hour, quarter) for the slot containing *now*."""
    cq = now.minute // 15
    end_minute = (cq + 1) * 15
    end_dt = now.replace(second=0, microsecond=0)
    if end_minute >= 60:
        end_dt = end_dt.replace(minute=0) + timedelta(hours=1)
    else:
        end_dt = end_dt.replace(minute=end_minute)
    return end_dt.strftime("%Y-%m-%d"), end_dt.hour, end_dt.minute // 15


def _refresh_current_price(data: dict[str, Any]) -> None:
    """Recompute live current slot price (safe to call on cached series)."""
    now = now_warsaw()
    date_key, end_hour, end_q = _current_period_end(now)
    time_label = f"{end_hour:02d}:{end_q * 15:02d}"
    current_price = None
    for slot in data.get("series_15min", []):
        if slot.get("date") == date_key and slot.get("time") == time_label:
            current_price = slot.get("price")
            break
    data["current_price_pln_kwh"] = current_price
    data["current_period"] = now.strftime("%Y-%m-%d %H:%M")


def _get_prices_sync() -> dict[str, Any]:
    global _cache
    now_ts = time.time()
    if _cache.get("ts", 0) > now_ts - CACHE_TTL_S:
        data = dict(_cache["data"])
        _refresh_current_price(data)
        return data

    result = _fetch_and_build()
    if any(p is not None for p in result.get("today", [])):
        _cache = {"ts": now_ts, "data": result}
    _refresh_current_price(result)
    return result


def _fetch_and_build() -> dict[str, Any]:
    now = now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    raw = _fetch_rce(today_str, tomorrow_str)

    # Parse 15-min records: one price per (date, hour, quarter).
    buckets_15: dict[tuple[str, int, int], float] = {}
    buckets_30: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    buckets_60: dict[tuple[str, int], list[float]] = defaultdict(list)

    for rec in raw:
        dtime_str: str = rec.get("dtime", "")
        rce_mwh: float | None = rec.get("rce_pln")
        if not dtime_str or rce_mwh is None:
            continue
        try:
            dt = datetime.strptime(dtime_str[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        date_key = dt.strftime("%Y-%m-%d")
        pln_kwh = float(rce_mwh) / 1000.0
        quarter = dt.minute // 15
        buckets_15[(date_key, dt.hour, quarter)] = pln_kwh
        half = 0 if dt.minute < 30 else 1
        buckets_30[(date_key, dt.hour, half)].append(pln_kwh)
        buckets_60[(date_key, dt.hour)].append(pln_kwh)

    def _avg(vals: list) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    def hourly(date_str: str) -> list[float | None]:
        return [_avg(buckets_60.get((date_str, h), [])) for h in range(24)]

    def quarter_hourly(date_str: str) -> list[dict]:
        """Return 96 slots per day (15-min resolution)."""
        result = []
        for h in range(24):
            for q in range(4):
                label = f"{h:02d}:{q * 15:02d}"
                price = buckets_15.get((date_str, h, q))
                result.append({"time": label, "price": round(price, 4) if price is not None else None})
        return result

    def half_hourly(date_str: str) -> list[dict]:
        """Return 48 slots per day, each with time label and price."""
        result = []
        for h in range(24):
            for half in range(2):
                label = f"{h:02d}:{30*half:02d}"
                vals = buckets_30.get((date_str, h, half), [])
                result.append({"time": label, "price": _avg(vals)})
        return result

    today_prices = hourly(today_str)
    tomorrow_prices = hourly(tomorrow_str)

    tomorrow_has_prices = any(
        buckets_15.get((tomorrow_str, h, q)) is not None
        for h in range(24) for q in range(4)
    )

    # Series from next 15-min period until end of today (+ tomorrow when published).
    series_from_now: list[dict] = []
    _, cur_end_hour, cur_end_q = _current_period_end(now)
    start_h, start_q = cur_end_hour, cur_end_q + 1
    if start_q >= 4:
        start_q = 0
        start_h += 1

    for h in range(start_h, 24):
        q_start = start_q if h == start_h else 0
        for q in range(q_start, 4):
            price = buckets_15.get((today_str, h, q))
            series_from_now.append({
                "day": "today", "date": today_str,
                "time": f"{h:02d}:{q * 15:02d}",
                "price": round(price, 4) if price is not None else None,
            })

    if tomorrow_has_prices:
        for h in range(24):
            for q in range(4):
                price = buckets_15.get((tomorrow_str, h, q))
                series_from_now.append({
                    "day": "tomorrow", "date": tomorrow_str,
                    "time": f"{h:02d}:{q * 15:02d}",
                    "price": round(price, 4) if price is not None else None,
                })

    series_15min: list[dict] = []
    for slot in quarter_hourly(today_str):
        series_15min.append({"day": "today", "date": today_str, **slot})
    for slot in quarter_hourly(tomorrow_str):
        series_15min.append({"day": "tomorrow", "date": tomorrow_str, **slot})

    series_30min = []
    for slot in half_hourly(today_str):
        series_30min.append({"day": "today", "date": today_str, **slot})
    for slot in half_hourly(tomorrow_str):
        series_30min.append({"day": "tomorrow", "date": tomorrow_str, **slot})

    result = {
        "current_price_pln_kwh": None,
        "current_period": now.strftime("%Y-%m-%d %H:%M"),
        "today": today_prices,
        "tomorrow": tomorrow_prices,
        "series_from_now": series_from_now,
        "series_15min": series_15min,
        "series_30min": series_30min,
        "tomorrow_has_prices": tomorrow_has_prices,
        "dates": {"today": today_str, "tomorrow": tomorrow_str},
    }
    _refresh_current_price(result)
    return result


def _fetch_rce(date_from: str, date_to: str) -> list[dict]:
    """Call PSE OData API and return raw list of 15-min records.

    NOTE: The requests library percent-encodes `$` in param keys ($filter →
    %24filter), which PSE API rejects.  We build the query string manually
    to keep literal `$` characters.

    Follows PSE ``nextLink`` cursor pagination ($first=500 ≈ 5 days per page).
    """
    qs = (
        f"$select=business_date,dtime,rce_pln"
        f"&$filter=business_date ge '{date_from}' and business_date le '{date_to}'"
        f"&$orderby=dtime asc"
        f"&$first=500"
    )
    url: str | None = f"{PSE_API_BASE}/rce-pln?{qs}"
    out: list[dict] = []
    while url:
        try:
            resp = requests.get(url, timeout=20, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("value", [])
            if batch:
                out.extend(batch)
            url = data.get("nextLink")
        except Exception as exc:
            log.warning("PSE RCE fetch failed: %s", exc)
            break
    return out


# ---------------------------------------------------------------------------
# Helpers used by other modules
# ---------------------------------------------------------------------------

def build_price_hourly_series(rce_data: dict[str, Any], cfg: dict) -> list[float | None]:
    """Build a continuous hourly price series from now until end of tomorrow.

    Returns a list of PLN/kWh prices indexed sequentially starting at the
    current hour.  Returns None for hours where RCE is not yet published
    (tomorrow before 14:00 publication).
    """
    now = now_warsaw()
    start_hour = now.hour
    today_str = now.strftime("%Y-%m-%d")
    tomorrow = now + timedelta(days=1)

    # Build indexed list: (date_str, hour) → price
    price_map: dict[tuple[str, int], float | None] = {}
    for h, p in enumerate(rce_data.get("today", [])):
        price_map[(today_str, h)] = p
    for h, p in enumerate(rce_data.get("tomorrow", [])):
        price_map[(tomorrow.strftime("%Y-%m-%d"), h)] = p

    series: list[float | None] = []
    for step in range(_simulation_steps(now)):
        dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=step)
        series.append(price_map.get((dt.strftime("%Y-%m-%d"), dt.hour)))
    return series


def _simulation_steps(now: datetime) -> int:
    """Number of hourly steps from current hour to 23:00 tomorrow (inclusive)."""
    end = (now + timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    start = now.replace(minute=0, second=0, microsecond=0)
    return int((end - start).total_seconds() // 3600) + 1


def effective_feed_in_price(
    rce_price: float | None,
    cfg: dict,
    use_rce: bool = False,
) -> float:
    """Return the effective feed-in price for this hour.

    If SA grid-export rule is active and a live RCE price is available,
    use the RCE price (in PLN/kWh).  Otherwise fall back to the configured
    fixed feed-in price.
    """
    if use_rce and rce_price is not None:
        return rce_price
    return float(cfg["grid"]["feed_in_price_pln"])
