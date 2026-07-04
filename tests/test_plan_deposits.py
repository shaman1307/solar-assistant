"""Energy deposit pool calculations and cascade replay."""

from datetime import date

from src.grid_config import BILLING_MODEL_VERSION
from src.plan_deposits import (
    DEPOSIT_START_MONTH,
    compute_energy_deposit_total,
    draw_import_from_deposits,
    iter_months,
    open_month_id,
    run_deposit_cascade,
)
from src.sqlite_store import (
    DEPOSIT_SEED_AMOUNT,
    DEPOSIT_SEED_MONTH,
    ensure_deposit_seed,
    load_all_deposits,
    load_month_history,
    reset_connection_for_tests,
    save_month_history,
    sum_deposit_current,
)


def test_import_always_drawn_from_oldest_deposit():
    deposits = {
        "2026-05": {"initial": 174.0, "current": 174.0},
        "2026-06": {"initial": 50.0, "current": 50.0},
    }
    total = compute_energy_deposit_total(
        10.0,
        100.0,
        deposits,
        ["2026-05", "2026-06"],
    )
    assert total == 10.0
    assert deposits["2026-05"]["current"] == 74.0
    assert deposits["2026-06"]["current"] == 50.0


def test_export_not_netted_with_import_when_pool_covers():
    deposits = {DEPOSIT_SEED_MONTH: {"initial": 174.0, "current": 174.0}}
    total = compute_energy_deposit_total(
        502.4514,
        101.9208,
        deposits,
        [DEPOSIT_SEED_MONTH],
    )
    assert total == 502.4514
    assert deposits[DEPOSIT_SEED_MONTH]["current"] == round(174.0 - 101.9208, 4)


def test_negative_when_pool_insufficient_for_import():
    deposits = {DEPOSIT_SEED_MONTH: {"initial": 174.0, "current": 174.0}}
    total = compute_energy_deposit_total(
        10.0,
        200.0,
        deposits,
        [DEPOSIT_SEED_MONTH],
    )
    assert total == -16.0
    assert deposits[DEPOSIT_SEED_MONTH]["current"] == 0.0


def test_draw_skips_zero_balance_months():
    deposits = {
        "2026-05": {"initial": 174.0, "current": 0.0},
        "2026-06": {"initial": 80.0, "current": 80.0},
    }
    uncovered = draw_import_from_deposits(50.0, deposits, ["2026-05", "2026-06"])
    assert uncovered == 0.0
    assert deposits["2026-05"]["current"] == 0.0
    assert deposits["2026-06"]["current"] == 30.0


def test_run_cascade_updates_open_month_deposit(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    june = {
        "month": "2026-06",
        "billing_model_version": BILLING_MODEL_VERSION,
        "rows": [{
            "date": "2026-06-01",
            "export_revenue": 502.4514,
            "import_energy_cost": 101.9208,
            "energy_cost_total": 502.4514,
            "import_cost_total": 29.4921,
        }],
        "totals": {
            "export_revenue": 502.4514,
            "import_energy_cost": 101.9208,
            "import_cost_total": 95.7845,
            "baseline_cost": 45.17,
            "baseline_service_fee": 0.0,
            "service_fee": 66.2924,
            "service_cost": 29.4921,
        },
    }
    save_month_history("2026-06", june)

    july = {
        "month": "2026-07",
        "rows": [],
        "totals": {
            "export_revenue": 10.0,
            "import_energy_cost": 100.0,
            "import_cost_total": 20.0,
            "baseline_cost": 30.0,
            "baseline_service_fee": 0.0,
            "service_fee": 5.0,
            "service_cost": 15.0,
        },
    }

    monkeypatch.setattr("src.plan_deposits.open_month_id", lambda _today=None: "2026-07")
    result, deposit_total = run_deposit_cascade("2026-07", july, today=date(2026, 7, 4))

    assert result["totals"]["energy_cost_total"] == 10.0
    deposits = load_all_deposits()
    assert deposits[DEPOSIT_SEED_MONTH]["current"] == 74.0
    assert deposits["2026-07"]["initial"] == 10.0
    assert deposits["2026-07"]["current"] == 10.0
    assert deposit_total == 84.0
    assert deposits[DEPOSIT_SEED_MONTH]["initial"] == DEPOSIT_SEED_AMOUNT


def test_iter_months_inclusive():
    assert iter_months("2026-05", "2026-07") == ["2026-05", "2026-06", "2026-07"]


def test_open_month_id():
    assert open_month_id(date(2026, 7, 4)) == "2026-07"


def test_cascade_does_not_persist_empty_stub_months(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()

    june = {
        "month": "2026-06",
        "billing_model_version": BILLING_MODEL_VERSION,
        "rows": [{
            "date": "2026-06-01",
            "export_revenue": 10.0,
            "import_energy_cost": 2.0,
            "energy_cost_total": 8.0,
            "import_cost_total": 1.0,
        }],
        "totals": {
            "export_revenue": 10.0,
            "import_energy_cost": 2.0,
            "energy_cost_total": 8.0,
            "import_cost_total": 1.0,
            "baseline_cost": 0.0,
            "baseline_service_fee": 0.0,
            "service_fee": 0.0,
            "service_cost": 0.0,
        },
    }
    save_month_history("2026-06", june)

    july = {
        "month": "2026-07",
        "rows": [],
        "totals": {
            "export_revenue": 1.0,
            "import_energy_cost": 0.5,
            "import_cost_total": 0.1,
            "baseline_cost": 0.0,
            "baseline_service_fee": 0.0,
            "service_fee": 0.0,
            "service_cost": 0.0,
        },
    }
    monkeypatch.setattr("src.plan_deposits.open_month_id", lambda _today=None: "2026-07")
    run_deposit_cascade("2026-07", july, today=date(2026, 7, 4))

    assert load_month_history("2026-05") is None
    assert load_month_history("2026-06") is not None
    assert load_month_history("2026-07") is not None


def test_deposits_june_backfill_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()
    from src.sqlite_store import _connect, load_all_deposits, load_month_history

    conn = _connect()
    conn.execute(
        "UPDATE meta SET value = '5' WHERE key = 'schema_version'",
    )
    conn.execute(
        """
        INSERT INTO deposits(month_id, deposit_initial, deposit_current)
        VALUES('2026-05', 174.0, 167.6245)
        """,
    )
    conn.execute(
        """
        INSERT INTO deposits(month_id, deposit_initial, deposit_current)
        VALUES('2026-07', 0.2171, 0.2171)
        """,
    )
    conn.commit()

    from src.sqlite_store import _apply_schema_migrations

    _apply_schema_migrations(conn, 5)

    deposits = load_all_deposits()
    assert deposits["2026-05"]["current"] == 72.0792
    assert deposits["2026-06"]["initial"] == 502.4514
    assert deposits["2026-06"]["current"] == 502.4514
    assert "2026-07" not in deposits
    assert load_month_history("2026-06") is None

    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'deposits_june_backfill_v1'",
    ).fetchone()
    assert row["value"] == "1"


def test_deposit_start_constant():
    assert DEPOSIT_START_MONTH == "2026-05"


def test_seed_may_2026(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    reset_connection_for_tests()
    ensure_deposit_seed()
    deposits = load_all_deposits()
    assert DEPOSIT_SEED_MONTH in deposits
    assert deposits[DEPOSIT_SEED_MONTH]["initial"] == DEPOSIT_SEED_AMOUNT
    assert sum_deposit_current(deposits) == DEPOSIT_SEED_AMOUNT
