"""Mission-control dashboard landing (MC-02).

Two endpoints, both GET-only (observability never mutates):

* ``GET /`` — the panel-grid landing: service status, recent-activity
  head, schedule-health summary, today's delegation cost.
* ``GET /dashboard/panels/activity`` — the recent-activity partial the
  page's htmx poll re-fetches every 5s (mirrors the restart partial's
  poll idiom, per the plan's "htmx polling, not SSE" decision).

Read-path contract (mission-control plan, "graceful degradation"):
every panel's data helper is fail-soft — a missing reader, a missing
data dir (fresh install has no ``audit/`` or ``transcripts/``), or any
unexpected exception degrades to ``None``/``[]`` and the template
renders ``_panel_empty.html`` instead. No panel ever 500s the landing.

Synchronous readers (:class:`AuditReader`, :class:`DelegationReader`)
run through ``run_in_threadpool`` so a slow disk never blocks the
event loop. The service-status panel reuses
:func:`anna_web.routes.healthz_routes.gather_health` — the exact probe
``/healthz`` serves — so the server-rendered first paint and the
client-side poller share one source of truth.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from anna_web.routes import healthz_routes

try:
    # MC-04 lands the DelegationReader independently of this subtask;
    # guard the import so the cost panel degrades to "no data yet"
    # rather than ImportError-ing app boot if it hasn't merged yet.
    from anna_web.readers.delegation_reader import DelegationReader
except Exception:  # pragma: no cover - import-order guard
    DelegationReader = None  # type: ignore[assignment, misc]

router = APIRouter(tags=["dashboard"])

# Number of audit events the recent-activity head shows. The full feed
# (MC-05, /activity) owns depth; the dashboard panel is a teaser.
ACTIVITY_HEAD_LIMIT = 10


@router.get("/", response_class=Response)
async def dashboard(request: Request) -> Response:
    """Render the panel-grid landing."""
    health = await healthz_routes.gather_health(request)
    events = await _recent_events(request)
    schedule_summary = await _schedule_summary(request)
    cost_today = await _cost_today(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "anna_running": health["anna_running"],
            "config_loaded": health["config_loaded"],
            "events": events,
            "schedule_summary": schedule_summary,
            "cost_today": cost_today,
        },
    )


@router.get("/dashboard/panels/activity", response_class=Response)
async def activity_panel(request: Request) -> Response:
    """Recent-activity head partial, swapped in by the page's htmx poll."""
    events = await _recent_events(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "_dashboard_feed.html",
        {"events": events},
    )


# ---------------------------------------------------------------------------
# Panel data helpers — all fail-soft, none raise into the route.
# ---------------------------------------------------------------------------


async def _recent_events(request: Request) -> list[Any]:
    """Newest audit events for the activity head; ``[]`` on any failure.

    ``AuditReader.read`` already never raises, but the defensive wrap
    also covers the reader being absent from ``app.state`` (test
    fixtures that skip create_app's wiring) and threadpool failures.
    """
    reader = getattr(request.app.state, "audit_reader", None)
    if reader is None:
        return []
    try:
        return await run_in_threadpool(reader.read, limit=ACTIVITY_HEAD_LIMIT)
    except Exception:
        return []


async def _schedule_summary(request: Request) -> dict[str, Any] | None:
    """Schedule-health rollup: total / enabled counts + failing rows.

    ``None`` (→ empty-state panel) when the store is absent, the read
    fails, or there are simply no schedules yet.
    """
    store = getattr(request.app.state, "schedule_store", None)
    if store is None:
        return None
    try:
        schedules = await store.list_all()
    except Exception:
        return None
    if not schedules:
        return None
    failing = [s for s in schedules if s.state.consecutive_failures > 0]
    return {
        "total": len(schedules),
        "enabled": sum(1 for s in schedules if s.enabled),
        "failing": failing,
    }


async def _cost_today(request: Request) -> dict[str, Any] | None:
    """Today's delegation cost bucket; ``None`` when there is nothing.

    Uses :meth:`DelegationReader.daily_rollup` with a one-day window —
    the single bucket it returns is today's. Zero runs (fresh install,
    quiet day, or the reader module not merged yet) renders the empty
    state rather than a $0.00 figure.
    """
    reader = _delegation_reader(request)
    if reader is None:
        return None
    try:
        rollup = await run_in_threadpool(reader.daily_rollup, window_days=1)
    except Exception:
        return None
    bucket = next(iter(rollup.values()), None)
    if not bucket or not bucket.get("runs"):
        return None
    return {"runs": bucket["runs"], "cost_usd": bucket["cost_usd"]}


def _delegation_reader(request: Request) -> Any | None:
    """Lazily build (and cache on ``app.state``) the DelegationReader.

    Constructed here rather than in ``create_app`` so app boot carries
    no dependency on MC-04's module — the guarded import above decides
    availability, and the per-app cache keeps the reader's mtime-keyed
    parse cache warm across polls.
    """
    if DelegationReader is None:
        return None
    reader = getattr(request.app.state, "delegation_reader", None)
    if reader is None:
        try:
            reader = DelegationReader.from_config(request.app.state.cfg)
        except Exception:
            return None
        request.app.state.delegation_reader = reader
    return reader
