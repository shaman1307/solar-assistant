"""Plan simulation parameters loaded from sa-config.yaml."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_SIMULATION: dict[str, Any] = {
    "min_soc_pct": 15,
    "horizon_hours": 24,
    "epsilon_kwh": 0.05,
    "losses_pct": {
        "grid_to_battery": 7.5,
        "battery_to_load_or_grid": 7.5,
        "pv_to_grid": 7.5,
        "pv_to_load": 7.5,
    },
}

MAX_BATTERY_CHARGE_POWER_KW = 5.0
MAX_BATTERY_DISCHARGE_POWER_KW = 8.0
DEFAULT_BATTERY_MAX_CHARGE_POWER_KW = 5.0
DEFAULT_BATTERY_MAX_DISCHARGE_POWER_KW = 8.0


def merge_battery_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure cfg['battery'] has timer power limit fields."""
    bat = cfg.setdefault("battery", {})
    bat.setdefault("max_charge_power_kw", DEFAULT_BATTERY_MAX_CHARGE_POWER_KW)
    bat.setdefault("max_discharge_power_kw", DEFAULT_BATTERY_MAX_DISCHARGE_POWER_KW)
    return cfg


def normalize_battery_power_limits(cfg: dict[str, Any]) -> dict[str, Any]:
    """Clamp battery timer power limits to allowed hardware ceilings."""
    merge_battery_defaults(cfg)
    bat = cfg["battery"]
    bat["max_charge_power_kw"] = round(
        min(MAX_BATTERY_CHARGE_POWER_KW, max(0.1, float(bat["max_charge_power_kw"]))),
        2,
    )
    bat["max_discharge_power_kw"] = round(
        min(MAX_BATTERY_DISCHARGE_POWER_KW, max(0.1, float(bat["max_discharge_power_kw"]))),
        2,
    )
    return cfg


def plan_timer_charge_power_kw(cfg: dict[str, Any]) -> float:
    """SA timer charge power (kW DC into battery)."""
    normalize_battery_power_limits(cfg)
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    return round(min(ac_kw, float(cfg["battery"]["max_charge_power_kw"])), 2)


def plan_timer_discharge_power_kw(cfg: dict[str, Any]) -> float:
    """SA timer discharge power (kW DC from battery)."""
    normalize_battery_power_limits(cfg)
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    return round(min(ac_kw, float(cfg["battery"]["max_discharge_power_kw"])), 2)


def plan_timer_discharge_ac_kw(cfg: dict[str, Any]) -> float:
    """AC output cap (kW) at timer DC discharge — DC × (1 − battery_out loss %)."""
    eta_out = get_simulation_params(cfg)["eta_battery_out"]
    return round(plan_timer_discharge_power_kw(cfg) * float(eta_out), 2)


def plan_timer_charge_grid_kw(cfg: dict[str, Any]) -> float:
    """Grid import (kW AC) to sustain timer DC charge — DC / (1 − grid_to_battery loss %)."""
    eta_grid = get_simulation_params(cfg)["eta_grid_battery"]
    dc_kw = plan_timer_charge_power_kw(cfg)
    if eta_grid <= 0:
        return dc_kw
    return round(dc_kw / float(eta_grid), 2)


def merge_simulation_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure cfg['simulation'] exists with default values (in-memory merge only)."""
    sim = cfg.setdefault("simulation", {})
    for key, value in DEFAULT_SIMULATION.items():
        if key == "losses_pct":
            losses = sim.setdefault("losses_pct", {})
            for loss_key, loss_val in value.items():
                losses.setdefault(loss_key, loss_val)
        else:
            sim.setdefault(key, value)
    return cfg


def get_simulation_params(cfg: dict[str, Any]) -> dict[str, float | int]:
    """Return normalized simulation settings (efficiencies, limits, thresholds)."""
    sim = merge_simulation_defaults(deepcopy(cfg)).get("simulation", {})
    losses = sim.get("losses_pct") or {}

    def loss_pct(key: str) -> float:
        return float(losses.get(key, DEFAULT_SIMULATION["losses_pct"][key]))

    def eta(key: str) -> float:
        return 1.0 - loss_pct(key) / 100.0

    return {
        "min_soc_pct": float(sim["min_soc_pct"]),
        "horizon_hours": int(sim["horizon_hours"]),
        "epsilon_kwh": float(sim["epsilon_kwh"]),
        "eta_grid_battery": eta("grid_to_battery"),
        "eta_battery_out": eta("battery_to_load_or_grid"),
        "eta_pv_grid": eta("pv_to_grid"),
        "eta_pv_load": eta("pv_to_load"),
    }


def plan_min_soc_pct(cfg: dict[str, Any]) -> float:
    return float(get_simulation_params(cfg)["min_soc_pct"])


def plan_min_soc_kwh(cfg: dict[str, Any]) -> float:
    cap = float(cfg["battery"]["capacity_kwh"])
    return (plan_min_soc_pct(cfg) / 100.0) * cap
