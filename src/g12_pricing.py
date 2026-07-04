"""G12 Energa buy-zone pricing (peak / offpeak)."""

from __future__ import annotations

from datetime import datetime

from .grid_config import merge_grid_defaults

# Energa G12 weekday peak windows (hour ranges, start inclusive, end exclusive).
G12_PEAK_HOURS_WEEKDAY: list[tuple[int, int]] = [(6, 13), (15, 22)]


def get_g12_zone(dt: datetime, cfg: dict) -> str:
    del cfg
    weekday = dt.weekday()
    if weekday >= 5:
        return "offpeak"
    hour = dt.hour
    for start, end in G12_PEAK_HOURS_WEEKDAY:
        if start <= hour < end:
            return "peak"
    return "offpeak"


def get_buy_price(dt: datetime, cfg: dict) -> tuple[float, str]:
    g12 = cfg["grid"]["g12"]
    zone = get_g12_zone(dt, cfg)
    price = g12["peak_price_pln_kwh"] if zone == "peak" else g12["offpeak_price_pln_kwh"]
    return float(price), zone


def g12_buy_energy_price_pln_kwh(zone: str, cfg: dict) -> float:
    """Energy (obrót) component of G12 buy price, PLN/kWh brutto."""
    g12 = cfg["grid"]["g12"]
    key = "peak_energy_only_pln_kwh" if zone == "peak" else "offpeak_energy_only_pln_kwh"
    return float(g12[key])


def g12_buy_service_price_pln_kwh(zone: str, cfg: dict) -> float:
    """Official G12 variable network rate (brutto): opłata sieciowa zmienna dzienna/nocna."""
    merge_grid_defaults(cfg)
    dist = cfg["grid"]["distribution"]
    key = (
        "peak_variable_network_pln_kwh"
        if zone == "peak"
        else "offpeak_variable_network_pln_kwh"
    )
    return float(dist[key])


def g12_import_cost_split(
    grid_import: float,
    zone: str,
    cfg: dict,
) -> tuple[float, float]:
    """Split grid import kWh cost into energy vs service (PLN)."""
    imp = max(0.0, float(grid_import))
    energy = imp * g12_buy_energy_price_pln_kwh(zone, cfg)
    service = imp * g12_buy_service_price_pln_kwh(zone, cfg)
    return energy, service
