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


def test_savings_is_actual_energy_net_minus_baseline_energy_net():
    # Actual: export 203.92 − tariff 57.37 = 146.55
    # Baseline: export 250 − tariff 80 = 170
    # Saved = 146.55 − 170 = −23.45
    saved = month_savings_pln(203.92, 57.37, 250.0, 80.0)
    assert saved == round((203.92 - 57.37) - (250.0 - 80.0), 4)


def test_july_style_savings_matches_user_energy_only():
    actual_export, actual_tariff = 203.916, 57.3681
    # baseline net ≈ 131.22 would mean saved ≈ 146.55 − 131.22
    baseline_export, baseline_tariff = 250.0, 118.777  # net 131.223
    saved = month_savings_pln(actual_export, actual_tariff, baseline_export, baseline_tariff)
    actual_net = actual_export - actual_tariff
    baseline_net = baseline_export - baseline_tariff
    assert saved == round(actual_net - baseline_net, 4)
