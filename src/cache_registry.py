"""Central invalidation for in-memory caches across modules."""

from __future__ import annotations

from . import forecast as forecast_mod
from . import influxdb as influxdb_mod
from . import rce as rce_mod
from . import sa_client
from .plan_simulation import invalidate_plan_cache
from .sqlite_store import ensure_month_history_billing_model


def invalidate_all_caches() -> None:
    """Drop all in-memory caches so the next request picks up fresh data."""
    influxdb_mod.invalidate_caches()
    forecast_mod.invalidate_cache()
    rce_mod.invalidate_cache()
    invalidate_plan_cache()
    sa_client.invalidate_rules_cache()
    ensure_month_history_billing_model()
