"""``/settings`` hub for the ANNA web dashboard (MC-10).

One GET-only endpoint:

* ``GET /settings`` — a hub page of panels linking every operator
  tool the mission-control nav consolidated under "Settings":

  - **Config** — link to the schema-driven ``/config`` editor, plus a
    config-loaded badge computed via
    :func:`anna_web.routes.healthz_routes.gather_health` (the exact
    probe ``/healthz`` serves — no second source of truth).
  - **Secrets** — link to the masked ``/env`` editor.
  - **Schedules** — links to the existing schedule run board and the
    create form (``/schedules``, ``/schedules/new``).
  - **Service** — the ``restart_button.html`` partial ``{% include %}``d
    directly, so the restart control is reachable from the nav again
    (it lost its Home placement in the MC-02 rebuild). The include
    carries no extra context, exactly like the Config pages: the
    partial falls back to the ``restart_unit`` Jinja global and
    renders its idle state; POST/poll semantics are owned entirely by
    :mod:`anna_web.routes.restart_routes` and are untouched here.
  - **Integrations** — a read-only description of
    ``cfg.integrations.obsidian`` (enabled/disabled badges, configured
    paths) linking to its ``/config/integrations`` section editor.

The hub itself mutates nothing and registers no new mutating
endpoints — it only links to and embeds the existing surfaces. Per the
mission-control plan's graceful-degradation contract, every data
lookup is fail-soft: a missing RestartManager or an exotic
``app.state`` degrades a badge to its "down" state rather than 500ing
the page.

See ``Inbox/2026-06-10-anna-web-mission-control-plan.md``, subtask 10.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from anna_web.routes import healthz_routes

router = APIRouter(tags=["settings"])


@router.get("/settings", response_class=HTMLResponse)
async def get_settings(request: Request) -> Response:
    """Render the settings hub.

    ``gather_health`` never raises (its documented contract), so the
    service / config badges always render. The obsidian block is
    pulled defensively off ``app.state.cfg`` so a test fixture that
    skips full config wiring renders the disabled state instead of
    AttributeError-ing the page.
    """
    health = await healthz_routes.gather_health(request)

    cfg = getattr(request.app.state, "cfg", None)
    obsidian = getattr(getattr(cfg, "integrations", None), "obsidian", None)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_nav": "settings",
            "anna_running": health["anna_running"],
            "config_loaded": health["config_loaded"],
            "obsidian": obsidian,
        },
    )
