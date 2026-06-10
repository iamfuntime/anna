"""Full activity feed view (MC-05).

Two endpoints, both GET-only (observability never mutates):

* ``GET /activity`` — the full-page audit-event feed: the dashboard's
  recent-activity head (MC-02) grown to depth, with per-event-family
  rendering and kind filtering.
* ``GET /activity/feed`` — the self-polling htmx partial. The partial
  renders its own container (filter bar + rows) with ``hx-swap:
  outerHTML``, so a filter click swaps in a container that polls the
  *new* kind — no JS, the active filter survives every 5s refresh.

Filtering is a plain ``?kind=`` query param mapped onto AuditReader's
``event_prefix`` (the reader already filters newest-first at the tail
layer); unknown values normalize to ``all`` rather than 422-ing a
hand-typed URL.

Read-path contract matches the dashboard (mission-control plan,
"graceful degradation"): :class:`AuditReader` never raises, the helper
wraps it defensively anyway, and an empty result renders
``_panel_empty.html``. Reads run through ``run_in_threadpool`` so a
slow disk never blocks the event loop.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from anna_web.readers.audit_reader import AuditEvent

router = APIRouter(tags=["activity"])

# Depth of the full feed. The dashboard head shows 10; this is the
# "what happened recently" page, bounded well inside AuditReader's
# two-day tail window.
FEED_LIMIT = 100

# Filter key -> AuditReader event_prefix. None = no filter (all events,
# including families this view doesn't render specially — the generic
# row shape still shows them rather than hiding activity).
KIND_PREFIXES: dict[str, str | None] = {
    "all": None,
    "subagents": "audit.subagent.",
    "schedules": "audit.schedule.",
    "dashboard": "audit.web.dashboard.",
}

# (key, label) pairs for the filter bar, in display order.
KIND_FILTERS: list[tuple[str, str]] = [
    ("all", "All"),
    ("subagents", "Sub-agents"),
    ("schedules", "Schedules"),
    ("dashboard", "Dashboard"),
]

_DASHBOARD_EVENT_PREFIX = "audit.web.dashboard."

# Event-name tail markers that read as failure. ``audit.subagent.fail``
# stamps the specific kind (timeout / error / ...) in fields["kind"],
# which the meta line surfaces; the *name* tail decides the row color.
_FAIL_MARKERS = ("fail", "timeout", "error")


@router.get("/activity", response_class=Response)
async def activity(request: Request, kind: str = "all") -> Response:
    """Render the full-page activity feed."""
    kind = _normalize_kind(kind)
    rows = await _feed_rows(request, kind)
    return request.app.state.templates.TemplateResponse(
        request,
        "activity.html",
        {
            "active_nav": "activity",
            "kind": kind,
            "kind_filters": KIND_FILTERS,
            "rows": rows,
        },
    )


@router.get("/activity/feed", response_class=Response)
async def activity_feed(request: Request, kind: str = "all") -> Response:
    """Self-polling feed partial (filter bar + rows), swapped outerHTML."""
    kind = _normalize_kind(kind)
    rows = await _feed_rows(request, kind)
    return request.app.state.templates.TemplateResponse(
        request,
        "_activity_feed.html",
        {"kind": kind, "kind_filters": KIND_FILTERS, "rows": rows},
    )


# ---------------------------------------------------------------------------
# Data helpers — fail-soft, none raise into the route.
# ---------------------------------------------------------------------------


def _normalize_kind(kind: str) -> str:
    """Clamp the query param onto a known filter key.

    A hand-typed or stale ``?kind=`` falls back to ``all`` — the feed
    shows everything rather than 422-ing or rendering a misleading
    empty state.
    """
    return kind if kind in KIND_PREFIXES else "all"


async def _feed_rows(request: Request, kind: str) -> list[dict[str, Any]]:
    """Newest-first display rows for ``kind``; ``[]`` on any failure.

    Same defensive wrap as the dashboard's ``_recent_events``: the
    reader never raises, but the wrap also covers it being absent from
    ``app.state`` and threadpool failures.
    """
    reader = getattr(request.app.state, "audit_reader", None)
    if reader is None:
        return []
    try:
        events = await run_in_threadpool(
            reader.read, limit=FEED_LIMIT, event_prefix=KIND_PREFIXES[kind]
        )
    except Exception:
        return []
    return [_row(e) for e in events]


def _row(event: AuditEvent) -> dict[str, Any]:
    """One template-ready row: split timestamp, status class, meta line.

    Computed here rather than in Jinja so the classification and the
    per-family meta wording are unit-testable and the template stays a
    dumb renderer (the _dashboard_feed idiom, one level richer).
    """
    date_part, time_part = _split_ts(event.ts)
    return {
        "date": date_part,
        "time": time_part,
        "event": event.event,
        "status": _status(event.event),
        "meta": _meta(event),
    }


def _split_ts(ts: str) -> tuple[str, str]:
    """``2026-06-10T07:00:00.000Z`` -> (``2026-06-10``, ``07:00:00``).

    The writer stamps ISO-8601 UTC, so fixed slicing is safe; anything
    shorter or oddly shaped degrades to (no date group, raw string)
    rather than raising on a torn line.
    """
    if len(ts) >= 19 and ts[10] in ("T", " "):
        return ts[:10], ts[11:19]
    return "", ts


def _status(name: str) -> str:
    """Map an event name to a feed-row status class suffix.

    fail/timeout/error tails -> ``fail`` (--status-fail treatment),
    completions -> ``ok``, everything else (spawns, fires, dashboard
    mutations, created/updated/...) -> ``idle``.
    """
    tail = name.rsplit(".", 1)[-1]
    if any(marker in tail for marker in _FAIL_MARKERS):
        return "fail"
    if tail.endswith("complete"):
        return "ok"
    return "idle"


def _meta(event: AuditEvent) -> str:
    """Per-family trailing meta line.

    Sub-agents: slug + model + cost + duration (each only when the
    writer stamped it — e.g. ``model`` appears on spawn, ``duration``
    on complete/fail). Schedules: schedule id + status (the event
    tail) + failure kind. Dashboard mutations: actor + action. Unknown
    families degrade to the dashboard head's actor-ish fallback.
    """
    fields = event.fields
    name = event.event
    parts: list[str] = []

    if name.startswith("audit.subagent."):
        for key in ("slug", "model"):
            value = fields.get(key)
            if value:
                parts.append(str(value))
        cost = fields.get("cost_usd")
        if isinstance(cost, (int, float)):
            parts.append(f"${cost:.2f}")
        duration = fields.get("duration_seconds")
        if isinstance(duration, (int, float)):
            parts.append(f"{duration:.1f}s")
        kind_field = fields.get("kind")
        if kind_field:
            parts.append(str(kind_field))
    elif name.startswith("audit.schedule."):
        schedule_id = fields.get("schedule_id")
        if schedule_id:
            parts.append(str(schedule_id))
        parts.append(name.rsplit(".", 1)[-1])
        kind_field = fields.get("kind")
        if kind_field:
            parts.append(str(kind_field))
    elif name.startswith(_DASHBOARD_EVENT_PREFIX):
        actor = fields.get("actor")
        if actor:
            parts.append(str(actor))
        parts.append(name[len(_DASHBOARD_EVENT_PREFIX) :])
    else:
        fallback = fields.get("slug") or fields.get("actor")
        if fallback:
            parts.append(str(fallback))

    return " · ".join(parts)
