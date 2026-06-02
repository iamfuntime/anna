"""FastAPI app factory for the ANNA web dashboard.

Subtask 2 (scaffold) of the Phase 2.5 buildout. This module exposes
``create_app(cfg)`` and a module-level ``app`` so uvicorn can import
it as ``anna_web.app:app``.

The scaffold wires per-process state (placeholder dict until the
ConfigStore/EnvStore/ScheduleStore subtasks land), mounts the
vendored static directory, registers the shutdown hook, and serves a
hardcoded placeholder index. Real routes land in subtasks 6-12.

See Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md for the full design.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from anna.config import AnnaConfig, load_config
from anna.log import get_logger

_STATIC_DIR = Path(__file__).parent / "static"

_PLACEHOLDER_HTML = (
    "<!doctype html>"
    "<html><head><title>ANNA Dashboard</title></head>"
    "<body><h1>ANNA Dashboard</h1>"
    "<p>ANNA Dashboard — scaffold ready. Subtasks 3-13 still in flight.</p>"
    "</body></html>"
)


def create_app(cfg: AnnaConfig) -> FastAPI:
    """Build the FastAPI app for the operator dashboard.

    Per-process singletons (ConfigStore, EnvStore, ScheduleStoreAdapter,
    RestartManager) hang off ``app.state``. At scaffold time they're a
    placeholder dict; subtasks 3-4-9-10 replace the dict entries with
    real objects without changing the wiring shape.
    """
    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # No startup work yet — the daemon entry point already logs
        # anna.web.dashboard.ready before uvicorn binds. The lifespan
        # hook owns the shutdown line so a graceful SIGTERM emits one
        # paired event without fighting asyncio signal handling here.
        try:
            yield
        finally:
            get_logger("anna.web").info("anna.web.dashboard.shutdown")

    app = FastAPI(
        title="ANNA Dashboard",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Per-process state. Routes pull from request.app.state.* once the
    # individual store subtasks land. Scaffolded as a dict so the
    # accessor pattern is stable even with placeholder values.
    app.state.cfg = cfg
    app.state.stores = {
        "config_store": None,
        "env_store": None,
        "schedule_store": None,
        "restart_manager": None,
    }

    # Vendored frontend assets (htmx + pico). Mounted unconditionally so
    # subtask 6's base template can <link>/<script> them once it lands.
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    async def _index() -> str:
        return _PLACEHOLDER_HTML

    return app


# Module-level app instance so uvicorn can string-import it as
# ``anna_web.app:app``. Loading config at import time matches how the
# daemon entry point sequences boot: any config-load failure surfaces
# before uvicorn binds the port.
app = create_app(load_config())
