"""Central invalidation for live-input caches (Influx, forecast, RCE, SA rules).

Energy arbitrage plan lives only in SQLite (plan_latest) and is never deleted
here. Config / override / EV replan via hourly_plan_refresh(unlock_plan_soc=True)
so write_plan rewrites future quarters and refreshes the solid SOC day curve.
"""

from __future__ import annotations

from . import forecast as forecast_mod
from . import influxdb as influxdb_mod
from . import rce as rce_mod
from . import sa_client
from .sqlite_store import ensure_month_history_billing_model


def invalidate_input_caches() -> None:
    """Drop live-input caches; Energy arbitrage plan in SQLite is untouched."""
    influxdb_mod.invalidate_caches()
    forecast_mod.invalidate_cache()
    rce_mod.invalidate_cache()
    sa_client.invalidate_rules_cache()
    ensure_month_history_billing_model()


def invalidate_all_caches() -> None:
    """Drop live-input caches; SQLite plan_latest is never deleted."""
    invalidate_input_caches()
