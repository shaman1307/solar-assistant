"""Battery discharge mode labels must match SA enum strings exactly."""

from src.sa_client import (
    _BATTERY_DISCHARGE_MODE_DEFAULT_OPTIONS,
    _battery_discharge_mode_options,
    _normalize_battery_discharge_mode_for_sa,
)


def test_default_options_match_sa_ui():
    assert list(_BATTERY_DISCHARGE_MODE_DEFAULT_OPTIONS) == [
        "Standby",
        "UPS load only",
        "UPS and home loads",
        "Grid export enabled",
    ]


def test_normalize_legacy_smart_labels():
    assert _normalize_battery_discharge_mode_for_sa("Grid sell") == "Grid export enabled"
    assert _normalize_battery_discharge_mode_for_sa("UPS loads only") == "UPS load only"
    assert _normalize_battery_discharge_mode_for_sa("Standby") == "Standby"


def test_config_options_normalized():
    cfg = {
        "sa": {
            "battery_discharge_mode_options": [
                "Standby",
                "UPS loads only",
                "Grid sell",
            ],
        },
    }
    assert _battery_discharge_mode_options(cfg) == [
        "Standby",
        "UPS load only",
        "Grid export enabled",
    ]
