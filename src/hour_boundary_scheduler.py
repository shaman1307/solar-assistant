"""
Hour boundary SA sync — :00, :15/:30/:45 Europe/Warsaw.

Writes to SA from the current hour's Energy arbitrage Timer Schedule cell
(e.g. Dis 19:30-20:00 6.51kW cap16%), not merged multi-hour proposed_schedule.
Work mode: On-grid at :00 when Timer Schedule (not charge-grid) or SOC is 100%;
for charge-grid hours ensure Limit home + paired battery mode before timer write;
Limit home load at :00/:15/:30/:45 when timer empty or discharge ended.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import sa_client
from .config import load_config
from .influxdb import now_warsaw
from .sqlite_store import read_plan
from .timer_plan import (
    build_sa_schedule_from_hour_row,
    hour_has_timer_schedule,
    timer_discharge_active_at,
)
from .work_mode_scheduler import (
    on_grid_job_applied,
    run_work_mode_hour_start,
    run_work_mode_limit_home,
)

log = logging.getLogger(__name__)

_last_hour_boundary_sync: dict[str, Any] = {
    "ran_at": None,
    "phase": None,
    "minute": None,
    "hour": None,
    "timer_schedule": None,
    "work_mode": None,
    "work_mode_limit": None,
    "timer_sync": None,
    "timed_power": None,
    "ok": None,
    "error": None,
}


def get_last_hour_boundary_sync() -> dict[str, Any]:
    return dict(_last_hour_boundary_sync)


def _smart_mode_enabled(cfg: dict) -> bool:
    return bool(cfg.get("smart_mode_enabled", False))


async def _plan_rows(cfg: dict) -> list[dict]:
    """Return plan rows from SQLite (sole source of truth)."""
    del cfg
    stored = read_plan()
    if not stored or not stored.get("rows"):
        log.warning("No plan in SQLite — SA timer sync skipped")
        return []
    return stored["rows"]


async def _sync_timer_from_hour_row(
    cfg: dict,
    rows: list[dict],
    hour: int,
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "hour": hour,
        "timer_schedule": None,
        "skipped": False,
        "skip_reason": None,
        "ok": None,
        "error": None,
    }
    if not hour_has_timer_schedule(rows, hour):
        status["skipped"] = True
        status["skip_reason"] = "empty_timer_schedule"
        status["ok"] = True
        return status

    row = next(r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL")
    timer_txt = str(row.get("timer_schedule") or "").strip()
    status["timer_schedule"] = timer_txt

    if not timer_txt:
        status["skipped"] = True
        status["skip_reason"] = "empty_timer_schedule"
        status["ok"] = True
        return status

    rules = await sa_client.get_rules(cfg)
    rows_for_build = list(rows)
    for i, r in enumerate(rows_for_build):
        if r.get("hour") == hour and r.get("start") != "TOTAL":
            rows_for_build[i] = {**r, "timer_schedule": timer_txt}
            break
    schedule = build_sa_schedule_from_hour_row(rows_for_build, hour, cfg, existing=rules)
    if not schedule:
        status["skipped"] = True
        status["skip_reason"] = "unparsed_timer_schedule"
        status["ok"] = False
        status["error"] = f"Could not parse timer schedule: {timer_txt!r}"
        return status

    log.info("SA timer sync — hour %02d from row: %s", hour, timer_txt)
    ok = await sa_client.apply_hourly_schedule_to_sa(cfg, schedule)
    status["ok"] = ok
    if not ok:
        status["error"] = "SA timer write failed"
    return status


async def _clear_timed_power_flags(cfg: dict) -> dict[str, Any]:
    """Disable Timed charge / Timed discharge on SA (checkboxes only, no slots)."""
    status: dict[str, Any] = {"ok": None, "error": None}
    ok = await sa_client.set_timed_power_flags(
        cfg,
        timed_charge_enabled=False,
        timed_discharge_enabled=False,
    )
    status["ok"] = ok
    if not ok:
        status["error"] = "SA timed power flags write failed"
    return status


async def run_hour_boundary_start() -> dict[str, Any]:
    """:00 — work mode first (On-grid or charge Limit-home repair), then timer sync."""
    global _last_hour_boundary_sync

    now = now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": "start",
        "minute": now.minute,
        "hour": hour,
        "timer_schedule": None,
        "work_mode": None,
        "work_mode_limit": None,
        "timer_sync": None,
        "timed_power": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["ok"] = None
            status["error"] = "smart_mode_disabled"
            _last_hour_boundary_sync = status
            return status

        rows = await _plan_rows(cfg)
        if hour_has_timer_schedule(rows, hour):
            row = next(r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL")
            status["timer_schedule"] = str(row.get("timer_schedule") or "").strip()

        status["work_mode"] = await run_work_mode_hour_start()
        status["timer_sync"] = await _sync_timer_from_hour_row(cfg, rows, hour)

        # Charge-grid prepare already set Limit home; do not run Limit-home again
        # (and never clear timed_charge while a charge window is active).
        charge_prepared = bool((status["work_mode"] or {}).get("charge_grid_prepare"))
        if on_grid_job_applied(status["work_mode"]) or charge_prepared:
            status["work_mode_limit"] = {
                "skipped": True,
                "skip_reason": (
                    "charge_grid_prepare" if charge_prepared else "on_grid_applied"
                ),
                "ok": True,
            }
        else:
            limit_status = await run_work_mode_limit_home()
            status["work_mode_limit"] = limit_status
            if limit_status.get("limit_due"):
                row = next(
                    (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
                    None,
                )
                timer_txt = str(row.get("timer_schedule") or "").strip() if row else ""
                discharge_still_active = bool(
                    timer_txt and timer_discharge_active_at(timer_txt, now),
                )
                if not discharge_still_active:
                    status["timed_power"] = await _clear_timed_power_flags(cfg)

        wm = status["work_mode"]
        ts = status["timer_sync"]
        tp = status.get("timed_power")
        limit_wm = status.get("work_mode_limit") or {}

        if not hour_has_timer_schedule(rows, hour):
            status["ok"] = True
        else:
            status["ok"] = (wm.get("ok") is not False) and (ts.get("ok") is not False)

        if limit_wm.get("limit_due"):
            status["ok"] = status["ok"] and (limit_wm.get("ok") is not False)
            if tp is not None:
                status["ok"] = status["ok"] and (tp.get("ok") is not False)

        if status["ok"] is False:
            parts = []
            if wm.get("ok") is False:
                parts.append("work mode on-grid")
            if ts.get("ok") is False:
                parts.append("timer")
            if limit_wm.get("ok") is False:
                parts.append("work mode limit")
            if tp and tp.get("ok") is False:
                parts.append("timed power")
            status["error"] = "SA sync failed: " + ", ".join(parts)

        _last_hour_boundary_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_hour_boundary_sync = status
        log.exception("Hour boundary sync failed (start)")
        return status


async def run_hour_boundary_limit_home() -> dict[str, Any]:
    """:15/:30/:45 — Limit home load when discharge ended and PV is zero."""
    global _last_hour_boundary_sync

    now = now_warsaw()
    status: dict[str, Any] = {
        "ran_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "limit_home",
        "minute": now.minute,
        "hour": now.hour,
        "timer_schedule": None,
        "work_mode": None,
        "work_mode_limit": None,
        "timer_sync": {
            "skipped": True,
            "skip_reason": "limit_home_no_timer_write",
            "ok": True,
        },
        "timed_power": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["ok"] = None
            status["error"] = "smart_mode_disabled"
            _last_hour_boundary_sync = status
            return status

        limit_status = await run_work_mode_limit_home()
        status["work_mode"] = limit_status
        status["work_mode_limit"] = limit_status
        if limit_status.get("timer_schedule"):
            status["timer_schedule"] = limit_status["timer_schedule"]
        if limit_status.get("limit_due"):
            status["timed_power"] = await _clear_timed_power_flags(cfg)
        else:
            status["timed_power"] = {
                "skipped": True,
                "skip_reason": limit_status.get("skip_reason"),
                "ok": True,
            }

        tp = status.get("timed_power") or {}
        if limit_status.get("skipped") and not limit_status.get("limit_due"):
            status["ok"] = True
        else:
            status["ok"] = (tp.get("ok") is not False) and (limit_status.get("ok") is not False)
        if status["ok"] is False:
            parts = []
            if tp.get("ok") is False:
                parts.append("timed power")
            if limit_status.get("ok") is False:
                parts.append("work mode limit")
            status["error"] = "SA sync failed: " + ", ".join(parts)
        _last_hour_boundary_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_hour_boundary_sync = status
        log.exception("Hour boundary limit home failed")
        return status


def register_hour_boundary_jobs(scheduler: AsyncIOScheduler) -> None:
    """Limit-home at :15/:30/:45 runs from quarter_plan_refresh (after plan write)."""
    log.info(
        "Hour boundary SA sync: :00 On-grid + hour Timer Schedule (after plan refresh); "
        ":15/:30/:45 Limit home when timer empty or discharge ended (after plan refresh).",
    )
