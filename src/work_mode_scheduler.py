"""
Work mode scheduler — SRNE grid export requires On-grid during timed discharge hours.

At :58 Europe/Warsaw: Limit power to home load for the same condition (hour ending).

Invoked from hour_boundary_scheduler (:00 start, :58 end). Not a standalone cron job.
"""

from __future__ import annotations

import logging
from typing import Any

from . import sa_client
from .config import load_config
from .influxdb import now_warsaw
from .plan_simulation import build_plan_simulation, get_cached_plan
from .timer_plan import hour_has_timer_schedule

log = logging.getLogger(__name__)

_last_work_mode_sync: dict[str, Any] = {
    "ran_at": None,
    "phase": None,
    "hour": None,
    "timer_schedule": None,
    "work_mode_target": None,
    "work_mode_before": None,
    "work_mode_after": None,
    "skipped": False,
    "skip_reason": None,
    "ok": None,
    "error": None,
}


def get_last_work_mode_sync() -> dict[str, Any]:
    return dict(_last_work_mode_sync)


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


async def _apply_work_mode_for_hour(
    *,
    phase: str,
    target_mode: str,
) -> dict[str, Any]:
    """phase: 'start' (:00) or 'end' (:58)."""
    global _last_work_mode_sync

    now = now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": phase,
        "hour": hour,
        "timer_schedule": None,
        "work_mode_target": target_mode,
        "work_mode_before": None,
        "work_mode_after": None,
        "skipped": False,
        "skip_reason": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["skipped"] = True
            status["skip_reason"] = "smart_mode_disabled"
            status["ok"] = None
            _last_work_mode_sync = status
            return status

        rows = await _plan_rows(cfg)
        if not hour_has_timer_schedule(rows, hour):
            status["skipped"] = True
            status["skip_reason"] = "empty_timer_schedule"
            status["ok"] = True
            _last_work_mode_sync = status
            log.info(
                "Work mode %s skipped — hour %02d has no Timer Schedule",
                phase,
                hour,
            )
            return status

        row = next(r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL")
        timer_txt = str(row.get("timer_schedule") or "").strip()
        status["timer_schedule"] = timer_txt

        rules = await sa_client.get_rules(cfg)
        before = rules.get("work_mode")
        status["work_mode_before"] = before
        if before == target_mode:
            status["work_mode_after"] = before
            status["skipped"] = True
            status["skip_reason"] = "already_set"
            status["ok"] = True
            _last_work_mode_sync = status
            log.info(
                "Work mode %s skipped — already %s (hour %02d, timer=%s)",
                phase,
                target_mode,
                hour,
                timer_txt,
            )
            return status

        log.info(
            "Work mode %s — hour %02d timer=%s: %s → %s",
            phase,
            hour,
            timer_txt,
            before,
            target_mode,
        )
        ok = await sa_client.set_work_mode(cfg, target_mode)
        status["ok"] = ok
        rules_after = await sa_client.get_rules(cfg, fresh=True)
        status["work_mode_after"] = rules_after.get("work_mode")
        if not ok:
            status["error"] = (
                f"SA did not confirm work mode {target_mode!r} within "
                f"{int(sa_client.WORK_MODE_VERIFY_TIMEOUT_S)}s"
            )
        _last_work_mode_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_work_mode_sync = status
        log.exception("Work mode %s job failed", phase)
        return status


async def run_work_mode_hour_start() -> dict[str, Any]:
    """:00 — On-grid for hours with a planned Timer Schedule."""
    return await _apply_work_mode_for_hour(
        phase="start",
        target_mode=sa_client.WORK_MODE_ON_GRID,
    )


async def run_work_mode_hour_end() -> dict[str, Any]:
    """:58 — Limit power to home load after timed export/charge hour."""
    return await _apply_work_mode_for_hour(
        phase="end",
        target_mode=sa_client.WORK_MODE_LIMIT_HOME_LOAD,
    )

