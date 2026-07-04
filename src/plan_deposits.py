"""Monthly energy deposit pool (Energy Cost Total / Deposit column)."""

from __future__ import annotations

from datetime import date
from typing import Any

from .grid_config import BILLING_MODEL_VERSION
from .influxdb import now_warsaw
from .plan_cost import month_savings_pln
from .sqlite_store import (
    ensure_deposit_seed,
    load_all_deposits,
    load_month_history,
    reset_deposit_current_to_initial,
    save_all_deposits,
    save_month_history,
    sum_deposit_current,
    upsert_open_month_deposit,
)

DEPOSIT_START_MONTH = "2026-05"


def month_sort_key(month_id: str) -> tuple[int, int]:
    year_s, mon_s = month_id.split("-", 1)
    return int(year_s), int(mon_s)


def iter_months(from_month: str, to_month: str) -> list[str]:
    if to_month < from_month:
        return []
    sy, sm = month_sort_key(from_month)
    ey, em = month_sort_key(to_month)
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def open_month_id(today: date | None = None) -> str:
    today = today or now_warsaw().date()
    return today.strftime("%Y-%m")


def draw_import_from_deposits(
    import_energy_cost: float,
    deposits: dict[str, dict[str, float]],
    prior_month_ids: list[str],
) -> float:
    """Pay import tariff from oldest non-zero deposit_current; return uncovered import."""
    remaining = max(0.0, float(import_energy_cost))
    for month_id in prior_month_ids:
        if remaining <= 0.0005:
            break
        row = deposits.get(month_id)
        if not row:
            continue
        available = max(0.0, float(row["current"]))
        if available <= 0.0005:
            continue
        take = min(available, remaining)
        row["current"] = round(available - take, 4)
        remaining -= take
    return round(remaining, 4)


def compute_energy_deposit_total(
    export_revenue: float,
    import_energy_cost: float,
    deposits: dict[str, dict[str, float]],
    prior_month_ids: list[str],
) -> float:
    """Energy Cost Total (Deposit): export credit; import paid from pool, not netted."""
    export = float(export_revenue)
    uncovered = draw_import_from_deposits(import_energy_cost, deposits, prior_month_ids)
    if uncovered > 0.0005:
        return round(export - uncovered, 4)
    return round(export, 4)


def _recalc_totals_savings(totals: dict[str, Any]) -> None:
    totals["savings_pln"] = month_savings_pln(
        float(totals.get("baseline_cost") or 0.0),
        float(totals.get("baseline_service_fee") or 0.0),
        float(totals.get("energy_cost_total") or 0.0),
        float(totals.get("import_cost_total") or 0.0),
    )


def _empty_totals() -> dict[str, Any]:
    return {
        "export_revenue": 0.0,
        "import_energy_cost": 0.0,
        "energy_cost_total": 0.0,
        "import_cost_total": 0.0,
        "baseline_cost": 0.0,
        "baseline_service_fee": 0.0,
        "savings_pln": 0.0,
    }


def _month_has_billing_data(payload: dict[str, Any]) -> bool:
    if payload.get("rows"):
        return True
    totals = payload.get("totals") or {}
    for key in ("export_revenue", "import_energy_cost", "production", "grid_export"):
        if float(totals.get(key) or 0) > 0.0005:
            return True
    return False


def _payload_for_month(month_id: str, target_month: str, target_payload: dict[str, Any]) -> dict[str, Any]:
    if month_id == target_month:
        return target_payload
    cached = load_month_history(month_id)
    if cached is not None:
        return cached
    return {"month": month_id, "rows": [], "totals": _empty_totals()}


def run_deposit_cascade(
    target_month: str,
    target_payload: dict[str, Any],
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], float]:
    """Replay deposits from DEPOSIT_START_MONTH through target_month; mutate target_payload."""
    ensure_deposit_seed()
    if target_month < DEPOSIT_START_MONTH:
        totals = target_payload.setdefault("totals", {})
        export = float(totals.get("export_revenue") or 0.0)
        import_tariff = float(totals.get("import_energy_cost") or 0.0)
        totals["energy_cost_total"] = round(export - import_tariff, 4)
        _recalc_totals_savings(totals)
        return target_payload, sum_deposit_current()

    reset_deposit_current_to_initial()
    deposits = load_all_deposits()
    current_open = open_month_id(today)
    months = iter_months(DEPOSIT_START_MONTH, target_month)
    final_payload = target_payload

    for month_id in months:
        payload = _payload_for_month(month_id, target_month, target_payload)
        totals = payload.setdefault("totals", _empty_totals())
        export = float(totals.get("export_revenue") or 0.0)
        import_tariff = float(totals.get("import_energy_cost") or 0.0)
        prior_ids = [m for m in months if m < month_id]
        energy_total = compute_energy_deposit_total(
            export,
            import_tariff,
            deposits,
            prior_ids,
        )
        totals["energy_cost_total"] = energy_total
        _recalc_totals_savings(totals)

        if month_id == current_open:
            upsert_open_month_deposit(month_id, energy_total)
            deposits[month_id] = {
                "initial": energy_total,
                "current": energy_total,
            }

        payload["billing_model_version"] = BILLING_MODEL_VERSION
        if month_id == target_month or _month_has_billing_data(payload):
            save_month_history(month_id, payload)
        if month_id == target_month:
            final_payload = payload

    save_all_deposits(deposits)
    return final_payload, sum_deposit_current(deposits)
