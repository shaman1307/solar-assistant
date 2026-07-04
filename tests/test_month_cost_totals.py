"""Monthly history cost total formulas."""

from src.plan_cost import (
    month_energy_cost_total,
    month_import_cost_total,
    month_savings_pln,
)


def test_energy_cost_total_is_export_minus_tariff():
    assert month_energy_cost_total(502.4514, 101.9208) == 400.5306


def test_import_cost_total_is_service_plus_fee():
    assert month_import_cost_total(40.4667, 66.2924) == 106.7591


def test_savings_from_baseline_and_totals():
    baseline_cost = 500.0
    baseline_fee = 60.0
    energy_total = 400.5306
    import_total = 106.7591
    saved = month_savings_pln(baseline_cost, baseline_fee, energy_total, import_total)
    # actual = 106.7591 - 400.5306 = -293.7715; baseline = 560; saved = 853.7715
    assert saved == round(560.0 - (import_total - energy_total), 4)


def test_signed_display_sums_to_net_credit():
    energy_total = month_energy_cost_total(502.4514, 101.9208)
    import_total = month_import_cost_total(40.4667, 66.2924)
    display_sum = energy_total + (-import_total)
    assert round(display_sum, 4) == 293.7715
