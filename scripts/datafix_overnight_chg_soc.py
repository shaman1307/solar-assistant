#!/usr/bin/env python3
"""Patch overnight grid-charge windows into the plan and unlock solid SOC.

Target date 2026-07-24 (~03:00–05:15 at ~5 kW, SOC 17→36%).
Write plan_overrides timer cells, refresh with unlock_plan_soc, and update
EA history/row Timer Schedule text for those hours.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Continuous Chg windows (one segment per hour) so each hour reaches target SOC.
DEFAULT_DATE = "2026-07-24"
DEFAULT_OVERRIDES: dict[int, str] = {
    3: "Chg 03:00-04:00 5kW cap36%",
    4: "Chg 04:00-05:00 5kW cap36%",
    5: "Chg 05:00-05:15 5kW cap36%",
}


def _patch_row_timers(plan: dict, date_str: str, overrides: dict[int, str]) -> int:
    changed = 0
    for key in ("rows", "history_rows"):
        for row in plan.get(key) or []:
            if row.get("start") == "TOTAL":
                continue
            if str(row.get("plan_date") or "") != date_str:
                continue
            hour = int(row.get("hour", -1))
            if hour not in overrides:
                continue
            new_timer = overrides[hour]
            if str(row.get("timer_schedule") or "").strip() == new_timer:
                continue
            row["timer_schedule"] = new_timer
            changed += 1
    return changed


async def _run(*, date_str: str, overrides: dict[int, str], dry_run: bool) -> int:
    from src.config import load_config, save_config
    from src.plan_timer_override import set_timer_schedule_override
    from src.plan_simulation import hourly_plan_refresh
    from src.sqlite_store import read_plan
    import json
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path

    cfg = load_config()
    for hour in sorted(overrides):
        set_timer_schedule_override(
            cfg,
            date_str,
            hour,
            overrides[hour],
            clear_later_hours=False,
        )
        print(f"override h{hour:02d}: {overrides[hour]}")

    if dry_run:
        print("dry-run: not saving config / not refreshing plan")
        return 0

    save_config(cfg)
    print("sa-config.yaml: plan_overrides.timer_schedule saved")

    plan = await hourly_plan_refresh(cfg, unlock_plan_soc=True)

    # write_plan() protects past hours — patch timer text in SQLite so EA
    # history cells show the Chg windows (same approach as datafix_plan_timer_schedule).
    db = Path(__file__).resolve().parents[1] / "data" / "solar_smart.db"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT payload_json FROM plan_latest WHERE id = 1").fetchone()
    if not row:
        print("plan_latest empty — skip EA timer patch", file=sys.stderr)
        conn.close()
    else:
        stored = json.loads(row[0])
        patched = _patch_row_timers(stored, date_str, overrides)
        if patched:
            payload = json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE plan_latest SET payload_json = ?, updated_at = ? WHERE id = 1",
                (payload, now),
            )
            conn.commit()
            print(f"EA rows: patched {patched} timer_schedule cell(s) in SQLite")
        else:
            print("EA rows: no timer_schedule cells needed patching")
        conn.close()

    soc = (plan.get("plan_soc_q15") or {}).get("today") or []
    samples = {
        "03:00": soc[12] if len(soc) > 12 else None,
        "04:00": soc[16] if len(soc) > 16 else None,
        "05:00": soc[20] if len(soc) > 20 else None,
        "06:00": soc[24] if len(soc) > 24 else None,
    }
    print(f"plan_soc_q15 samples: {samples}")
    print("OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=DEFAULT_DATE, help="YYYY-MM-DD (default: 2026-07-24)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(date_str=args.date, overrides=DEFAULT_OVERRIDES, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
