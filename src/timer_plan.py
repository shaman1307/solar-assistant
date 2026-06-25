"""
Action labels and timer schedule compression (SolarAssistant timer slots).

Actions:
  - Idle - Grid Usage for Load
  - Idle - PV to Load. On-Grid
  - PV to Load. On-Grid
  - Grid Usage for Load
  - Charging from Grid
  - Charging from PV
  - Discharging to Load
  - Discharging to Grid and Load
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .simulation_config import get_simulation_params, plan_min_soc_pct

ACTION_IDLE_GRID = "Idle - Grid Usage for Load"
ACTION_IDLE_PV = "Idle - PV to Load. On-Grid"
ACTION_TIE_PV = "PV to Load. On-Grid"
ACTION_TIE_GRID = "Grid Usage for Load"
ACTION_CHARGE_GRID = "Charging from Grid"
ACTION_CHARGE_SOLAR = "Charging from PV"
ACTION_DISCHARGE_LOAD = "Discharging to Load"
ACTION_DISCHARGE_GRID = "Discharging to Grid and Load"
# Minimum hourly grid export (kWh) to label "Discharging to Grid and Load" vs "Discharging to Load".
GRID_EXPORT_ACTION_MIN_KWH = 0.5

# SOC at or above this (%) allows "PV export to grid" (full battery spill).
_PV_EXPORT_FULL_SOC_PCT = 99.9

ACTION_CYCLE = [
    ACTION_DISCHARGE_LOAD,
    ACTION_IDLE_GRID,
    ACTION_CHARGE_SOLAR,
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
]

_LEGACY_ACTION_MAP = {
    "From grid": ACTION_IDLE_GRID,
    "Charge from grid": ACTION_CHARGE_GRID,
    "Charge from solar": ACTION_CHARGE_SOLAR,
    "Discharge to grid": ACTION_DISCHARGE_GRID,
    "Discharge to load": ACTION_DISCHARGE_LOAD,
    "Discharging to Grid and Installation": ACTION_DISCHARGE_GRID,
    "PV export": ACTION_IDLE_PV,
    "PV export to grid": ACTION_IDLE_PV,
}


def normalize_action(action: str) -> str:
    return _LEGACY_ACTION_MAP.get(action, action)


def _pv_export_to_grid(
    *,
    pv: float | None,
    grid_export: float,
    soc_pct: float | None,
    epsilon: float = 0.001,
) -> bool:
    """PV spill to grid: production this hour and battery full (SOC ~100%)."""
    pv_val = float(pv) if pv is not None else 0.0
    if pv_val <= epsilon or grid_export <= epsilon:
        return False
    if soc_pct is None:
        return False
    return float(soc_pct) >= _PV_EXPORT_FULL_SOC_PCT


def classify_action(
    *,
    bat_charge: float = 0.0,
    bat_discharge: float = 0.0,
    grid_import: float = 0.0,
    grid_export: float = 0.0,
    production: float | None = None,
    pv: float | None = None,
) -> str:
    """Derive display action from gross battery charge/discharge and grid/PV context."""
    charge = max(0.0, float(bat_charge or 0))
    discharge = max(0.0, float(bat_discharge or 0))
    g_imp = max(0.0, float(grid_import or 0))
    g_exp = max(0.0, float(grid_export or 0))
    prod = float(production if production is not None else pv or 0)

    if charge > discharge:
        return ACTION_CHARGE_GRID if g_imp > prod else ACTION_CHARGE_SOLAR
    if discharge > charge:
        return (
            ACTION_DISCHARGE_GRID
            if g_exp > GRID_EXPORT_ACTION_MIN_KWH
            else ACTION_DISCHARGE_LOAD
        )
    if charge == 0 and discharge == 0:
        return ACTION_IDLE_PV if prod > 0 else ACTION_IDLE_GRID
    # Tie: equal gross charge and discharge in the same hour
    return ACTION_TIE_PV if prod > 0 else ACTION_TIE_GRID


def _slot_action_energy(slot: dict[str, Any], action: str) -> float:
    """kWh attributed to a 15-min slot action."""
    imp = float(slot.get("grid_import") or 0)
    exp = float(slot.get("grid_export") or 0)
    bd = float(slot.get("battery_delta") or 0)
    if action == ACTION_DISCHARGE_GRID:
        return max(min(exp, max(0.0, -bd)), 0.0)
    if action == ACTION_DISCHARGE_LOAD:
        return max(0.0, -bd)
    if action == ACTION_CHARGE_GRID:
        return max(imp, max(0.0, bd))
    if action == ACTION_CHARGE_SOLAR:
        return max(0.0, bd)
    if action == ACTION_IDLE_GRID:
        return imp
    return 0.0


def _battery_discharge_split(
    slot: dict[str, Any],
    epsilon: float,
) -> tuple[float, float]:
    """Split q15 battery discharge into load vs grid export (kWh)."""
    bd = float(slot.get("battery_delta") or 0)
    if bd >= -epsilon:
        return 0.0, 0.0
    dis = abs(bd)
    batt_exp_raw = slot.get("battery_export_kwh")
    if batt_exp_raw is not None:
        batt_exp = max(0.0, min(float(batt_exp_raw), dis))
    else:
        exp = float(slot.get("grid_export") or 0)
        batt_exp = min(exp, dis) if exp > epsilon else 0.0
    to_load = max(0.0, dis - batt_exp)
    return to_load, batt_exp


def _hour_action_energies(
    slots: list[dict[str, Any]],
    epsilon: float,
) -> dict[str, float]:
    """Dominant action energies aligned with debug Smart flow columns (kWh)."""
    energy: dict[str, float] = defaultdict(float)
    grid_used = 0.0
    grid_export = 0.0
    bat_charge = 0.0
    bat_discharge = 0.0
    batt_export = 0.0

    for slot in slots:
        grid_used += float(slot.get("grid_import") or 0)
        grid_export += float(slot.get("grid_export") or 0)
        bd = float(slot.get("battery_delta") or 0)
        if bd > 0:
            bat_charge += bd
        elif bd < 0:
            bat_discharge += abs(bd)
        _, to_grid = _battery_discharge_split(slot, epsilon)
        batt_export += to_grid

    if batt_export > epsilon:
        energy[ACTION_DISCHARGE_GRID] = max(grid_export, batt_export)
        to_load = max(0.0, bat_discharge - batt_export)
        if to_load > epsilon:
            energy[ACTION_DISCHARGE_LOAD] = to_load
    else:
        if grid_export > epsilon:
            energy[ACTION_DISCHARGE_GRID] = grid_export
        to_load = max(0.0, bat_discharge - grid_export)
        if to_load > epsilon:
            energy[ACTION_DISCHARGE_LOAD] = to_load

    if bat_charge > epsilon:
        if grid_used > epsilon:
            energy[ACTION_CHARGE_GRID] = bat_charge + grid_used
        else:
            energy[ACTION_CHARGE_SOLAR] = bat_charge
    elif grid_used > epsilon:
        energy[ACTION_IDLE_GRID] = grid_used

    return energy


def _battery_export_energy(slots: list[dict[str, Any]], epsilon: float) -> float:
    return sum(_battery_discharge_split(slot, epsilon)[1] for slot in slots)


def _top_hour_action(energy: dict[str, float], epsilon: float) -> str:
    ranked = sorted(
        ((act, kwh) for act, kwh in energy.items() if kwh > epsilon),
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked:
        if energy.get(ACTION_CHARGE_SOLAR, 0) > 0:
            return ACTION_CHARGE_SOLAR
        return ACTION_IDLE_GRID
    return ranked[0][0]


def _hour_export_debug_action(
    hour: int,
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    epsilon: float = 0.001,
) -> str | None:
    """Legacy export label helper; prefer classify_action via summarize_hour_actions_debug."""
    del hour, cfg
    pv_total = sum(float(s.get("pv") or 0) for s in slots)
    grid_export = sum(float(s.get("grid_export") or 0) for s in slots)
    if pv_total > epsilon and grid_export > pv_total + epsilon:
        batt_exp = _battery_export_energy(slots, epsilon)
        bat_discharge = sum(
            max(0.0, -float(s.get("battery_delta") or 0)) for s in slots
        )
        soc_pct = float(slots[-1]["soc_pct"]) if slots and slots[-1].get("soc_pct") is not None else None
        if batt_exp > epsilon and batt_exp >= bat_discharge - epsilon:
            return ACTION_DISCHARGE_GRID
        if _pv_export_to_grid(
            pv=pv_total, grid_export=grid_export, soc_pct=soc_pct, epsilon=epsilon,
        ):
            return ACTION_IDLE_PV
        return ACTION_DISCHARGE_GRID
    batt_exp = _battery_export_energy(slots, epsilon)
    if batt_exp > epsilon:
        return ACTION_DISCHARGE_GRID
    if grid_export > epsilon and pv_total <= epsilon:
        return ACTION_DISCHARGE_GRID
    return None


def summarize_hour_actions(
    slots: list[dict[str, Any]],
    *,
    epsilon: float = 0.001,
) -> str:
    """Dominant hour action by summed q15 energy (single label)."""
    return _top_hour_action(_hour_action_energies(slots, epsilon), epsilon)


def summarize_hour_actions_debug(
    slots: list[dict[str, Any]],
    hour: int,
    cfg: dict,
    *,
    epsilon: float = 0.001,
) -> str:
    """Debug/PROD hourly action — same classify rules as Influx history rows."""
    del hour, cfg, epsilon
    pv_total = sum(float(s.get("pv") or 0) for s in slots)
    grid_import = sum(float(s.get("grid_import") or 0) for s in slots)
    grid_export = sum(float(s.get("grid_export") or 0) for s in slots)
    bat_charge = sum(max(0.0, float(s.get("battery_delta") or 0)) for s in slots)
    bat_discharge = sum(max(0.0, -float(s.get("battery_delta") or 0)) for s in slots)
    return classify_action(
        bat_charge=bat_charge,
        bat_discharge=bat_discharge,
        grid_import=grid_import,
        grid_export=grid_export,
        production=pv_total,
    )


def _hour_q15_timer_energy(
    slots: list[dict[str, Any]],
    target_action: str,
    epsilon: float,
    *,
    hour_action: str | None = None,
) -> tuple[float, list[int]]:
    """Total kWh and active q15 indices (0..3) in the hour for one timer kind."""
    total = 0.0
    active: list[int] = []
    hour_act = normalize_action(hour_action or "")
    for qi, slot in enumerate(slots):
        if target_action == ACTION_DISCHARGE_GRID:
            _, to_grid = _battery_discharge_split(slot, epsilon)
            if to_grid <= epsilon:
                continue
            total += to_grid
        elif target_action == ACTION_CHARGE_GRID:
            act = normalize_action(slot.get("action") or "")
            imp = float(slot.get("grid_import") or 0)
            if act == ACTION_CHARGE_GRID:
                total += _slot_action_energy(slot, ACTION_CHARGE_GRID)
                active.append(qi)
            elif hour_act == ACTION_CHARGE_GRID and imp > epsilon:
                # Hour totals say grid charge, but per-q15 classify may be PV (g_imp <= pv).
                total += _slot_action_energy(slot, ACTION_CHARGE_GRID)
                active.append(qi)
        else:
            continue
        if target_action == ACTION_DISCHARGE_GRID:
            active.append(qi)
    return total, active


def _charge_timer_cap_pct(slots: list[dict[str, Any]], cfg: dict) -> int:
    """SA charge stop target: reserve SOC needed until PV carries load again."""
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc = int(plan_min_soc_pct(cfg))
    reserve_kwh = next(
        (float(s["reserve_kwh"]) for s in slots if s.get("reserve_kwh") is not None),
        None,
    )
    if reserve_kwh is not None and battery_cap > 0:
        pct = reserve_kwh / battery_cap * 100.0
        return int(round(min(100.0, max(float(min_soc), pct))))
    if slots:
        return int(round(float(slots[-1].get("soc_pct", min_soc))))
    return min_soc


def _hour_timer_segment(
    hour: int,
    slots: list[dict[str, Any]],
    target_action: str,
    cfg: dict,
    *,
    epsilon: float = 0.001,
    hour_action: str | None = None,
) -> str | None:
    """One SA timer line for this clock hour from q15 energy (>=30 min, power = kWh / duration)."""
    total_kwh, active_q = _hour_q15_timer_energy(
        slots, target_action, epsilon, hour_action=hour_action,
    )
    if not active_q or total_kwh <= epsilon:
        return None

    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    min_soc = int(plan_min_soc_pct(cfg))
    hour_start = hour * 60

    first_q = min(active_q)
    last_q = max(active_q)
    natural_from = hour_start + first_q * 15
    to_min = hour_start + (last_q + 1) * 15
    from_min = natural_from
    if to_min - from_min < MIN_TIMER_BLOCK_MINUTES:
        from_min = max(to_min - MIN_TIMER_BLOCK_MINUTES, hour_start)
    if to_min - from_min < MIN_TIMER_BLOCK_MINUTES:
        to_min = min(hour_start + 60, from_min + MIN_TIMER_BLOCK_MINUTES)
    if to_min - from_min < MIN_TIMER_BLOCK_MINUTES:
        return None

    duration_h = (to_min - from_min) / 60.0
    if duration_h <= 0:
        return None
    power_kw = round(min(total_kwh / duration_h, ac_kw), 2)
    if power_kw <= 0:
        return None

    prefix = "Chg" if target_action == ACTION_CHARGE_GRID else "Dis"
    if target_action == ACTION_CHARGE_GRID:
        cap = _charge_timer_cap_pct(slots, cfg)
    else:
        cap = min_soc
    return f"{prefix} {_min_to_hhmm(from_min)}-{_min_to_hhmm(to_min)} {power_kw}kW cap{cap}%"


def _hour_slot_totals(slots: list[dict[str, Any]]) -> dict[str, float]:
    """Hourly sums from q15 slots for action / timer display."""
    return {
        "bat_charge": sum(max(0.0, float(s.get("battery_delta") or 0)) for s in slots),
        "bat_discharge": sum(max(0.0, -float(s.get("battery_delta") or 0)) for s in slots),
        "grid_import": sum(float(s.get("grid_import") or 0) for s in slots),
        "grid_export": sum(float(s.get("grid_export") or 0) for s in slots),
        "production": sum(float(s.get("pv") or 0) for s in slots),
    }


def _fallback_charge_grid_timer(
    hour: int,
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    epsilon: float = 0.001,
) -> str:
    """Full clock-hour charge slot when q15 clip cannot reach SA minimum duration."""
    totals = _hour_slot_totals(slots)
    energy = totals["grid_import"] + totals["bat_charge"]
    if energy <= epsilon:
        energy = max(totals["grid_import"], totals["bat_charge"], epsilon)
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    hour_start = hour * 60
    power_kw = round(min(energy, ac_kw), 2)
    if power_kw <= 0:
        power_kw = round(min(ac_kw, 1.0), 2)
    cap = _charge_timer_cap_pct(slots, cfg) if slots else 80
    return (
        f"Chg {_min_to_hhmm(hour_start)}-{_min_to_hhmm(hour_start + 60)} "
        f"{power_kw}kW cap{cap}%"
    )


def build_hour_timer_schedule(
    hour: int,
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    action: str | None = None,
    grid_export: float | None = None,
    epsilon: float = 0.001,
) -> str:
    """Per-hour SA timer — only when *action* (row label) warrants grid charge/discharge."""
    totals = _hour_slot_totals(slots)
    act = normalize_action(action if action is not None else classify_action(**totals))
    g_exp = float(grid_export if grid_export is not None else totals["grid_export"])
    if act == ACTION_CHARGE_GRID:
        seg = _hour_timer_segment(
            hour, slots, ACTION_CHARGE_GRID, cfg,
            epsilon=epsilon, hour_action=act,
        )
        return seg or _fallback_charge_grid_timer(hour, slots, cfg, epsilon=epsilon)
    if act == ACTION_DISCHARGE_GRID and g_exp > 0:
        seg = _hour_timer_segment(
            hour, slots, ACTION_DISCHARGE_GRID, cfg,
            epsilon=epsilon, hour_action=act,
        )
        return seg or ""
    return ""


def _hour_from_row(row: dict) -> int:
    return int(row["start"].split(" ")[1].split(":")[0])


def _inactive_slot(slot_n: int, kind: str, template: dict | None) -> dict[str, Any]:
    base = template or {}
    return {
        "slot": slot_n,
        "from": "00:00",
        "to": "00:00",
        "capacity_pct": base.get("capacity_pct", 15 if kind == "discharge" else 80),
        "voltage_v": base.get("voltage_v", 57.6 if kind == "charge" else 42.0),
        "power_kw": base.get("power_kw", 0.0),
        **({"grid": base.get("grid", True), "generator": base.get("generator", False)} if kind == "charge" else {}),
    }


def _merge_blocks(rows: list[dict], target_action: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        action = normalize_action(row.get("action", ""))
        if action != target_action:
            if current:
                blocks.append(current)
                current = None
            continue

        hour = _hour_from_row(row)
        if target_action == ACTION_CHARGE_GRID:
            power_kw = max(
                float(row.get("battery", 0) or 0) + float(row.get("grid_import", row.get("buy", 0)) or 0),
                1.0,
            )
        else:
            power_kw = max(float(row.get("grid_export", row.get("feed_in", 0)) or 0), 1.0)

        if current and hour == current["_last_hour"] + 1:
            current["to_hour"] = hour + 1
            current["_last_hour"] = hour
            current["power_kw"] = max(current["power_kw"], power_kw)
            current["capacity_pct"] = round(float(row.get("soc", current["capacity_pct"])), 0)
        else:
            if current:
                blocks.append(current)
            current = {
                "from_hour": hour,
                "to_hour": hour + 1,
                "_last_hour": hour,
                "power_kw": power_kw,
                "capacity_pct": round(float(row.get("soc", 80)), 0),
            }

    if current:
        blocks.append(current)
    return blocks


def _minute_of_day_from_start(row: dict) -> int:
    parts = row["start"].split(" ")[1].split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _merge_blocks_q15(rows: list[dict], target_action: str) -> list[dict[str, Any]]:
    """Merge consecutive 15-minute rows into timer blocks (HH:MM boundaries)."""
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        action = normalize_action(row.get("action", ""))
        if action != target_action:
            if current:
                blocks.append(current)
                current = None
            continue

        slot_min = _minute_of_day_from_start(row)
        if target_action == ACTION_CHARGE_GRID:
            import_kwh = float(row.get("battery", 0) or 0) + float(
                row.get("grid_import", row.get("buy", 0)) or 0
            )
            power_kw = max(import_kwh * 4.0, 1.0)
        else:
            export_kwh = float(row.get("grid_export", row.get("feed_in", 0)) or 0)
            power_kw = max(export_kwh * 4.0, 1.0)

        if current and slot_min == current["_last_end_min"]:
            current["to_min"] = slot_min + 15
            current["_last_end_min"] = slot_min + 15
            current["power_kw"] = max(current["power_kw"], power_kw)
            current["capacity_pct"] = round(float(row.get("soc", current["capacity_pct"])), 0)
        else:
            if current:
                blocks.append(current)
            current = {
                "from_min": slot_min,
                "to_min": slot_min + 15,
                "_last_end_min": slot_min + 15,
                "power_kw": power_kw,
                "capacity_pct": round(float(row.get("soc", 80)), 0),
            }

    if current:
        blocks.append(current)
    return blocks


MIN_TIMER_BLOCK_MINUTES = 30


def _filter_min_duration_blocks(
    blocks: list[dict[str, Any]],
    min_minutes: int = MIN_TIMER_BLOCK_MINUTES,
) -> list[dict[str, Any]]:
    """Drop SA timer blocks shorter than min_minutes (after q15 merge)."""
    return [b for b in blocks if (b["to_min"] - b["from_min"]) >= min_minutes]


def _min_to_hhmm(total_min: int) -> str:
    total_min %= 24 * 60
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _blocks_q15_to_slots(
    blocks: list[dict[str, Any]],
    kind: str,
    templates: list[dict[str, Any]],
    cfg: dict,
) -> list[dict[str, Any]]:
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    min_soc = plan_min_soc_pct(cfg)
    slots: list[dict[str, Any]] = []

    for i in range(3):
        tpl = templates[i] if i < len(templates) else {}
        if i >= len(blocks):
            slots.append(_inactive_slot(i + 1, kind, tpl))
            continue

        blk = blocks[i]
        slot: dict[str, Any] = {
            "slot": i + 1,
            "from": _min_to_hhmm(blk["from_min"]),
            "to": _min_to_hhmm(blk["to_min"]),
            "capacity_pct": int(blk["capacity_pct"]) if kind == "charge" else int(min_soc),
            "voltage_v": float(tpl.get("voltage_v", 57.6 if kind == "charge" else 42.0)),
            "power_kw": round(min(float(blk["power_kw"]), ac_kw), 2),
        }
        if kind == "charge":
            slot["grid"] = True
            slot["generator"] = False
        slots.append(slot)

    return slots


def _extend_blocks_to_min_duration(
    blocks: list[dict[str, Any]],
    rows: list[dict],
    target_action: str,
    min_minutes: int = MIN_TIMER_BLOCK_MINUTES,
) -> list[dict[str, Any]]:
    """Extend short blocks across following q15 slots with the same action."""
    actions_by_min: dict[int, str] = {}
    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        actions_by_min[_minute_of_day_from_start(row)] = normalize_action(row.get("action", ""))

    extended: list[dict[str, Any]] = []
    for blk in blocks:
        dur = blk["to_min"] - blk["from_min"]
        if dur >= min_minutes:
            extended.append(blk)
            continue
        end = blk["to_min"]
        cursor = blk["to_min"]
        while end - blk["from_min"] < min_minutes and cursor < 24 * 60:
            act = actions_by_min.get(cursor)
            if act is not None and act != target_action:
                break
            end = cursor + 15
            cursor = end
        if end - blk["from_min"] >= min_minutes:
            nb = dict(blk)
            nb["to_min"] = end
            nb["_last_end_min"] = end
            extended.append(nb)
    return extended


def _remerge_overlapping_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge blocks that touch after extension (from_min sorted)."""
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: b["from_min"])
    merged: list[dict[str, Any]] = [dict(ordered[0])]
    for blk in ordered[1:]:
        cur = merged[-1]
        if blk["from_min"] <= cur["to_min"]:
            cur["to_min"] = max(cur["to_min"], blk["to_min"])
            cur["_last_end_min"] = cur["to_min"]
            cur["power_kw"] = max(cur["power_kw"], blk["power_kw"])
            cur["capacity_pct"] = blk["capacity_pct"]
        else:
            merged.append(dict(blk))
    return merged


def derive_timer_schedule_q15(
    rows: list[dict],
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    charge_merged = _merge_blocks_q15(rows, ACTION_CHARGE_GRID)
    discharge_merged = _merge_blocks_q15(rows, ACTION_DISCHARGE_GRID)
    charge_blocks = _filter_min_duration_blocks(
        _remerge_overlapping_blocks(
            _extend_blocks_to_min_duration(charge_merged, rows, ACTION_CHARGE_GRID),
        ),
    )
    discharge_blocks = _filter_min_duration_blocks(
        _remerge_overlapping_blocks(
            _extend_blocks_to_min_duration(discharge_merged, rows, ACTION_DISCHARGE_GRID),
        ),
    )

    return {
        "timed_charge_enabled": bool(charge_blocks),
        "timed_discharge_enabled": bool(discharge_blocks),
        "charge_slots": _blocks_q15_to_slots(charge_blocks, "charge", existing.get("charge_slots", []), cfg),
        "discharge_slots": _blocks_q15_to_slots(
            discharge_blocks, "discharge", existing.get("discharge_slots", []), cfg
        ),
    }


def _parse_hhmm_to_min(time_str: str) -> int | None:
    try:
        parts = str(time_str).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def _slot_is_active(slot: dict[str, Any]) -> bool:
    if slot.get("from") == "00:00" and slot.get("to") == "00:00" and float(slot.get("power_kw") or 0) <= 0:
        return False
    from_min = _parse_hhmm_to_min(slot.get("from", "00:00"))
    to_min = _parse_hhmm_to_min(slot.get("to", "00:00"))
    return from_min is not None and to_min is not None and to_min > from_min


def _hour_slot_clip(
    slot: dict[str, Any],
    hour: int,
) -> tuple[str, str, int] | None:
    """Return (from, to, duration_min) of slot clipped to [hour:00, hour+1:00), or None."""
    from_min = _parse_hhmm_to_min(slot.get("from", "00:00"))
    to_min = _parse_hhmm_to_min(slot.get("to", "00:00"))
    if from_min is None or to_min is None or to_min <= from_min:
        return None
    hour_start = hour * 60
    hour_end = (hour + 1) * 60
    clip_from = max(from_min, hour_start)
    clip_to = min(to_min, hour_end)
    if clip_from >= clip_to:
        return None
    return _min_to_hhmm(clip_from), _min_to_hhmm(clip_to), clip_to - clip_from


def hour_has_timer_schedule(rows: list[dict], hour: int) -> bool:
    """True when Energy arbitrage row for *hour* has a non-empty Timer Schedule cell."""
    row = next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )
    if not row:
        return False
    return bool(str(row.get("timer_schedule") or "").strip())


_TIMER_SCHEDULE_SEG_RE = re.compile(
    r"^(Chg|Dis)\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+([\d.]+)kW\s+cap(\d+)%",
    re.IGNORECASE,
)


def _normalize_hhmm(time_str: str) -> str:
    parts = str(time_str).strip().split(":")
    if len(parts) != 2:
        return time_str
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def parse_timer_schedule_segments(text: str) -> list[dict[str, Any]]:
    """Parse Energy arbitrage Timer Schedule cell — e.g. Dis 19:30-20:00 6.51kW cap16%."""
    segments: list[dict[str, Any]] = []
    for part in (p.strip() for p in text.split("|")):
        if not part:
            continue
        match = _TIMER_SCHEDULE_SEG_RE.match(part)
        if not match:
            continue
        kind_raw, from_t, to_t, power_s, cap_s = match.groups()
        kind = kind_raw.lower()
        segments.append({
            "kind": kind,
            "from": _normalize_hhmm(from_t),
            "to": _normalize_hhmm(to_t),
            "power_kw": float(power_s),
            "capacity_pct": int(cap_s),
        })
    return segments


def build_sa_schedule_from_hour_row(
    rows: list[dict],
    hour: int,
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """SA write payload from one Energy arbitrage hour row (Timer Schedule column)."""
    row = next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )
    if not row:
        return None
    timer_txt = str(row.get("timer_schedule") or "").strip()
    if not timer_txt:
        return None

    segments = parse_timer_schedule_segments(timer_txt)
    if not segments:
        return None

    existing = existing or {}
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    charge_slots = _ensure_three_slots(existing.get("charge_slots", []), "charge")
    discharge_slots = _ensure_three_slots(existing.get("discharge_slots", []), "discharge")
    timed_charge = False
    timed_discharge = False

    for seg in segments:
        if seg["kind"] == "chg":
            timed_charge = True
            tpl = charge_slots[0]
            charge_slots[0] = {
                "slot": 1,
                "from": seg["from"],
                "to": seg["to"],
                "capacity_pct": seg["capacity_pct"],
                "voltage_v": float(tpl.get("voltage_v", 57.6)),
                "power_kw": round(min(float(seg["power_kw"]), ac_kw), 2),
                "grid": True,
                "generator": False,
            }
        elif seg["kind"] == "dis":
            timed_discharge = True
            tpl = discharge_slots[0]
            discharge_slots[0] = {
                "slot": 1,
                "from": seg["from"],
                "to": seg["to"],
                "capacity_pct": seg["capacity_pct"],
                "voltage_v": float(tpl.get("voltage_v", 42.0)),
                "power_kw": round(min(float(seg["power_kw"]), ac_kw), 2),
            }

    if not timed_charge and not timed_discharge:
        return None

    return {
        "target_hour": hour,
        "planned_action": row.get("action"),
        "timer_schedule": timer_txt,
        "timed_charge_enabled": timed_charge,
        "timed_discharge_enabled": timed_discharge,
        "charge_slots": charge_slots,
        "discharge_slots": discharge_slots,
    }


def format_hour_timer_schedule(hour: int, schedule: dict[str, Any]) -> str:
    """SA timer text for one clock hour — clipped range; omit if overlap < 30 min."""
    parts: list[str] = []
    for kind, prefix in (("charge_slots", "Chg"), ("discharge_slots", "Dis")):
        for slot in schedule.get(kind) or []:
            if not _slot_is_active(slot):
                continue
            clip = _hour_slot_clip(slot, hour)
            if clip is None:
                continue
            clip_from, clip_to, dur = clip
            if dur < MIN_TIMER_BLOCK_MINUTES:
                continue
            cap = slot.get("capacity_pct", "")
            pw = slot.get("power_kw", 0)
            parts.append(f"{prefix} {clip_from}-{clip_to} {pw}kW cap{cap}%")
    return " | ".join(parts) if parts else ""


def _blocks_to_slots(
    blocks: list[dict[str, Any]],
    kind: str,
    templates: list[dict[str, Any]],
    cfg: dict,
) -> list[dict[str, Any]]:
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    min_soc = plan_min_soc_pct(cfg)
    slots: list[dict[str, Any]] = []

    for i in range(3):
        tpl = templates[i] if i < len(templates) else {}
        if i >= len(blocks):
            slots.append(_inactive_slot(i + 1, kind, tpl))
            continue

        blk = blocks[i]
        from_h = blk["from_hour"] % 24
        to_h = blk["to_hour"] % 24
        slot: dict[str, Any] = {
            "slot": i + 1,
            "from": f"{from_h:02d}:00",
            "to": f"{to_h:02d}:00",
            "capacity_pct": int(blk["capacity_pct"]) if kind == "charge" else int(min_soc),
            "voltage_v": float(tpl.get("voltage_v", 57.6 if kind == "charge" else 42.0)),
            "power_kw": round(min(float(blk["power_kw"]), ac_kw), 2),
        }
        if kind == "charge":
            slot["grid"] = True
            slot["generator"] = False
        slots.append(slot)

    return slots


def derive_timer_schedule(
    rows: list[dict],
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    charge_blocks = _merge_blocks(rows, ACTION_CHARGE_GRID)
    discharge_blocks = _merge_blocks(rows, ACTION_DISCHARGE_GRID)

    return {
        "timed_charge_enabled": bool(charge_blocks),
        "timed_discharge_enabled": bool(discharge_blocks),
        "charge_slots": _blocks_to_slots(charge_blocks, "charge", existing.get("charge_slots", []), cfg),
        "discharge_slots": _blocks_to_slots(discharge_blocks, "discharge", existing.get("discharge_slots", []), cfg),
    }


def _ensure_three_slots(slots: list[dict], kind: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for i in range(3):
        if i < len(slots):
            result.append(dict(slots[i]))
        else:
            result.append(_inactive_slot(i + 1, kind, None))
    return result


def _row_power_kw(row: dict, action: str, cfg: dict) -> float:
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    if action == ACTION_DISCHARGE_GRID:
        export_kwh = float(row.get("grid_export", row.get("feed_in", 0)) or 0)
        if export_kwh > 0:
            return round(ac_kw, 2)
        load = float(row.get("consumption", 0) or 0)
        return round(min(ac_kw, max(load, 1.0)), 2)
    if action == ACTION_CHARGE_GRID:
        charge_kw = max(float(row.get("battery", 0) or 0), 0.0) + float(
            row.get("grid_import", row.get("buy", 0)) or 0
        )
        fallback = float(cfg.get("_charge_rate_kw", cfg["inverter"]["ac_capacity_kw"]))
        return round(min(ac_kw, max(charge_kw, fallback, 1.0)), 2)
    return 0.0


def build_hourly_schedule(
    rows: list[dict],
    target_hour: int,
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    charge_slots = _ensure_three_slots(existing.get("charge_slots", []), "charge")
    discharge_slots = _ensure_three_slots(existing.get("discharge_slots", []), "discharge")

    row = next(
        (r for r in rows if r.get("hour") == target_hour and r.get("start") != "TOTAL"),
        None,
    )
    action = normalize_action(row.get("action", "")) if row else ACTION_IDLE_GRID
    timer_txt = (row.get("timer_schedule") or "").strip() if row else ""
    if timer_txt.startswith("Dis"):
        action = ACTION_DISCHARGE_GRID
    elif timer_txt.startswith("Chg"):
        action = ACTION_CHARGE_GRID
    from_h = target_hour % 24
    to_h = (target_hour + 1) % 24
    min_soc = int(plan_min_soc_pct(cfg))

    timed_charge = False
    timed_discharge = False

    if action == ACTION_CHARGE_GRID and row:
        timed_charge = True
        tpl = charge_slots[0]
        charge_slots[0] = {
            "slot": 1,
            "from": f"{from_h:02d}:00",
            "to": f"{to_h:02d}:00",
            "capacity_pct": int(round(float(row.get("soc", 80)))),
            "voltage_v": float(tpl.get("voltage_v", 57.6)),
            "power_kw": _row_power_kw(row, action, cfg),
            "grid": True,
            "generator": False,
        }
        discharge_slots[0] = _inactive_slot(1, "discharge", discharge_slots[0])

    elif action == ACTION_DISCHARGE_GRID and row:
        timed_discharge = True
        tpl = discharge_slots[0]
        discharge_slots[0] = {
            "slot": 1,
            "from": f"{from_h:02d}:00",
            "to": f"{to_h:02d}:00",
            "capacity_pct": min_soc,
            "voltage_v": float(tpl.get("voltage_v", 42.0)),
            "power_kw": _row_power_kw(row, action, cfg),
        }
        charge_slots[0] = _inactive_slot(1, "charge", charge_slots[0])

    else:
        charge_slots[0] = _inactive_slot(1, "charge", charge_slots[0])
        discharge_slots[0] = _inactive_slot(1, "discharge", discharge_slots[0])

    return {
        "target_hour": target_hour,
        "planned_action": action,
        "timed_charge_enabled": timed_charge,
        "timed_discharge_enabled": timed_discharge,
        "charge_slots": charge_slots,
        "discharge_slots": discharge_slots,
    }


def _schedule_timed_enabled(schedule: dict[str, Any]) -> bool:
    return bool(schedule.get("timed_charge_enabled") or schedule.get("timed_discharge_enabled"))


def _slot_covers_minute(slot: dict[str, Any], minute_of_day: int) -> bool:
    if not _slot_is_active(slot):
        return False
    start = _parse_hhmm_to_min(slot.get("from", "00:00"))
    end = _parse_hhmm_to_min(slot.get("to", "00:00"))
    return start is not None and end is not None and start <= minute_of_day < end


def pick_sa_timer_schedule(
    rows: list[dict],
    *,
    now_hour: int,
    cfg: dict,
    existing: dict[str, Any] | None = None,
    proposed: dict[str, Any] | None = None,
    now_minute: int | None = None,
) -> dict[str, Any] | None:
    """SA write payload: q15 block covering now, else timed current hour, else next hour."""
    existing = existing or {}
    minute = now_minute if now_minute is not None else now_hour * 60

    if proposed and _schedule_timed_enabled(proposed):
        discharge = (proposed.get("discharge_slots") or [{}])[0]
        charge = (proposed.get("charge_slots") or [{}])[0]
        if proposed.get("timed_discharge_enabled") and _slot_covers_minute(discharge, minute):
            return proposed
        if proposed.get("timed_charge_enabled") and _slot_covers_minute(charge, minute):
            return proposed

    current = build_hourly_schedule(rows, now_hour, cfg, existing)
    if _schedule_timed_enabled(current):
        return current

    next_h = (now_hour + 1) % 24
    upcoming = build_hourly_schedule(rows, next_h, cfg, existing)
    if _schedule_timed_enabled(upcoming):
        return upcoming
    return None
