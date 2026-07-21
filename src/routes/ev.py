"""EV charging API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import ev_charging as ev
from .. import forecast as forecast_mod
from ..config import load_config
from ..plan_simulation import hourly_plan_refresh

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/api/ev-charging")
async def api_get_ev_charging() -> dict[str, Any]:
    cfg = load_config()
    return ev.api_payload(cfg)


@router.post("/api/ev-charging")
async def api_save_ev_charging(body: dict) -> dict[str, Any]:
    cfg = load_config()
    date_str = str(body.get("date") or "").strip()
    if not date_str:
        raise HTTPException(status_code=400, detail="date is required")
    try:
        session = ev.apply_session_update(
            date_str=date_str,
            day=body.get("day"),
            night=body.get("night"),
            cfg=cfg,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await forecast_mod.apply_overrides_to_cache(cfg)
    await hourly_plan_refresh(cfg, unlock_plan_soc=True)
    log.info("EV charging updated for %s", date_str)
    return {"status": "saved", "date": date_str, "session": session}
