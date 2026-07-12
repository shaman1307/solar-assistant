"""Reserve SOC uses hourly PV coverage, not a single sunny q15 slot."""

from src.plan_optimizer import _reserve_soc_kwh_from_step


def test_reserve_not_cut_by_single_sunny_q15():
    """One bright 15-min slot must not end the night reserve early."""
    pv = [
        0.0, 0.0, 0.0, 0.0,
        0.5, 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0,
    ]
    load = [0.12] * 12
    reserve = _reserve_soc_kwh_from_step(
        0, pv, load,
        reserve_floor_kwh=1.5,
        eta_out=0.9,
        eta_pv_load=0.95,
        epsilon=0.01,
    )
    # Hour 1 total PV still below load — reserve must cover h1+h2 deficits + floor.
    assert reserve > 2.0
