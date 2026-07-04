"""Internal cost precision: sum at 4 dp before display rounding."""

from src.plan_cost import hour_meter_cash_pln


def _cfg() -> dict:
    return {
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
            },
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.6229,
        },
    }


def test_hour_energy_cost_keeps_four_decimals():
    cash = hour_meter_cash_pln(1.234, 0.567, 0.6229, 0.4500, _cfg(), g12_zone="offpeak")
    assert cash["energy_cost"] == round(
        cash["import_energy_cost"] - cash["export_revenue"], 4,
    )
