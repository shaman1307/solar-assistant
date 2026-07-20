"""
Solar Smart — FastAPI application.

Standalone: http://<pi-ip>:8000/  (does not use port 80; SolarAssistant keeps default :80)
Start with:  uvicorn src.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, load_config
from .sqlite_store import ensure_month_history_billing_model
from . import forecast as forecast_mod
from .app_logging import setup_app_file_logging
from .plan_simulation import build_plan_simulation
from .routes import register_routes
from .plan_monthly_refresh import maybe_run_daily_month_history
from .scheduler import create_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
setup_app_file_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    ensure_month_history_billing_model()
    scheduler = create_scheduler(cfg)
    scheduler.start()

    async def _deferred_month_history() -> None:
        try:
            await maybe_run_daily_month_history(cfg)
        except Exception:
            log.exception("Startup month_history refresh failed")

    asyncio.create_task(_deferred_month_history())

    async def _deferred_startup_plan() -> None:
        # After deploy/restart: wait for beam.smp + SA to recover before any REST reads.
        deploy_marker = BASE_DIR / ".smart-deployed"
        delay_s = 180.0 if deploy_marker.exists() else 60.0
        if deploy_marker.exists():
            try:
                deploy_marker.unlink()
            except OSError:
                pass
            log.info("Post-deploy startup — waiting %.0fs before plan build", delay_s)
        else:
            log.info("Startup — waiting %.0fs before plan build", delay_s)
        await asyncio.sleep(delay_s)
        try:
            await forecast_mod.run_hourly_pv_refresh(cfg)
            result = await build_plan_simulation(
                cfg, force_refresh=False, invalidate_inputs=False,
            )
            log.info(
                "Startup plan ready — %s, %d rows",
                result.get("computed_at"),
                len(result.get("rows") or []),
            )
        except Exception:
            log.exception("Startup plan simulation failed")

    asyncio.create_task(_deferred_startup_plan())
    log.info("Solar Smart started.")
    yield
    scheduler.shutdown(wait=False)
    log.info("Solar Smart stopped.")


app = FastAPI(
    title="Solar Smart",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

register_routes(app)
