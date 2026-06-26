"""
Work mode scheduler — SRNE grid export requires On-grid during timed discharge hours.

At :00 Europe/Warsaw: On-grid when Timer Schedule present (except charge-grid) or SOC is 100%.
At :00/:15/:30/:45: Limit power to home load when discharge has ended and live PV is zero.
"""

from __future__ import annotations

import logging
from typing import Any

from . import sa_client
from .config import load_config
from .influxdb import now_warsaw
from .plan_simulation import build_plan_simulation, get_cached_plan
from .timer_plan import (
    ACTION_CHARGE_GRID,
    hour_has_timer_schedule,
    normalize_action,
    plan_row_grid_export_kwh,
    timer_discharge_end_due,
)

log = logging.getLogger(__name__)

LIVE_PV_ZERO_KW = 0.01
SOC_FULL_PCT = 100.0

_last_work_mode_sync: dict[str, Any] = {
    "ran_at": None,
    "phase": None,
    "hour": None,
    "timer_schedule": None,
    "grid_export_kwh": None,
    "production_kw": None,
    "discharge_end_hhmm": None,
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


def on_grid_job_applied(status: dict[str, Any] | None) -> bool:
    """True when the :00 On-grid job set or already had On-grid."""
    if not status:
        return False
    if status.get("work_mode_target") != sa_client.WORK_MODE_ON_GRID:
        return False
    if status.get("skipped"):
        return status.get("skip_reason") == "already_set"
    return status.get("ok") is not False


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


def _hour_row(rows: list[dict], hour: int) -> dict[str, Any] | None:
    return next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )


def _hours_to_check_for_limit(now_hour: int) -> list[int]:
    hours = [now_hour]
    if now_hour > 0:
        hours.insert(0, now_hour - 1)
    return hours


async def _set_work_mode_if_needed(
    cfg: dict,
    *,
    target_mode: str,
    status: dict[str, Any],
    timer_txt: str,
) -> None:
    rules = await sa_client.get_rules(cfg)
    before = rules.get("work_mode")
    status["work_mode_before"] = before
    if before == target_mode:
        status["work_mode_after"] = before
        status["skipped"] = True
        status["skip_reason"] = "already_set"
        status["ok"] = True
        log.info(
            "Work mode %s skipped — already %s (hour %02d, timer=%s)",
            status["phase"],
            target_mode,
            status["hour"],
            timer_txt,
        )
        return

    log.info(
        "Work mode %s — hour %02d timer=%s production=%.3f kW: %s → %s",
        status["phase"],
        status["hour"],
        timer_txt or "—",
        float(status.get("production_kw") or 0),
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


async def run_work_mode_hour_start() -> dict[str, Any]:
    """:00 — On-grid when Timer Schedule (not charge-grid) or SOC is 100%."""
    global _last_work_mode_sync

    now = now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": "start",
        "hour": hour,
        "timer_schedule": None,
        "grid_export_kwh": None,
        "production_kw": None,
        "discharge_end_hhmm": None,
        "work_mode_target": sa_client.WORK_MODE_ON_GRID,
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
        row = _hour_row(rows, hour)
        if not row:
            status["skipped"] = True
            status["skip_reason"] = "no_hour_row"
            status["ok"] = True
            _last_work_mode_sync = status
            return status

        timer_txt = str(row.get("timer_schedule") or "").strip()
        status["timer_schedule"] = timer_txt or None
        status["grid_export_kwh"] = round(plan_row_grid_export_kwh(row), 3)

        metrics = await sa_client.get_live_metrics(cfg)
        live_soc = float(metrics.get("battery_soc", 0.0))
        soc_full = live_soc >= SOC_FULL_PCT

        has_timer = hour_has_timer_schedule(rows, hour)
        charge_grid = normalize_action(row.get("action") or "") == ACTION_CHARGE_GRID
        timer_on_grid = has_timer and not charge_grid

        if not (timer_on_grid or soc_full):
            status["skipped"] = True
            status["skip_reason"] = "no_on_grid_trigger"
            status["ok"] = True
            log.info(
                "Work mode start skipped — hour %02d (timer=%s action=%s soc=%.1f%%)",
                hour,
                timer_txt or "—",
                row.get("action"),
                live_soc,
            )
            _last_work_mode_sync = status
            return status

        await _set_work_mode_if_needed(
            cfg,
            target_mode=sa_client.WORK_MODE_ON_GRID,
            status=status,
            timer_txt=timer_txt,
        )
        _last_work_mode_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_work_mode_sync = status
        log.exception("Work mode start job failed")
        return status


async def run_work_mode_limit_home() -> dict[str, Any]:
    """:00/:15/:30/:45 — Limit home load when discharge ended and live PV is zero."""
    global _last_work_mode_sync

    now = now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": "limit_home",
        "hour": hour,
        "timer_schedule": None,
        "grid_export_kwh": None,
        "production_kw": None,
        "discharge_end_hhmm": None,
        "limit_due": False,
        "work_mode_target": sa_client.WORK_MODE_LIMIT_HOME_LOAD,
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

        metrics = await sa_client.get_live_metrics(cfg)
        pv_kw = float(metrics.get("pv_power", 0.0))
        status["production_kw"] = round(pv_kw, 3)
        if pv_kw > LIVE_PV_ZERO_KW:
            status["skipped"] = True
            status["skip_reason"] = "production_not_zero"
            status["ok"] = True
            log.info(
                "Work mode limit_home skipped — hour %02d production=%.3f kW",
                hour,
                pv_kw,
            )
            _last_work_mode_sync = status
            return status

        rows = await _plan_rows(cfg)
        due = False
        due_end: str | None = None
        due_timer = ""
        due_row: dict[str, Any] | None = None
        for h in _hours_to_check_for_limit(hour):
            row = _hour_row(rows, h)
            if not row:
                continue
            timer_txt = str(row.get("timer_schedule") or "").strip()
            if not timer_txt:
                continue
            is_due, end_hhmm = timer_discharge_end_due(timer_txt, now)
            if is_due:
                due = True
                due_end = end_hhmm
                due_timer = timer_txt
                due_row = row
                break

        if not due or not due_row:
            status["skipped"] = True
            status["skip_reason"] = "discharge_not_ended"
            status["ok"] = True
            log.info("Work mode limit_home skipped — hour %02d discharge not ended yet", hour)
            _last_work_mode_sync = status
            return status

        status["limit_due"] = True
        status["discharge_end_hhmm"] = due_end
        status["timer_schedule"] = due_timer
        status["grid_export_kwh"] = round(plan_row_grid_export_kwh(due_row), 3)

        await _set_work_mode_if_needed(
            cfg,
            target_mode=sa_client.WORK_MODE_LIMIT_HOME_LOAD,
            status=status,
            timer_txt=due_timer,
        )
        _last_work_mode_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_work_mode_sync = status
        log.exception("Work mode limit_home job failed")
        return status
