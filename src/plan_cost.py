"""Hourly and plan-period cash balance from G12 buy and grid export credits."""

from __future__ import annotations

from .g12_pricing import g12_import_cost_split
from .plan_optimizer import export_credit_price, g12_tariff_from_cfg


def _attach_import_split(
    result: dict[str, float | None],
    *,
    grid_import: float,
    g12_zone: str,
    cfg: dict,
) -> dict[str, float | None]:
    imp_energy, service_cost = g12_import_cost_split(grid_import, g12_zone, cfg)
    export_revenue = float(result.get("export_revenue") or 0.0)
    energy_cost = imp_energy - export_revenue
    total_cost = energy_cost + service_cost
    result["import_energy_cost"] = round(imp_energy, 4)
    result["service_cost"] = round(service_cost, 4)
    result["energy_cost"] = round(energy_cost, 4)
    result["cost"] = round(total_cost, 4)
    return result


def hour_grid_cash_pln(
    grid_import: float,
    grid_export: float,
    buy_price: float,
    rce_price: float | None,
    cfg: dict,
    *,
    battery_export: float = 0.0,
    g12_zone: str = "offpeak",
) -> dict[str, float | None]:
    """Cash balance for one planned/smart hour (PLN).

    Positive net = expense (import cost exceeds export credit).
    Negative net = net credit (export revenue exceeds import cost).
    """
    tariff = g12_tariff_from_cfg(cfg)

    imp = max(0.0, float(grid_import))
    exp = max(0.0, float(grid_export))
    batt = max(0.0, min(float(battery_export), exp))
    pv_exp = max(0.0, exp - batt)

    import_cost = imp * float(buy_price)

    if exp <= 0.0:
        export_revenue = 0.0
        eff_credit: float | None = None
    else:
        credit_batt = export_credit_price(rce_price, tariff, from_battery=True, cfg=cfg)
        credit_pv = export_credit_price(rce_price, tariff, from_battery=False, cfg=cfg)
        export_revenue = batt * credit_batt + pv_exp * credit_pv
        eff_credit = export_revenue / exp

    net = import_cost - export_revenue
    return _attach_import_split(
        {
            "import_cost": round(import_cost, 4),
            "export_revenue": round(export_revenue, 4),
            "cost": round(net, 4),
            "export_credit": round(eff_credit, 4) if eff_credit is not None else None,
        },
        grid_import=imp,
        g12_zone=g12_zone,
        cfg=cfg,
    )


def hour_meter_cash_pln(
    grid_import: float,
    grid_export: float,
    buy_price: float,
    rce_price: float | None,
    cfg: dict,
    *,
    g12_zone: str = "offpeak",
) -> dict[str, float | None]:
    """Cash for factual Influx history: import × buy (brutto), export × hourly RCE (brutto)."""
    imp = max(0.0, float(grid_import))
    exp = max(0.0, float(grid_export))
    import_cost = imp * float(buy_price)
    if exp <= 0.0 or rce_price is None:
        export_revenue = 0.0
        export_credit: float | None = None
    else:
        export_credit = float(rce_price)
        export_revenue = exp * export_credit
    net = import_cost - export_revenue
    return _attach_import_split(
        {
            "import_cost": round(import_cost, 4),
            "export_revenue": round(export_revenue, 4),
            "cost": round(net, 4),
            "export_credit": round(export_credit, 4) if export_credit is not None else None,
        },
        grid_import=imp,
        g12_zone=g12_zone,
        cfg=cfg,
    )


def infer_battery_export_kwh(
    grid_export: float,
    battery_delta: float,
    epsilon: float = 0.001,
    bat_discharge: float | None = None,
) -> float:
    """Meter kWh from battery to grid (0 when export is PV spill only)."""
    exp = max(0.0, float(grid_export))
    if exp <= epsilon:
        return 0.0
    dis_net = abs(float(battery_delta)) if float(battery_delta) < -epsilon else 0.0
    dis_gross = (
        float(bat_discharge)
        if bat_discharge is not None and float(bat_discharge) > epsilon
        else 0.0
    )
    dis = max(dis_net, dis_gross)
    if dis <= epsilon:
        return 0.0
    return min(exp, dis)


def derive_grid_flows_from_balance(
    pv: float,
    load: float,
    battery_delta: float,
    epsilon: float = 0.001,
) -> tuple[float, float]:
    """Derive grid import/export (positive kWh) from energy balance when Influx grid is missing."""
    net = float(load) - float(pv) - float(battery_delta)
    if net > epsilon:
        return net, 0.0
    if net < -epsilon:
        return 0.0, -net
    return 0.0, 0.0


def compute_plan_totals(rows: list[dict]) -> dict:
    """Aggregate rows; TOTAL cost = Σ import_cost − Σ export_revenue (day balance)."""
    if not rows:
        return {
            "start": "TOTAL",
            "production": 0.0,
            "consumption": 0.0,
            "battery": 0.0,
            "bat_charge": 0.0,
            "bat_discharge": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "soc": 0.0,
            "cost": 0.0,
            "energy_cost": 0.0,
            "service_cost": 0.0,
            "import_cost": 0.0,
            "export_revenue": 0.0,
            "action": "",
            "rce_price": None,
            "g12_zone": "",
            "buy_price": None,
        }

    import_cost = sum(float(r.get("import_cost") or 0.0) for r in rows)
    export_revenue = sum(float(r.get("export_revenue") or 0.0) for r in rows)
    energy_cost = sum(float(r.get("energy_cost") or 0.0) for r in rows)
    service_cost = sum(float(r.get("service_cost") or 0.0) for r in rows)

    return {
        "start": "TOTAL",
        "production": round(sum(float(r["production"]) for r in rows), 2),
        "consumption": round(sum(float(r["consumption"]) for r in rows), 2),
        "battery": round(sum(float(r["battery"]) for r in rows), 2),
        "bat_charge": round(sum(float(r.get("bat_charge") or 0) for r in rows), 2),
        "bat_discharge": round(sum(float(r.get("bat_discharge") or 0) for r in rows), 2),
        "grid_import": round(sum(float(r["grid_import"]) for r in rows), 2),
        "grid_export": round(sum(float(r["grid_export"]) for r in rows), 2),
        "soc": rows[-1]["soc"],
        "import_cost": round(import_cost, 4),
        "export_revenue": round(export_revenue, 4),
        "energy_cost": round(energy_cost, 4),
        "service_cost": round(service_cost, 4),
        "cost": round(energy_cost + service_cost, 4),
        "action": "",
        "rce_price": None,
        "g12_zone": "",
        "buy_price": None,
    }


def month_energy_cost_total(export_revenue: float, import_energy_cost: float) -> float:
    """Export RCE credit minus G12 import tariff (brutto)."""
    return round(export_revenue - import_energy_cost, 4)


def month_import_cost_total(service_cost: float, service_fee: float = 0.0) -> float:
    """Distribution service cost plus monthly service fee."""
    return round(service_cost + service_fee, 4)


def month_savings_pln(
    baseline_cost: float,
    baseline_service_fee: float,
    energy_cost_total: float,
    import_cost_total: float,
) -> float:
    """Saved = baseline bill − actual bill (import total − energy total)."""
    baseline_bill = baseline_cost + baseline_service_fee
    actual_bill = import_cost_total - energy_cost_total
    return round(baseline_bill - actual_bill, 4)
