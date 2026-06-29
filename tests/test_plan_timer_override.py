"""Manual Timer Schedule overrides on Energy arbitrage plan."""

from src.plan_timer_override import (
    hour_control_from_timer_override,
    set_timer_schedule_override,
)
from src.plan_optimizer import HourControl


def test_hour_control_empty_timer():
    ctrl = hour_control_from_timer_override(14, 0, "")
    assert ctrl == HourControl(0.0, 0.0)


def test_hour_control_charge_segment():
    txt = "Chg 14:00-15:00 5kW cap80%"
    ctrl = hour_control_from_timer_override(14, 0, txt)
    assert ctrl.grid_charge_kw == 5.0
    assert ctrl.battery_export_kwh == 0.0


def test_hour_control_discharge_segment():
    txt = "Dis 19:30-20:00 6.51kW cap16%"
    ctrl = hour_control_from_timer_override(19, 2, txt)
    assert ctrl.grid_charge_kw == 0.0
    assert ctrl.battery_export_kwh > 0


def test_is_timer_schedule_hour_editable_future_only():
    from src.plan_timer_override import is_timer_schedule_hour_editable

    assert is_timer_schedule_hour_editable("2026-06-28", 12, today_date="2026-06-29", plan_from_hour=10) is False
    assert is_timer_schedule_hour_editable("2026-06-29", 10, today_date="2026-06-29", plan_from_hour=10) is False
    assert is_timer_schedule_hour_editable("2026-06-29", 11, today_date="2026-06-29", plan_from_hour=10) is True
    assert is_timer_schedule_hour_editable("2026-06-30", 0, today_date="2026-06-29", plan_from_hour=10) is True


def test_set_timer_override_clears_later_hours():
    cfg: dict = {}
    set_timer_schedule_override(cfg, "2026-06-29", 10, "Chg 10:00-11:00 5kW cap80%")
    set_timer_schedule_override(cfg, "2026-06-29", 16, "Dis 16:00-17:00 6kW cap20%")
    set_timer_schedule_override(cfg, "2026-06-29", 14, "Dis 14:00-15:00 6kW cap20%")
    day = cfg["plan_overrides"]["timer_schedule"]["2026-06-29"]
    assert "10" in day
    assert "16" not in day
    assert day["14"].startswith("Dis")
