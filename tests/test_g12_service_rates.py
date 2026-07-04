"""Variable network service cost uses invoice table-2 rates, not G12 full − energy."""

from src.g12_pricing import g12_buy_service_price_pln_kwh, g12_import_cost_split
from src.grid_config import VAT_BRUTTO_MULTIPLIER, merge_grid_defaults


def test_service_rates_from_invoice_table2():
    cfg = merge_grid_defaults({})
    peak = g12_buy_service_price_pln_kwh("peak", cfg)
    off = g12_buy_service_price_pln_kwh("offpeak", cfg)
    assert peak == round(0.3844 * VAT_BRUTTO_MULTIPLIER, 4)
    assert off == round(0.0827 * VAT_BRUTTO_MULTIPLIER, 4)


def test_june_invoice_variable_network_brutto():
    """Invoice FES/00040: 27 kWh peak + 188 kWh offpeak variable network."""
    cfg = merge_grid_defaults({})
    peak_rate = g12_buy_service_price_pln_kwh("peak", cfg)
    off_rate = g12_buy_service_price_pln_kwh("offpeak", cfg)
    total_brutto = round(27 * peak_rate + 188 * off_rate, 2)
    assert total_brutto == 31.89  # invoice line netto 10.38 + 15.55 → brutto 31.89
