"""Incremental Energy arbitrage plan merge — SQLite is the source of truth."""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Any

from .influxdb import now_warsaw
from .plan_cost import compute_plan_totals
from .plan_hourly_actuals import (
    Q15_PER_HOUR,
    _actual_q15_battery_grid,
    _actual_q15_slice_kwh,
    _clamp_soc_pct,
    _soc_kwh_after_battery_delta,
    apply_q15_physics_to_row,
    hour_start_soc_kwh,
    refresh_row_grid_cash,
)
from .simulation_config import plan_min_soc_kwh, plan_min_soc_pct
from .timer_plan import (
    ACTION_DISCHARGE_GRID,
    classify_action,
    clip_timer_schedule_not_before,
    normalize_action,
    quarter_start_minute,
)

log = logging.getLogger(__name__)


def last_completed_quarter_tick(now: datetime) -> tuple[int, int]:
    """Return (hour_offset, quarter) for the q15 slot that just ended.

    hour_offset 0 = current clock hour; -1 = previous hour (only at :00).
    """
    minute = now.minute
    if minute == 0:
        return -1, 3
    if minute < 15:
        return 0, -1
    if minute < 30:
        return 0, 0
    if minute < 45:
        return 0, 1
    return 0, 2


def _row_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row.get("plan_date") or ""), int(row.get("hour", -1))


def _find_row(rows: list[dict], plan_date: str, hour: int) -> dict[str, Any] | None:
    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or "") == plan_date and int(row.get("hour", -1)) == hour:
            return row
    return None


def _q15_slot_actual(slot: dict[str, Any] | None) -> bool:
    return bool(slot and slot.get("from_actual"))


def _ensure_q15_length(q15: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(q15)
    while len(out) < Q15_PER_HOUR:
        prev_soc = out[-1].get("soc", 0.0) if out else 0.0
        out.append({
            "quarter": len(out),
            "production": 0.0,
            "consumption": 0.0,
            "soc": prev_soc,
            "battery": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "from_actual": False,
        })
    return out[:Q15_PER_HOUR]


def _build_actual_q15_slot(
    hour: int,
    quarter: int,
    *,
    series_10min: dict[str, list[float | None]] | None,
    soc_start_kwh: float,
    cfg: dict,
) -> dict[str, Any]:
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    min_kwh = plan_min_soc_kwh(cfg)
    bat_delta, grid_import, grid_export = _actual_q15_battery_grid(
        series_10min, hour, quarter,
    )
    pv = _actual_q15_slice_kwh((series_10min or {}).get("pv"), hour, quarter)
    load = _actual_q15_slice_kwh((series_10min or {}).get("load"), hour, quarter)
    soc_kwh = _soc_kwh_after_battery_delta(
        soc_start_kwh,
        bat_delta,
        min_kwh=min_kwh,
        battery_cap=battery_cap,
    )
    return {
        "quarter": quarter,
        "production": round(pv, 4),
        "consumption": round(load, 4),
        "soc": _clamp_soc_pct((soc_kwh / battery_cap) * 100.0, min_soc_pct),
        "battery": bat_delta,
        "grid_import": grid_import,
        "grid_export": grid_export,
        "from_actual": True,
    }


def _apply_actual_quarter_if_needed(
    row: dict[str, Any],
    hour: int,
    quarter: int,
    *,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
) -> bool:
    """Write one actual q15 slot when not yet frozen. Returns True if row changed."""
    q15 = _ensure_q15_length(list(row.get("q15") or []))
    if _q15_slot_actual(q15[quarter]):
        return False

    min_soc_pct = plan_min_soc_pct(cfg)
    soc_start = hour_start_soc_kwh(today_hourly, hour, battery_cap, min_soc_pct)
    if soc_start is None:
        soc_start = (float(q15[0].get("soc") or 50) / 100.0) * battery_cap
    soc_kwh = soc_start
    for q in range(quarter):
        if _q15_slot_actual(q15[q]):
            soc_kwh = (float(q15[q].get("soc") or 0) / 100.0) * battery_cap
        else:
            bat = float(q15[q].get("battery") or 0)
            soc_kwh = _soc_kwh_after_battery_delta(
                soc_kwh,
                bat,
                min_kwh=plan_min_soc_kwh(cfg),
                battery_cap=battery_cap,
            )

    q15[quarter] = _build_actual_q15_slot(
        hour,
        quarter,
        series_10min=series_10min,
        soc_start_kwh=soc_kwh,
        cfg=cfg,
    )
    row["q15"] = q15
    apply_q15_physics_to_row(row, q15)
    refresh_row_grid_cash(row, cfg)
    return True


def _merge_current_hour_q15(
    row: dict[str, Any],
    *,
    now: datetime,
    hour: int,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
) -> None:
    """Apply the latest completed quarter from Influx; keep all other slots from SQLite.

    The current hour is excluded from the optimizer on :15/:30/:45 refreshes —
    non-actual quarters keep their :00 values unchanged.
    """
    hour_offset, tick_q = last_completed_quarter_tick(now)
    if hour_offset == 0 and tick_q >= 0:
        changed = _apply_actual_quarter_if_needed(
            row,
            hour,
            tick_q,
            series_10min=series_10min,
            today_hourly=today_hourly,
            cfg=cfg,
            battery_cap=battery_cap,
        )
        if not changed:
            return  # nothing new, nothing to recompute

    # Recompute hourly aggregates from q15 (timer/action/labels stay untouched).
    q15 = _ensure_q15_length(list(row.get("q15") or []))
    apply_q15_physics_to_row(row, q15)
    refresh_row_grid_cash(row, cfg)


def _lock_hour_labels(row: dict[str, Any], fresh_row: dict[str, Any]) -> None:
    if row.get("timer_schedule_manual"):
        row["hour_labels_locked"] = True
        return
    if not row.get("hour_labels_locked"):
        row["timer_schedule"] = fresh_row.get("timer_schedule", "")
        row["action"] = fresh_row.get("action", "")
        row["hour_labels_locked"] = True


def _copy_future_row(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Future plan hour — full row from optimizer (including timer/action)."""
    for key, val in src.items():
        dst[key] = copy.deepcopy(val)
    dst["hour_labels_locked"] = False


def _history_has(history: list[dict], plan_date: str, hour: int) -> bool:
    return _find_row(history, plan_date, hour) is not None


def merge_incremental_plan(
    existing: dict[str, Any],
    fresh: dict[str, Any],
    *,
    now: datetime | None = None,
    metrics: dict[str, Any] | None = None,
    cfg: dict,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply quarter tick merge: immutable past, incremental current, live future."""
    del rules
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    current_hour = now.hour
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    today_hourly = (metrics or {}).get("today_hourly")
    series_10min = (metrics or {}).get("series_10min")

    merged = copy.deepcopy(fresh)
    merged["history_rows"] = copy.deepcopy(existing.get("history_rows") or [])
    history = merged["history_rows"]
    fresh_by_key = {
        _row_key(r): r for r in (fresh.get("rows") or []) if r.get("start") != "TOTAL"
    }
    existing_by_key = {
        _row_key(r): r for r in (existing.get("rows") or []) if r.get("start") != "TOTAL"
    }

    hour_offset, tick_q = last_completed_quarter_tick(now)

    # :00 — patch last quarter of the hour that just ended, then move to history.
    if hour_offset == -1 and tick_q == 3 and current_hour > 0:
        prev_hour = current_hour - 1
        prev_key = (today_str, prev_hour)
        prev_row = existing_by_key.get(prev_key) or _find_row(history, today_str, prev_hour)
        if prev_row is not None:
            prev_copy = copy.deepcopy(prev_row)
            _apply_actual_quarter_if_needed(
                prev_copy,
                prev_hour,
                3,
                series_10min=series_10min,
                today_hourly=today_hourly,
                cfg=cfg,
                battery_cap=battery_cap,
            )
            prev_copy["history_hour"] = True
            if not _history_has(history, today_str, prev_hour):
                history.append(prev_copy)
            else:
                for i, hrow in enumerate(history):
                    if _row_key(hrow) == prev_key:
                        history[i] = prev_copy
                        break

    out_rows: list[dict[str, Any]] = []
    for key, fresh_row in sorted(fresh_by_key.items(), key=lambda x: (x[0][0], x[0][1])):
        plan_date, hour = key
        if plan_date < today_str:
            continue
        if plan_date == today_str and hour < current_hour:
            if not _history_has(history, plan_date, hour):
                old = existing_by_key.get(key)
                if old is not None:
                    hist_copy = copy.deepcopy(old)
                    hist_copy["history_hour"] = True
                    history.append(hist_copy)
            continue

        existing_row = existing_by_key.get(key)
        if existing_row is None:
            new_row = copy.deepcopy(fresh_row)
            if plan_date == today_str and hour == current_hour and now.minute == 0:
                _lock_hour_labels(new_row, fresh_row)
            out_rows.append(new_row)
            continue

        row = copy.deepcopy(existing_row)

        if plan_date == today_str and hour == current_hour:
            if now.minute == 0 and not row.get("hour_labels_locked"):
                _lock_hour_labels(row, fresh_row)
            if hour_offset == 0 and tick_q >= 0:
                _apply_actual_quarter_if_needed(
                    row,
                    hour,
                    tick_q,
                    series_10min=series_10min,
                    today_hourly=today_hourly,
                    cfg=cfg,
                    battery_cap=battery_cap,
                )
            _merge_current_hour_q15(
                row,
                now=now,
                hour=hour,
                series_10min=series_10min,
                today_hourly=today_hourly,
                cfg=cfg,
                battery_cap=battery_cap,
            )
            # Never keep Dis/Chg segments that already ended (mid-hour refresh).
            if not row.get("timer_schedule_manual"):
                earliest = quarter_start_minute(now)
                clipped = clip_timer_schedule_not_before(
                    str(row.get("timer_schedule") or ""), earliest, cfg=cfg,
                )
                row["timer_schedule"] = clipped
                if (
                    not clipped
                    and normalize_action(str(row.get("action") or "")) == ACTION_DISCHARGE_GRID
                ):
                    row["action"] = classify_action(
                        bat_charge=float(row.get("bat_charge") or 0),
                        bat_discharge=float(row.get("bat_discharge") or 0),
                        grid_import=float(row.get("grid_import") or 0),
                        grid_export=float(row.get("grid_export") or 0),
                        production=float(row.get("production") or 0),
                    )
            out_rows.append(row)
            continue

        # Future hours (today after current, and tomorrow): live optimizer row.
        _copy_future_row(row, fresh_row)
        out_rows.append(row)

    merged["history_rows"] = history
    merged["rows"] = out_rows
    merged["has_history_rows"] = bool(history)
    merged["computed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    merged["today_date"] = today_str
    merged["plan_from_hour"] = current_hour
    return merged


def plan_needs_full_rebuild(cached: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """True when no plan exists or calendar day changed (midnight full rebuild)."""
    if not cached:
        return True
    now = now or now_warsaw()
    return cached.get("today_date") != now.strftime("%Y-%m-%d")


def attach_immutable_history(
    result: dict[str, Any],
    existing: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> None:
    """Keep past hours from SQLite; full rebuild may only refresh current+future.

    Same calendar day: *history_rows* are copied from *existing* and never replaced
    by a fresh Influx rebuild. Hours still sitting in *existing.rows* that are now
    before the current clock hour are promoted into history (append-only).

    New day / empty SQLite: keep *result.history_rows* as a one-shot seed (meters
    with empty timers from the fresh sim). No preserve/fill of Timer Schedule.
    """
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    current_hour = now.hour

    if existing is not None and str(existing.get("today_date") or "") == today_str:
        history = copy.deepcopy(existing.get("history_rows") or [])
        for row in existing.get("rows") or []:
            if row.get("start") == "TOTAL":
                continue
            plan_date = str(row.get("plan_date") or "")
            try:
                hour = int(row.get("hour", -1))
            except (TypeError, ValueError):
                continue
            if plan_date != today_str or hour < 0 or hour >= current_hour:
                continue
            if _history_has(history, today_str, hour):
                continue
            hist_copy = copy.deepcopy(row)
            hist_copy["history_hour"] = True
            history.append(hist_copy)
    else:
        history = copy.deepcopy(result.get("history_rows") or [])

    history.sort(
        key=lambda r: (str(r.get("plan_date") or ""), int(r.get("hour", -1))),
    )

    live_rows: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if plan_date == today_str and hour < current_hour:
            continue
        live_rows.append(row)

    result["history_rows"] = history
    result["rows"] = live_rows
    result["has_history_rows"] = bool(history)
    result["today_date"] = today_str
    result["plan_from_hour"] = current_hour

    today_plan = [
        r for r in live_rows
        if str(r.get("plan_date") or "") == today_str
    ]
    result["totals"] = compute_plan_totals(history + today_plan)
