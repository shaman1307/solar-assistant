"""RCE netto → brutto conversion for billing."""

from src.grid_config import VAT_BRUTTO_MULTIPLIER
from src.rce import rce_pln_kwh_brutto_from_mwh


def test_rce_mwh_to_brutto_kwh():
    # 718.2 PLN/MWh netto → 0.7182 netto kWh → 0.8834 brutto kWh
    assert rce_pln_kwh_brutto_from_mwh(718.2) == round(0.7182 * VAT_BRUTTO_MULTIPLIER, 4)
