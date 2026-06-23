"""G12 Energa buy-zone pricing (peak / offpeak)."""

from __future__ import annotations

from datetime import datetime

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
    """Non-energy (distribution + fees) share of G12 buy price, PLN/kWh brutto."""
    g12 = cfg["grid"]["g12"]
    if zone == "peak":
        full = float(g12["peak_price_pln_kwh"])
        energy = float(g12["peak_energy_only_pln_kwh"])
    else:
        full = float(g12["offpeak_price_pln_kwh"])
        energy = float(g12["offpeak_energy_only_pln_kwh"])
    return max(0.0, full - energy)


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
