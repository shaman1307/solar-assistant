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
    },
}


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
    }


def plan_min_soc_pct(cfg: dict[str, Any]) -> float:
    return float(get_simulation_params(cfg)["min_soc_pct"])


def plan_min_soc_kwh(cfg: dict[str, Any]) -> float:
    cap = float(cfg["battery"]["capacity_kwh"])
    return (plan_min_soc_pct(cfg) / 100.0) * cap
