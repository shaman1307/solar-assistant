#!/usr/bin/env python3
"""Patch timer_schedule on a locked plan row in SQLite plan_latest."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "solar_smart.db"


def _patch_row_timer(
    plan: dict,
    *,
    plan_date: str,
    hour: int,
    old_timer: str | None,
    new_timer: str,
) -> int:
    changed = 0
    for key in ("rows", "history_rows"):
        for row in plan.get(key) or []:
            if row.get("start") == "TOTAL":
                continue
            if str(row.get("plan_date") or "") != plan_date:
                continue
            if int(row.get("hour", -1)) != hour:
                continue
            cur = str(row.get("timer_schedule") or "").strip()
            if old_timer is not None and cur != old_timer:
                continue
            row["timer_schedule"] = new_timer
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch plan_latest timer_schedule cell")
    parser.add_argument("--plan-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--hour", type=int, required=True, help="0-23")
    parser.add_argument("--new-timer", required=True, help="New Timer Schedule text")
    parser.add_argument(
        "--old-timer",
        default=None,
        help="Only patch when current value matches (safety check)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not DB.is_file():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT payload_json FROM plan_latest WHERE id = 1").fetchone()
    if not row:
        print("plan_latest empty", file=sys.stderr)
        return 1

    plan = json.loads(row["payload_json"])
    changed = _patch_row_timer(
        plan,
        plan_date=args.plan_date,
        hour=args.hour,
        old_timer=args.old_timer,
        new_timer=args.new_timer,
    )
    if changed == 0:
        print("No matching rows updated")
        return 1

    print(f"Patched {changed} row(s): h{args.hour:02d} -> {args.new_timer!r}")
    if args.dry_run:
        print("dry-run: not writing")
        return 0

    payload = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        UPDATE plan_latest
        SET payload_json = ?, updated_at = ?
        WHERE id = 1
        """,
        (payload, now),
    )
    conn.commit()
    conn.close()
    print("OK: plan_latest saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
