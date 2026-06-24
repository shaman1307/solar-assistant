"""FastAPI route modules."""

from fastapi import FastAPI

from . import config as config_routes
from . import data
from . import debug
from . import ev as ev_routes
from . import rules
from . import ui


def register_routes(app: FastAPI) -> None:
    app.include_router(ui.router)
    app.include_router(data.router)
    app.include_router(rules.router)
    app.include_router(config_routes.router)
    app.include_router(ev_routes.router)
    app.include_router(debug.router)
