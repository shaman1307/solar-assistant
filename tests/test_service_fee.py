"""Monthly service fee and grid config defaults."""

from src.grid_config import compute_service_fee_pln, merge_grid_defaults


def _cfg() -> dict:
    return merge_grid_defaults({
        "grid": {
            "g12": {"offpeak_price_pln_kwh": 0.6229},
        },
    })


def test_service_fee_fixed_plus_per_kwh():
    cfg = _cfg()
    fee = compute_service_fee_pln(215.0, cfg)
    fixed = (
        0.9102 + 24.8091 + 29.5815
    )
    variable = 215.0 * (0.0408 + 0.0090 + 0.0037)
    assert fee == round(fixed + variable, 4)


def test_grid_export_threshold_default_from_offpeak():
    cfg = merge_grid_defaults({"grid": {"g12": {"offpeak_price_pln_kwh": 0.6229}}})
    assert cfg["grid"]["grid_export_threshold_pln_kwh"] == 0.6229
