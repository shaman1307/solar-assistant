"""Central invalidation for live-input caches (Influx, forecast, RCE, SA rules).

Energy arbitrage plan lives only in SQLite (plan_latest). Quarter refresh must
use invalidate_input_caches(); delete_plan() only on explicit config/override reset.
"""

from __future__ import annotations

from . import forecast as forecast_mod
from . import influxdb as influxdb_mod
from . import rce as rce_mod
from . import sa_client
from .sqlite_store import delete_plan, ensure_month_history_billing_model


def invalidate_input_caches() -> None:
    """Drop live-input caches; Energy arbitrage plan in SQLite is untouched."""
    influxdb_mod.invalidate_caches()
    forecast_mod.invalidate_cache()
    rce_mod.invalidate_cache()
    sa_client.invalidate_rules_cache()
    ensure_month_history_billing_model()


def invalidate_all_caches() -> None:
    """Drop live-input caches and delete SQLite plan_latest (full reset)."""
    invalidate_input_caches()
    delete_plan()
