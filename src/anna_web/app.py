"""FastAPI app factory for the ANNA web dashboard.

This module exposes ``create_app(cfg)`` and a module-level ``app`` so
uvicorn can import it as ``anna_web.app:app``.

It wires per-process state (ConfigStore / EnvStore /
ScheduleStoreAdapter / RestartManager / AuditReader), mounts the
vendored static directory, registers the shutdown hook, and includes
every route module. ``GET /`` is the mission-control dashboard landing
(``routes/dashboard_routes.py``, MC-02); the Phase 2.5 editor routes
(config / env / schedules / restart / healthz) are unchanged.

See Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md for the original
design and Inbox/2026-06-10-anna-web-mission-control-plan.md for the
mission-control rebuild.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from anna.config import AnnaConfig, load_config
from anna.log import get_logger
from anna_web import audit as web_audit
from anna_web import integrations as web_integrations
from anna_web.config_store import ConfigStore
from anna_web.env_store import EnvStore
from anna_web.middleware import SameOriginMiddleware, is_wildcard_host
from anna_web.readers.audit_reader import AuditReader
from anna_web.restart import RestartManager
from anna_web.routes import (
    config_routes,
    dashboard_routes,
    env_routes,
    healthz_routes,
    restart_routes,
    schedule_routes,
)
from anna_web.schedule_store_adapter import ScheduleStoreAdapter

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(cfg: AnnaConfig) -> FastAPI:
    """Build the FastAPI app for the operator dashboard.

    Per-process singletons (ConfigStore, EnvStore, ScheduleStoreAdapter,
    RestartManager, AuditReader) hang off ``app.state``; routes pull
    them via ``request.app.state.*`` so tests can rebind any of them
    against a tmp anna_home.
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
            # Pair the operational log line with an audit row so the
            # operator's audit-log review surfaces every clean shutdown
            # alongside the startup row emitted by __main__.
            try:
                web_audit.emit("shutdown", cfg=cfg)
            except Exception:  # pragma: no cover - defensive
                # Never let an audit emit failure poison shutdown.
                pass

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
    config_store = ConfigStore(anna_home=cfg.anna_home)
    app.state.config_store = config_store
    env_store = EnvStore(anna_home=cfg.anna_home)
    app.state.env_store = env_store
    schedule_store = ScheduleStoreAdapter(anna_home=cfg.anna_home, config=cfg)
    app.state.schedule_store = schedule_store
    restart_manager = RestartManager(target_unit=cfg.web.target_unit)
    app.state.restart_manager = restart_manager
    # One AuditReader per process (MC-02): its (mtime, size)-keyed parse
    # cache lives on app.state so an unchanged audit file costs one
    # stat() per dashboard poll. Routes call it via run_in_threadpool.
    app.state.audit_reader = AuditReader(cfg.audit_dir)
    app.state.stores = {
        "config_store": config_store,
        "env_store": env_store,
        "schedule_store": schedule_store,
        "restart_manager": restart_manager,
    }

    # Jinja2 template environment. Lives on app.state so subtask 7+
    # route modules can pull it via request.app.state.templates without
    # re-instantiating per request.
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Globals so the orphaned-no-longer restart partial can be `{% include %}`d
    # from any page (Home + Config) without each route threading the unit name
    # through its context, and so the config editor can flag a non-loopback
    # web.host bind inline. Both reuse cfg/metadata already on hand here.
    templates.env.globals["restart_unit"] = cfg.web.target_unit
    templates.env.globals["is_non_loopback_host"] = config_routes.is_non_loopback_host
    # Optional-integration nav gating (mission-control subtask 8):
    # base.html renders one nav <li> per entry, so a disabled
    # integration leaves no trace in the chrome. Computed once at app
    # construction — gates are restart-applied like all anna.yaml
    # config, so per-request recomputation would buy nothing.
    templates.env.globals["integration_nav"] = web_integrations.nav_entries(cfg)
    app.state.templates = templates

    # Vendored frontend assets (htmx + pico + app.css/app.js). Mounted
    # unconditionally so the base template's <link>/<script> tags
    # resolve.
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    # GET / is the mission-control dashboard landing (MC-02). The old
    # index.html stays on disk unrouted until the rebuild completes.
    app.include_router(dashboard_routes.router)
    app.include_router(config_routes.router)
    app.include_router(env_routes.router)
    app.include_router(schedule_routes.router)
    app.include_router(healthz_routes.router)
    app.include_router(restart_routes.router)

    # Config-gated integration routers (mission-control subtask 8).
    # Mounted only when the integration's gate passes for cfg — with
    # the all-off defaults nothing mounts here and e.g. /tasks 404s.
    for integration_router in web_integrations.routers(cfg):
        app.include_router(integration_router)

    # Same-origin middleware: rejects mutating requests whose Origin
    # doesn't match the dashboard's bind address. Registered last so
    # it runs first in the request pipeline (Starlette layers middleware
    # in reverse-registration order). The healthz endpoint is GET-only
    # and therefore unrestricted by the middleware itself.
    #
    # A wildcard bind (0.0.0.0 / ::) has no single canonical origin —
    # the static "http://0.0.0.0:8765" never matches a real browser's
    # Origin, which would silently 403 every POST and leave the LAN-bind
    # dashboard read-only. In that case the middleware anchors the
    # same-origin check to the request's Host header instead. Concrete /
    # loopback binds keep the strict static-origin match unchanged.
    app.add_middleware(
        SameOriginMiddleware,
        allowed_origin=f"http://{cfg.web.host}:{cfg.web.port}",
        wildcard_host=is_wildcard_host(cfg.web.host),
    )

    return app


# Module-level app instance so uvicorn can string-import it as
# ``anna_web.app:app``. Loading config at import time matches how the
# daemon entry point sequences boot: any config-load failure surfaces
# before uvicorn binds the port.
app = create_app(load_config())
