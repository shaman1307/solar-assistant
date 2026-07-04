"""SQLite month_history schema v2+ and split cost columns."""

from src.grid_config import BILLING_MODEL_VERSION
from src.plan_cost import month_energy_cost_total, month_import_cost_total
from src.sqlite_store import (
    load_month_history,
    reset_connection_for_tests,
    save_month_history,
)


def _sample_payload() -> dict:
    export = 10.0
    import_tariff = 5.0
    service = 2.0
    fee = 2.0
    energy_total = month_energy_cost_total(export, import_tariff)
    import_total = month_import_cost_total(service, fee)
    return {
        "month": "2026-06",
        "billing_model_version": BILLING_MODEL_VERSION,
        "rows": [
            {
                "date": "2026-06-01",
                "export_revenue": export,
                "import_energy_cost": import_tariff,
                "energy_cost_total": month_energy_cost_total(export, import_tariff),
                "import_cost_total": month_import_cost_total(service),
                "energy_cost": -5.0,
                "service_cost": service,
            },
        ],
        "totals": {
            "export_revenue": export,
            "import_energy_cost": import_tariff,
            "energy_cost_total": energy_total,
            "import_cost_total": import_total,
            "energy_cost": -5.0,
            "service_cost": service,
            "service_fee": fee,
        },
    }


def test_save_load_month_history_with_split_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    payload = _sample_payload()
    save_month_history("2026-06", payload)
    loaded = load_month_history("2026-06")

    assert loaded is not None
    assert loaded["totals"]["energy_cost_total"] == 5.0
    assert loaded["totals"]["import_cost_total"] == 4.0
    assert loaded["rows"][0]["import_cost_total"] == 2.0


def test_load_rejects_legacy_payload_without_split_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    legacy = {
        "month": "2026-06",
        "billing_model_version": BILLING_MODEL_VERSION,
        "rows": [{"date": "2026-06-01", "energy_cost": -5.0}],
        "totals": {"energy_cost": -5.0, "service_cost": 2.0},
    }
    save_month_history("2026-06", legacy)
    assert load_month_history("2026-06") is None


def test_schema_v3_columns_backfilled(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    payload = _sample_payload()
    save_month_history("2026-06", payload)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT billing_model_version, export_revenue, import_energy_cost,
               service_cost, service_fee, energy_cost_total, import_cost_total, energy_cost
        FROM month_history WHERE month = '2026-06'
        """,
    ).fetchone()
    conn.close()

    assert row[0] == BILLING_MODEL_VERSION
    assert row[5] == 5.0
    assert row[6] == 4.0
