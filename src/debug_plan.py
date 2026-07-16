"""Merge Energy arbitrage plan rows into inverter-debug day payloads."""

from __future__ import annotations

from typing import Any


def ea_row_to_smart_view(row: dict[str, Any]) -> dict[str, Any]:
    """Debug smart column view from an EA hourly row (no cost fields)."""
    return {
        "grid_used": row.get("grid_import"),
        "grid_export": row.get("grid_export"),
        "bat_charge": row.get("bat_charge"),
        "bat_discharge": row.get("bat_discharge"),
        "soc": row.get("soc"),
    }


def ea_rows_by_hour_for_date(
    ea_plan: dict[str, Any],
    date_str: str,
) -> dict[int, dict[str, Any]]:
    """Index EA history + plan rows by hour for a calendar date."""
    today_str = ea_plan.get("today_date")
    by_hour: dict[int, dict[str, Any]] = {}

    if date_str == today_str:
        for row in ea_plan.get("history_rows") or []:
            h = row.get("hour")
            if h is not None:
                by_hour[int(h)] = row

    for row in ea_plan.get("rows") or []:
        if str(row.get("plan_date") or "") != date_str:
            continue
        h = row.get("hour")
        if h is not None:
            by_hour[int(h)] = row

    return by_hour


def merge_ea_plan_into_debug_day(
    day: dict[str, Any],
    ea_plan: dict[str, Any],
    date_str: str,
) -> None:
    """Attach smart flows, action, and timer from EA plan onto debug sim rows."""
    ea_by_hour = ea_rows_by_hour_for_date(ea_plan, date_str)

    for row in day.get("rows") or []:
        if row.get("skipped"):
            continue
        h = row.get("hour")
        if h is None:
            continue
        ea = ea_by_hour.get(int(h))
        if ea is None:
            continue
        row["smart"] = ea_row_to_smart_view(ea)
        row["action"] = ea.get("action", "")
        row["timer_schedule"] = ea.get("timer_schedule", "")
        if ea.get("buy_price") is not None:
            row["buy_price"] = ea["buy_price"]
        if ea.get("rce_q15"):
            row["rce_q15"] = ea["rce_q15"]
