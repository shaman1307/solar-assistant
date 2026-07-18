"""Unit tests for battery_export_step_allowed (two consecutive q15 above threshold)."""

from src.plan_optimizer import battery_export_step_allowed

FLOOR = 0.62


def test_single_high_q15_forbidden():
    """One spike above floor without a neighbour → no battery export."""
    rce = [0.40, 0.90, 0.40, 0.40]
    assert battery_export_step_allowed(1, rce, FLOOR, step_scale=0.25) is False


def test_two_consecutive_mid_hour_allowed():
    rce = [0.40, 0.70, 0.71, 0.40]
    assert battery_export_step_allowed(1, rce, FLOOR, step_scale=0.25) is True
    assert battery_export_step_allowed(2, rce, FLOOR, step_scale=0.25) is True


def test_last_quarter_allowed_with_prev_neighbour():
    rce = [0.40, 0.40, 0.70, 0.71]
    assert battery_export_step_allowed(3, rce, FLOOR, step_scale=0.25) is True
    assert battery_export_step_allowed(2, rce, FLOOR, step_scale=0.25) is True


def test_first_quarter_allowed_with_next_neighbour():
    rce = [0.70, 0.71, 0.40, 0.40]
    assert battery_export_step_allowed(0, rce, FLOOR, step_scale=0.25) is True


def test_below_floor_never_allowed():
    rce = [0.70, 0.50, 0.71, 0.72]
    assert battery_export_step_allowed(1, rce, FLOOR, step_scale=0.25) is False


def test_hourly_scale_needs_previous_step():
    """step_scale=1.0: first hour alone cannot export; second can if both above."""
    rce = [0.90, 0.90]
    assert battery_export_step_allowed(0, rce, FLOOR, step_scale=1.0) is False
    assert battery_export_step_allowed(1, rce, FLOOR, step_scale=1.0) is True
