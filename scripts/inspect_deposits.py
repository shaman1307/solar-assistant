#!/usr/bin/env python3
"""Inspect deposits and month_history totals (run on Pi or locally)."""
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
    conn.row_factory = sqlite3.Row
    print("=== deposits ===")
    for row in conn.execute("SELECT * FROM deposits ORDER BY month_id"):
        print(dict(row))
    print("=== month_history months ===")
    for row in conn.execute("SELECT month FROM month_history ORDER BY month"):
        print(row["month"])
    print("=== month_history totals (2026-05..2026-07) ===")
    for row in conn.execute(
        "SELECT month, payload_json FROM month_history WHERE month >= '2026-05' AND month <= '2026-07' ORDER BY month",
    ):
        payload = json.loads(row["payload_json"])
        totals = payload.get("totals") or {}
        print(
            row["month"],
            "billing=",
            payload.get("billing_model_version"),
            "export=",
            totals.get("export_revenue"),
            "import_tariff=",
            totals.get("import_energy_cost"),
            "energy_total=",
            totals.get("energy_cost_total"),
        )
    print("=== meta ===")
    for row in conn.execute("SELECT * FROM meta"):
        print(dict(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
