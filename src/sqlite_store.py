"""SQLite persistence for plan snapshots, monthly history, and EV charging state."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .grid_config import BILLING_MODEL_VERSION
from .influxdb import now_warsaw

log = logging.getLogger(__name__)

_DB_PATH = BASE_DIR / "data" / "solar_smart.db"
_LEGACY_EV_JSON = BASE_DIR / "data" / "ev_charging.json"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA_VERSION = 3

# Monthly history totals mirrored from payload JSON (schema v2+).
_MONTH_HISTORY_TOTAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("billing_model_version", "TEXT"),
    ("export_revenue", "REAL"),
    ("import_energy_cost", "REAL"),
    ("service_cost", "REAL"),
    ("service_fee", "REAL"),
    ("energy_cost_total", "REAL"),
    ("import_cost_total", "REAL"),
    ("energy_cost", "REAL"),
)

_MONTH_HISTORY_ROW_KEYS: tuple[str, ...] = (
    "export_revenue",
    "import_energy_cost",
    "energy_cost_total",
    "import_cost_total",
)


def db_path() -> Path:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plan_latest (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS month_history (
            month TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            billing_model_version TEXT,
            export_revenue REAL,
            import_energy_cost REAL,
            service_cost REAL,
            service_fee REAL,
            energy_cost_total REAL,
            import_cost_total REAL,
            energy_cost REAL
        );

        CREATE TABLE IF NOT EXISTS ev_store (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
        return
    stored_version = int(row["value"])
    if stored_version < _SCHEMA_VERSION:
        _apply_schema_migrations(conn, stored_version)


def _month_history_table_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(month_history)")}


def _month_history_payload_current(payload: dict[str, Any]) -> bool:
    """True when payload includes split energy cost columns for totals and each day."""
    if payload.get("billing_model_version") != BILLING_MODEL_VERSION:
        return False
    totals = payload.get("totals") or {}
    for key in _MONTH_HISTORY_ROW_KEYS:
        if key not in totals:
            return False
    for row in payload.get("rows") or []:
        for key in _MONTH_HISTORY_ROW_KEYS:
            if key not in row:
                return False
    return True


def _month_history_totals_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    totals = payload.get("totals") or {}
    return (
        payload.get("billing_model_version"),
        totals.get("export_revenue"),
        totals.get("import_energy_cost"),
        totals.get("service_cost"),
        totals.get("service_fee"),
        totals.get("energy_cost_total"),
        totals.get("import_cost_total"),
        totals.get("energy_cost"),
    )


def _backfill_month_history_columns(conn: sqlite3.Connection) -> None:
    """Populate v2 total columns from existing JSON payloads."""
    for row in conn.execute("SELECT month, payload_json FROM month_history"):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if not _month_history_payload_current(payload):
            continue
        conn.execute(
            """
            UPDATE month_history SET
                billing_model_version = ?,
                export_revenue = ?,
                import_energy_cost = ?,
                service_cost = ?,
                service_fee = ?,
                energy_cost_total = ?,
                import_cost_total = ?,
                energy_cost = ?
            WHERE month = ?
            """,
            (*_month_history_totals_values(payload), row["month"]),
        )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    existing = _month_history_table_columns(conn)
    if "import_cost_total" not in existing:
        conn.execute("ALTER TABLE month_history ADD COLUMN import_cost_total REAL")
    _backfill_month_history_columns(conn)
    log.info("SQLite schema migrated to v3 (import_cost_total column)")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    existing = _month_history_table_columns(conn)
    for name, col_type in _MONTH_HISTORY_TOTAL_COLUMNS:
        if name not in existing:
            conn.execute(
                f"ALTER TABLE month_history ADD COLUMN {name} {col_type}",
            )
    _backfill_month_history_columns(conn)
    log.info("SQLite schema migrated to v2 (month_history billing columns)")


def _apply_schema_migrations(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 2:
        _migrate_to_v2(conn)
    if from_version < 3:
        _migrate_to_v3(conn)
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def _now_iso() -> str:
    return now_warsaw().strftime("%Y-%m-%d %H:%M:%S")


def ensure_month_history_billing_model() -> bool:
    """Clear month_history when billing model version changes. Returns True if cleared."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'billing_model_version'",
        ).fetchone()
        stored = row["value"] if row else None
        if stored == BILLING_MODEL_VERSION:
            return False
        conn.execute("DELETE FROM month_history")
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES('billing_model_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (BILLING_MODEL_VERSION,),
        )
        conn.commit()
        log.info(
            "Billing model %s — month_history cache cleared (was %s)",
            BILLING_MODEL_VERSION,
            stored,
        )
        return True


def save_plan_snapshot(plan: dict[str, Any]) -> None:
    payload = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO plan_latest(id, payload_json, updated_at)
            VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (payload, _now_iso()),
        )
        conn.commit()


def load_plan_snapshot() -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM plan_latest WHERE id = 1",
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("plan_latest JSON corrupt: %s", exc)
        return None


def save_month_history(month: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    totals = _month_history_totals_values(payload)
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO month_history(
                month, payload_json, updated_at,
                billing_model_version, export_revenue, import_energy_cost,
                service_cost, service_fee, energy_cost_total, import_cost_total,
                energy_cost
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                billing_model_version = excluded.billing_model_version,
                export_revenue = excluded.export_revenue,
                import_energy_cost = excluded.import_energy_cost,
                service_cost = excluded.service_cost,
                service_fee = excluded.service_fee,
                energy_cost_total = excluded.energy_cost_total,
                import_cost_total = excluded.import_cost_total,
                energy_cost = excluded.energy_cost
            """,
            (month, body, _now_iso(), *totals),
        )
        conn.commit()


def load_month_history(month: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            """
            SELECT payload_json, updated_at, billing_model_version,
                   export_revenue, import_energy_cost, service_cost,
                   service_fee, energy_cost_total, import_cost_total, energy_cost
            FROM month_history WHERE month = ?
            """,
            (month,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("month_history JSON corrupt for %s: %s", month, exc)
        return None
    if not _month_history_payload_current(data):
        log.info("month_history cache stale for %s (missing split cost fields)", month)
        return None
    if row["billing_model_version"] != BILLING_MODEL_VERSION:
        log.info(
            "month_history cache stale for %s (billing model %s != %s)",
            month,
            row["billing_model_version"],
            BILLING_MODEL_VERSION,
        )
        return None
    data.setdefault("_cached_at", row["updated_at"])
    return data


def invalidate_month_history(month: str | None = None) -> None:
    with _lock:
        conn = _connect()
        if month:
            conn.execute("DELETE FROM month_history WHERE month = ?", (month,))
        else:
            conn.execute("DELETE FROM month_history")
        conn.commit()


def load_ev_store_json() -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM ev_store WHERE id = 1",
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("ev_store JSON corrupt: %s", exc)
        return None


def save_ev_store_json(data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO ev_store(id, payload_json, updated_at)
            VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (payload, _now_iso()),
        )
        conn.commit()


def migrate_ev_json_to_sqlite() -> None:
    """One-time import of data/ev_charging.json into SQLite."""
    if load_ev_store_json() is not None:
        return
    if not _LEGACY_EV_JSON.is_file():
        return
    try:
        raw = json.loads(_LEGACY_EV_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("EV JSON migration skipped: %s", exc)
        return
    save_ev_store_json(raw)
    log.info("Migrated ev_charging.json into SQLite")


def reset_connection_for_tests() -> None:
    """Close DB handle (tests only)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
