#!/usr/bin/env python3
"""Data-patch EA archive (and today's plan_latest history): fill unfrozen q15 from Influx.

Recovers missed :00 q3 freezes across plan_day_archive. Already-frozen quarters
stay untouched. Recomputes hour grid totals / cash and day totals.

Usage (on Pi):
  .venv/bin/python3 scripts/datafix_archive_q15_actuals.py
  .venv/bin/python3 scripts/datafix_archive_q15_actuals.py --dry-run
  .venv/bin/python3 scripts/datafix_archive_q15_actuals.py --day 2026-08-01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "solar_smart.db"


def _row_needs_q15_patch(row: dict[str, Any]) -> bool:
    from src.plan_cache_merge import _ensure_q15_length, _q15_slot_actual

    q15 = _ensure_q15_length(list(row.get("q15") or []))
    if not q15:
        return True
    return not all(_q15_slot_actual(s) for s in q15)


def _patch_day_rows(
    rows: list[dict[str, Any]],
    *,
    day: str,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
) -> dict[str, int]:
    """Finalize unfrozen q15 on *rows* for *day*. Returns counters."""
    from src.plan_cache_merge import Q15_PER_HOUR, _finalize_hour_actual_quarters
    from src.plan_hourly_actuals import apply_q15_physics_to_row, refresh_row_grid_cash

    stats = {"hours_seen": 0, "hours_patched": 0, "q3_filled": 0, "slots_filled": 0}
    if not series_10min:
        return stats

    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or day) != day:
            continue
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if hour < 0 or hour > 23:
            continue
        stats["hours_seen"] += 1
        if not _row_needs_q15_patch(row):
            continue

        before = list(row.get("q15") or [])
        before_fa = [bool(s.get("from_actual")) for s in before]
        q3_was = bool(before[3].get("from_actual")) if len(before) > 3 else False

        changed = _finalize_hour_actual_quarters(
            row,
            hour,
            through_quarter=Q15_PER_HOUR - 1,
            series_10min=series_10min,
            today_hourly=today_hourly,
            cfg=cfg,
            battery_cap=battery_cap,
        )
        if not changed:
            continue

        after = list(row.get("q15") or [])
        after_fa = [bool(s.get("from_actual")) for s in after]
        filled = sum(
            1
            for q in range(min(len(before_fa), len(after_fa)))
            if (not before_fa[q]) and after_fa[q]
        )
        stats["hours_patched"] += 1
        stats["slots_filled"] += filled
        if (not q3_was) and len(after) > 3 and after[3].get("from_actual"):
            stats["q3_filled"] += 1

        apply_q15_physics_to_row(row, after)
        refresh_row_grid_cash(row, cfg)

    return stats


def _recompute_totals(payload: dict[str, Any]) -> None:
    from src.plan_cost import compute_plan_totals

    day_rows = list(payload.get("history_rows") or []) + list(payload.get("rows") or [])
    day_rows = [r for r in day_rows if r.get("start") != "TOTAL"]
    payload["totals"] = compute_plan_totals(day_rows)


def _write_plan_latest_direct(plan: dict[str, Any]) -> None:
    """Persist plan_latest without write_plan guard (archive-style history patch)."""
    payload = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            """
            INSERT INTO plan_latest(id, payload_json, updated_at)
            VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (payload, now),
        )
        conn.commit()
    finally:
        conn.close()


async def _patch_archive_day(
    day: str,
    *,
    cfg: dict,
    battery_cap: float,
    dry_run: bool,
) -> dict[str, Any]:
    from src import influxdb as influxdb_mod
    from src.sqlite_store import load_plan_day_archive, save_plan_day_archive

    out: dict[str, Any] = {"day": day, "kind": "archive", "ok": False}
    archived = load_plan_day_archive(day)
    if not archived:
        out["error"] = "missing archive"
        return out

    accruals = await influxdb_mod.get_accruals_for_date(day)
    series = accruals.get("series_10min")
    hourly = accruals.get("hourly")
    if not series:
        out["error"] = "no series_10min"
        return out

    rows = list(archived.get("history_rows") or [])
    if not rows and archived.get("rows"):
        rows = list(archived.get("rows") or [])
        archived["history_rows"] = rows
        archived["rows"] = []

    stats = _patch_day_rows(
        rows,
        day=day,
        series_10min=series,
        today_hourly=hourly if isinstance(hourly, dict) else None,
        cfg=cfg,
        battery_cap=battery_cap,
    )
    out.update(stats)
    out["ok"] = True

    if stats["hours_patched"] == 0:
        out["saved"] = False
        return out

    _recompute_totals(archived)
    if dry_run:
        out["saved"] = False
        out["dry_run"] = True
        return out

    save_plan_day_archive(day, archived)
    out["saved"] = True
    return out


async def _patch_plan_latest_today(
    *,
    cfg: dict,
    battery_cap: float,
    dry_run: bool,
) -> dict[str, Any] | None:
    from src import influxdb as influxdb_mod
    from src.influxdb import now_warsaw
    from src.sqlite_store import read_plan

    plan = read_plan()
    if not plan:
        return None
    today = now_warsaw().strftime("%Y-%m-%d")
    if str(plan.get("today_date") or "") != today:
        return {
            "day": str(plan.get("today_date") or ""),
            "kind": "plan_latest",
            "ok": False,
            "error": f"plan today_date != {today}",
        }

    out: dict[str, Any] = {"day": today, "kind": "plan_latest", "ok": False}
    accruals = await influxdb_mod.get_accruals_for_date(today)
    series = accruals.get("series_10min")
    hourly = accruals.get("hourly")
    if not series:
        out["error"] = "no series_10min"
        return out

    hist = list(plan.get("history_rows") or [])
    stats = _patch_day_rows(
        hist,
        day=today,
        series_10min=series,
        today_hourly=hourly if isinstance(hourly, dict) else None,
        cfg=cfg,
        battery_cap=battery_cap,
    )
    out.update(stats)
    out["ok"] = True
    if stats["hours_patched"] == 0:
        out["saved"] = False
        return out

    plan["history_rows"] = hist
    _recompute_totals(plan)
    if dry_run:
        out["saved"] = False
        out["dry_run"] = True
        return out

    _write_plan_latest_direct(plan)
    out["saved"] = True
    return out


async def _run(*, days: list[str] | None, dry_run: bool, include_today: bool) -> int:
    from src.config import load_config
    from src.simulation_config import merge_simulation_defaults
    from src.sqlite_store import list_plan_day_archives

    cfg = merge_simulation_defaults(load_config())
    battery_cap = float(cfg["battery"]["capacity_kwh"])

    if days:
        archive_days = list(days)
    else:
        archive_days = list_plan_day_archives(limit=365)

    print(
        f"EA q15 archive datafix — {len(archive_days)} archive day(s)"
        f"{' (dry-run)' if dry_run else ''}"
    )

    totals = {
        "days": 0,
        "hours_patched": 0,
        "q3_filled": 0,
        "slots_filled": 0,
        "saved": 0,
        "errors": 0,
    }

    for day in sorted(archive_days):
        result = await _patch_archive_day(
            day, cfg=cfg, battery_cap=battery_cap, dry_run=dry_run,
        )
        totals["days"] += 1
        if not result.get("ok"):
            totals["errors"] += 1
            print(f"  {day}: ERROR {result.get('error')}")
            continue
        hp = int(result.get("hours_patched") or 0)
        q3 = int(result.get("q3_filled") or 0)
        slots = int(result.get("slots_filled") or 0)
        totals["hours_patched"] += hp
        totals["q3_filled"] += q3
        totals["slots_filled"] += slots
        if result.get("saved"):
            totals["saved"] += 1
        print(
            f"  {day}: hours={result.get('hours_seen')} patched={hp} "
            f"q3_filled={q3} slots={slots} saved={result.get('saved')}"
        )

    if include_today:
        today_result = await _patch_plan_latest_today(
            cfg=cfg, battery_cap=battery_cap, dry_run=dry_run,
        )
        if today_result is None:
            print("  plan_latest: empty")
        elif not today_result.get("ok"):
            totals["errors"] += 1
            print(
                f"  plan_latest {today_result.get('day')}: "
                f"ERROR {today_result.get('error')}"
            )
        else:
            hp = int(today_result.get("hours_patched") or 0)
            q3 = int(today_result.get("q3_filled") or 0)
            slots = int(today_result.get("slots_filled") or 0)
            totals["hours_patched"] += hp
            totals["q3_filled"] += q3
            totals["slots_filled"] += slots
            if today_result.get("saved"):
                totals["saved"] += 1
            print(
                f"  plan_latest {today_result.get('day')}: "
                f"hours={today_result.get('hours_seen')} patched={hp} "
                f"q3_filled={q3} slots={slots} saved={today_result.get('saved')}"
            )

    print(
        "done — "
        f"days={totals['days']} hours_patched={totals['hours_patched']} "
        f"q3_filled={totals['q3_filled']} slots_filled={totals['slots_filled']} "
        f"saved={totals['saved']} errors={totals['errors']}"
    )
    return 1 if totals["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--day",
        action="append",
        dest="days",
        help="Patch one archive day (YYYY-MM-DD); repeatable. Default: all archives.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute patches but do not write SQLite.",
    )
    parser.add_argument(
        "--skip-today",
        action="store_true",
        help="Do not patch plan_latest history for today.",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            days=args.days,
            dry_run=args.dry_run,
            include_today=not args.skip_today,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
