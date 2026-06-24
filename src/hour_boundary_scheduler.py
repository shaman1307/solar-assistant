"""
Hour boundary SA sync — :00 and :58 Europe/Warsaw only.

Writes to SA from the current hour's Energy arbitrage Timer Schedule cell
(e.g. Dis 19:30-20:00 6.51kW cap16%), not merged multi-hour proposed_schedule.
Also switches Work mode (On-grid at :00, Limit power to home load at :58).
Timer Schedule is written to SA at :00 only; :58 is work mode only.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import sa_client
from .config import load_config
from .influxdb import now_warsaw
from .plan_simulation import build_plan_simulation, get_cached_plan
from .timer_plan import build_sa_schedule_from_hour_row, hour_has_timer_schedule
from .work_mode_scheduler import run_work_mode_hour_end, run_work_mode_hour_start

log = logging.getLogger(__name__)

_last_hour_boundary_sync: dict[str, Any] = {
    "ran_at": None,
    "phase": None,
    "minute": None,
    "hour": None,
    "timer_schedule": None,
    "work_mode": None,
    "timer_sync": None,
    "ok": None,
    "error": None,
}


def get_last_hour_boundary_sync() -> dict[str, Any]:
    return dict(_last_hour_boundary_sync)


def _smart_mode_enabled(cfg: dict) -> bool:
    return bool(cfg.get("smart_mode_enabled", False))


async def _plan_rows(cfg: dict) -> list[dict]:
    cached = get_cached_plan()
    if cached and cached.get("rows"):
        return cached["rows"]
    result = await build_plan_simulation(
        cfg,
        force_refresh=False,
        invalidate_inputs=False,
    )
    return result.get("rows") or []


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

    rules = await sa_client.get_rules(cfg)
    schedule = build_sa_schedule_from_hour_row(rows, hour, cfg, existing=rules)
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


async def run_hour_boundary_sync(*, phase: str) -> dict[str, Any]:
    """phase: 'start' (:00) or 'end' (:58)."""
    global _last_hour_boundary_sync

    now = now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": phase,
        "minute": now.minute,
        "hour": hour,
        "timer_schedule": None,
        "work_mode": None,
        "timer_sync": None,
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

        if phase == "start":
            status["work_mode"] = await run_work_mode_hour_start()
            status["timer_sync"] = await _sync_timer_from_hour_row(cfg, rows, hour)
        else:
            status["work_mode"] = await run_work_mode_hour_end()
            status["timer_sync"] = {
                "skipped": True,
                "skip_reason": "end_phase_work_mode_only",
                "ok": True,
            }

        wm = status["work_mode"]
        ts = status["timer_sync"]
        if not hour_has_timer_schedule(rows, hour):
            status["ok"] = True
        elif phase == "end":
            status["ok"] = wm.get("ok") is not False
        else:
            status["ok"] = (wm.get("ok") is not False) and (ts.get("ok") is not False)

        if status["ok"] is False:
            parts = []
            if wm.get("ok") is False:
                parts.append("work mode")
            if phase == "start" and ts.get("ok") is False:
                parts.append("timer")
            status["error"] = "SA sync failed: " + ", ".join(parts)

        _last_hour_boundary_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_hour_boundary_sync = status
        log.exception("Hour boundary sync failed (%s)", phase)
        return status


async def run_hour_boundary_start() -> dict[str, Any]:
    return await run_hour_boundary_sync(phase="start")


async def run_hour_boundary_end() -> dict[str, Any]:
    return await run_hour_boundary_sync(phase="end")


def register_hour_boundary_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register :58 end job (:00 start runs after plan refresh in quarter_plan_refresh)."""
    scheduler.add_job(
        run_hour_boundary_end,
        trigger=CronTrigger(minute=58, timezone="Europe/Warsaw"),
        id="hour_boundary_end",
        replace_existing=True,
        misfire_grace_time=120,
    )
    log.info(
        "Hour boundary SA sync: :00 On-grid + hour Timer Schedule (after plan refresh); "
        ":58 Limit home only (smart mode) — Europe/Warsaw.",
    )
