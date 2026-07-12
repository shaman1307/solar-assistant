#!/usr/bin/env python3
"""Inspect plan_latest slot range and tomorrow forecast in SQLite."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "solar_smart.db"


def main() -> int:
    if not DB.is_file():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT payload_json, updated_at FROM plan_latest WHERE id = 1",
    ).fetchone()
    if not row:
        print("plan_latest: EMPTY")
        return 0

    plan = json.loads(row[0])
    print("sqlite updated_at:", row[1])
    print("computed_at:", plan.get("computed_at"))
    print("today_date:", plan.get("today_date"))
    print("plan_from_hour:", plan.get("plan_from_hour"))

    rows = [r for r in (plan.get("rows") or []) if r.get("start") != "TOTAL"]
    tom_rows = plan.get("tomorrow_remainder_rows") or []
    all_rows = rows + tom_rows

    if not all_rows:
        print("rows: EMPTY")
    else:

        def key(r: dict) -> tuple[str, int]:
            return (str(r.get("plan_date") or ""), int(r.get("hour", -1)))

        first = min(all_rows, key=key)
        last = max(all_rows, key=key)
        dates: dict[str, int] = {}
        for r in all_rows:
            d = str(r.get("plan_date") or "")
            dates[d] = dates.get(d, 0) + 1

        print("first slot:", first.get("plan_date"), f"h{int(first['hour']):02d}")
        print("last slot:", last.get("plan_date"), f"h{int(last['hour']):02d}")
        print("slot counts by date:", dict(sorted(dates.items())))
        print("rows (today+future):", len(rows))
        print("tomorrow_remainder_rows:", len(tom_rows))

    fc = plan.get("forecast") or {}
    tom_fc = fc.get("tomorrow") or {}
    today_fc = fc.get("today") or {}
    pv_t = tom_fc.get("pv") or []
    load_t = tom_fc.get("load") or []
    print()
    print("forecast in SQLite:")
    print("  today.pv len:", len(today_fc.get("pv") or []))
    print("  today.load len:", len(today_fc.get("load") or []))
    print("  tomorrow.pv len:", len(pv_t), "sum:", round(sum(float(x) for x in pv_t), 3) if pv_t else 0)
    print("  tomorrow.load len:", len(load_t), "sum:", round(sum(float(x) for x in load_t), 3) if load_t else 0)
    if pv_t:
        print("  tomorrow pv h0-3:", [round(float(pv_t[i]), 3) for i in range(min(4, len(pv_t)))])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
