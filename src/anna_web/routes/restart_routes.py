"""``/restart`` route surface for the ANNA web dashboard.

Subtask 10 of the Phase 2.5 buildout. One operator surface: the big red
"Restart anna.service" button on the dashboard. POST hits
``app.state.restart_manager.restart()`` and renders the
``restart_button.html`` partial in either its "restarting" or "idle"
state depending on the outcome.

Three routes:

* ``POST /restart`` — request a restart. Returns the partial in the
  "restarting" state with an embedded HTMX poll that drives the UI back
  to "idle" once the daemon is healthy again. On failure returns 500
  plus an error toast and the partial stays idle for retry.
* ``GET /restart`` — return the idle partial. The client-side polling
  loop in ``restart_button.html`` swaps to this once it has seen enough
  consecutive healthy /healthz responses to consider the restart done.
* ``HEAD /restart`` is intentionally NOT exposed; the only safe verb is
  POST plus the GET-for-rehydration above.

Audit-event emission for ``audit.web.dashboard.restart_request`` is
wired through :mod:`anna_web.audit`; the emit fires on the success
path with the pinned unit name + dispatch method. Same-origin
enforcement runs in :class:`anna_web.middleware.SameOriginMiddleware`
ahead of this handler.

The route NEVER accepts a unit name from the request body. The unit is
pinned on the :class:`anna_web.restart.RestartManager` at construction
time inside :func:`anna_web.app.create_app`. There is no path for a
crafted POST to ask for ``RestartUnit("any-other.service")``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from anna_web import audit as web_audit
from anna_web.restart import RestartResult

router = APIRouter(prefix="/restart", tags=["restart"])


def _idle_context(request: Request) -> dict:
    """Build the template context for the idle state of the button."""
    manager = request.app.state.restart_manager
    return {
        "state": "idle",
        "unit": manager.target_unit,
        "method": None,
        "job_id": None,
        "error": None,
        "last_restart": None,
        "last_restart_relative": None,
        "toast": None,
    }


def _restarting_context(request: Request, result: RestartResult) -> dict:
    """Build the template context for the optimistic "restarting" state."""
    manager = request.app.state.restart_manager
    return {
        "state": "restarting",
        "unit": manager.target_unit,
        "method": result.method,
        "job_id": result.job_id,
        "error": None,
        "last_restart": None,
        "last_restart_relative": None,
        "toast": {
            "level": "success",
            "message": f"Restart dispatched via {result.method}.",
        },
    }


def _error_context(request: Request, result: RestartResult) -> dict:
    """Build the template context for a failed restart."""
    manager = request.app.state.restart_manager
    return {
        "state": "idle",
        "unit": manager.target_unit,
        "method": result.method,
        "job_id": None,
        "error": result.error,
        "last_restart": None,
        "last_restart_relative": None,
        "toast": {
            "level": "error",
            "message": f"Restart failed: {result.error}",
        },
    }


@router.get("", response_class=HTMLResponse)
async def get_restart(request: Request) -> Response:
    """Return the idle partial.

    The client-side polling loop in ``restart_button.html`` swaps to
    this endpoint after it sees enough healthy /healthz responses to
    consider the restart finished. The route is a pure render — no
    state mutation — so a stray GET from a browser refresh is safe.
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "restart_button.html",
        _idle_context(request),
    )


@router.post("", response_class=HTMLResponse)
async def post_restart(request: Request) -> Response:
    """Dispatch a restart of the pinned unit.

    Body is empty / unused — the unit name lives on
    ``app.state.restart_manager`` and was pinned at app construction.
    The route never reads ``request.json()`` or ``request.form()``,
    which is the load-bearing guarantee that "no crafted POST can
    redirect the restart to another unit."

    Success: 200 + the ``restart_button.html`` partial in its
    "restarting" state (which embeds the HTMX poll against /healthz so
    the UI can swap itself back to idle once the daemon is up).

    Failure: 500 + the same partial in its idle state plus an error
    toast inline. HTMX leaves the button enabled so the operator can
    retry.

    Emits ``audit.web.dashboard.restart_request`` on the success path
    with the pinned unit name and dispatch method (``dbus`` or
    ``subprocess``). Failures land in the operational stream via
    :mod:`anna_web.restart`; no audit row on failure because the
    daemon was not actually mutated.
    """
    manager = request.app.state.restart_manager
    templates = request.app.state.templates

    result = await manager.restart()

    if result.ok:
        web_audit.emit(
            "restart_request",
            request=request,
            unit=manager.target_unit,
            method=result.method,
        )
        return templates.TemplateResponse(
            request,
            "restart_button.html",
            _restarting_context(request, result),
        )

    return templates.TemplateResponse(
        request,
        "restart_button.html",
        _error_context(request, result),
        status_code=500,
    )
