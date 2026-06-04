"""``/healthz`` route for the ANNA web dashboard (subtask 11).

One endpoint:

* ``GET /healthz`` — JSON health probe used by the systemd unit's
  healthcheck-on-restart logic, the operator dashboard's Restart
  button (which polls every 2s after a restart click), and any
  external uptime monitoring the operator wires in.

Returns ``{"status": "ok", "anna_running": <bool>, "config_loaded":
<bool>}``. The endpoint is required to be cheap, fast, and
crash-tolerant: every internal failure short-circuits to ``False`` on
the affected flag rather than 500ing the probe out. Operator-side a
``status: ok`` JSON body with ``anna_running: false`` is the signal
the daemon is down, NOT the dashboard.

Notes:

* ``anna_running`` is computed via
  :meth:`anna_web.restart.RestartManager.also_health_probe` (lands in
  subtask 10). The lookup uses ``getattr(..., None)`` so this route
  remains independently testable when subtask 10 hasn't landed yet or
  when a test fixture skips the RestartManager init.
* ``config_loaded`` is an ``isinstance(..., AnnaConfig)`` check
  against ``app.state.cfg``. No disk read; the boot path already
  validated config before uvicorn bound the port, so a positive here
  means the in-memory copy is sound.
* Same-origin enforcement (subtask 12) explicitly excludes GET
  methods, so external monitoring can hit ``/healthz`` without an
  ``Origin`` header.

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md`` "Subtasks → 11"
and "Architecture → A ``/healthz`` endpoint" for the full design.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from anna.config import AnnaConfig


router = APIRouter(prefix="/healthz", tags=["healthz"])

# Probe timeout for the RestartManager dbus/subprocess call. The
# Restart button polls every 2s, so anything that takes longer than
# the poll interval is functionally "down" from the operator's
# perspective. Tuned slightly under 2s so a slow probe doesn't pile
# up requests behind the lock.
_PROBE_TIMEOUT_SECONDS = 2.0


@router.get("")
async def get_healthz(request: Request) -> JSONResponse:
    """Health probe. Always 200 with a JSON body.

    The route MUST NOT raise. Every branch wraps its internal failure
    in a try/except and falls back to ``False`` for the affected
    flag. ``status`` stays ``"ok"`` per the plan; operators read
    ``anna_running``/``config_loaded`` for the actual signal.
    """
    return JSONResponse(await gather_health(request))


async def gather_health(request: Request) -> dict[str, Any]:
    """Compute the health body dict consumed by ``/healthz`` and Home.

    Factored out of :func:`get_healthz` so the dashboard's Home page
    (subtask 8) can server-render the same ``anna_running`` /
    ``config_loaded`` flags at load time, reusing the exact probe shape
    the client-side poller later reads off ``/healthz`` — no second
    source of truth. Same crash-tolerance contract: never raises; every
    internal failure degrades to ``False`` on the affected flag.
    """
    anna_running = False
    config_loaded = False

    # config_loaded: cheap isinstance check against app.state.cfg.
    # Wrapped defensively in case app.state itself is in an exotic
    # state (e.g. cfg attribute missing entirely).
    try:
        cfg = getattr(request.app.state, "cfg", None)
        config_loaded = isinstance(cfg, AnnaConfig)
    except Exception:
        config_loaded = False

    # anna_running: ask the RestartManager. Defensive getattr keeps
    # us independent of subtask 10's landing order — if
    # restart_manager isn't on app.state yet, we return False rather
    # than AttributeError-ing the probe.
    restart_manager = getattr(request.app.state, "restart_manager", None)
    if restart_manager is None:
        anna_running = False
    else:
        try:
            probe = await restart_manager.also_health_probe(
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            anna_running = _probe_active(probe)
        except Exception:
            # dbus unreachable, subprocess fails, probe times out,
            # restart_manager hands back something unexpected — all
            # surface as "anna not running" from the dashboard's
            # perspective. Never 500 the probe.
            anna_running = False

    return {
        "status": "ok",
        "anna_running": anna_running,
        "config_loaded": config_loaded,
    }


def _probe_active(probe: Any) -> bool:
    """Return True iff ``probe`` reports ``active_state == "active"``.

    Tolerates non-dict returns (defensive against future
    RestartManager refactors or test mocks that hand back a stub).
    """
    if not isinstance(probe, dict):
        return False
    return probe.get("active_state") == "active"
