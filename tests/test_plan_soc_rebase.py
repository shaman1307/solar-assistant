"""Future plan rows: SOC chained from blended current-hour anchor."""

from src.plan_hourly_actuals import display_soc_rebased_from_prior


def test_rebase_soc_adds_optimizer_delta():
    soc = display_soc_rebased_from_prior(55.0, 58.0, 52.0, min_soc_pct=15.0)
    assert soc == 61.0


def test_rebase_soc_clamps_to_min():
    soc = display_soc_rebased_from_prior(20.0, 10.0, 25.0, min_soc_pct=15.0)
    assert soc == 15.0


def test_rebase_soc_clamps_to_max():
    soc = display_soc_rebased_from_prior(98.0, 105.0, 100.0, min_soc_pct=15.0)
    assert soc == 100.0
