"""Timer schedule discharge end parsing for work mode."""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.timer_plan import (
    timer_discharge_early_end_hhmm,
    timer_discharge_end_due,
    timer_discharge_end_times_hhmm,
)


def test_full_hour_discharge_not_early():
    assert timer_discharge_early_end_hhmm("Dis 19:00-20:00 7.12kW cap16%", 19) is None


def test_early_end_at_45():
    assert timer_discharge_early_end_hhmm("Dis 22:00-22:45 6.54kW cap16%", 22) == "22:45"


def test_no_discharge():
    assert timer_discharge_early_end_hhmm("Chg 02:00-03:00 4.0kW cap21%", 2) is None


def test_picks_latest_discharge_end():
    txt = "Dis 22:00-22:30 3kW cap16% | Dis 22:30-22:45 6kW cap16%"
    assert timer_discharge_early_end_hhmm(txt, 22) == "22:45"


def test_discharge_end_times():
    txt = "Dis 22:00-22:45 6.54kW cap16%"
    assert timer_discharge_end_times_hhmm(txt) == ["22:45"]


def test_discharge_end_due_at_45():
    txt = "Dis 22:00-22:45 6.54kW cap16%"
    now = datetime(2026, 6, 26, 22, 45, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = timer_discharge_end_due(txt, now)
    assert due is True
    assert end == "22:45"


def test_discharge_end_not_due_before_45():
    txt = "Dis 22:00-22:45 6.54kW cap16%"
    now = datetime(2026, 6, 26, 22, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, _ = timer_discharge_end_due(txt, now)
    assert due is False


def test_discharge_end_due_on_hour_boundary():
    txt = "Dis 19:00-20:00 7.12kW cap16%"
    now = datetime(2026, 6, 26, 20, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = timer_discharge_end_due(txt, now)
    assert due is True
    assert end == "20:00"


def test_discharge_end_not_due_stale_previous_hour():
    txt = "Dis 19:00-20:00 7.12kW cap16%"
    now = datetime(2026, 6, 26, 22, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, _ = timer_discharge_end_due(txt, now)
    assert due is False
