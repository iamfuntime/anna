"""Delegation & cost view (MC-07).

Two endpoints, both GET-only (observability never mutates):

* ``GET /delegations`` — the full page: summary strip (today / this
  week / run count / busiest agent), per-model cost split with
  CSS-only bars, per-agent run history, and a 14-day daily cost strip.
* ``GET /delegations/panels/data`` — the data-sections partial the
  page's htmx poll re-fetches every 30s (same polling-not-SSE idiom as
  the dashboard's activity head, MC-02).

All numbers come from :class:`DelegationReader` (MC-04), the read
layer over sub-agent transcript trailers. The reader is synchronous
and disk-bound, so both routes call it via ``run_in_threadpool`` — and
they make exactly one threadpool hop per request: ``_gather`` runs all
four reader queries in one sync call, riding the reader's per-file
parse cache for everything after ``runs()``.

Read-path contract (mission-control plan, "graceful degradation"):
any failure — reader construction, missing transcript root, an
unexpected exception mid-aggregation — degrades to the empty context
and the template renders ``_panel_empty.html`` per section. The page
never 500s on a fresh install.

Bars are pure CSS: ``_gather`` computes each row's percentage of the
window maximum server-side and the templates emit it as an inline
width/height style. No chart library, no JS.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from anna_web.readers.delegation_reader import (
    DEFAULT_WINDOW_DAYS,
    DelegationReader,
)

router = APIRouter(tags=["delegations"])

# History-table depth. The window already bounds the scan; this caps
# the rendered rows so a delegation-heavy fortnight doesn't ship a
# multi-thousand-row table on every 30s poll.
HISTORY_ROW_LIMIT = 50


@router.get("/delegations", response_class=Response)
async def delegations_page(request: Request) -> Response:
    """Render the delegation & cost page."""
    context = await _view_data(request)
    context["active_nav"] = "delegations"
    return request.app.state.templates.TemplateResponse(
        request,
        "delegations.html",
        context,
    )


@router.get("/delegations/panels/data", response_class=Response)
async def delegations_data_panel(request: Request) -> Response:
    """Data-sections partial, swapped in by the page's 30s htmx poll."""
    context = await _view_data(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "_delegations_data.html",
        context,
    )


# ---------------------------------------------------------------------------
# Data helpers — all fail-soft, none raise into the route.
# ---------------------------------------------------------------------------


def _empty_context() -> dict[str, Any]:
    """The fresh-install shape: every section renders _panel_empty.html."""
    return {
        "has_data": False,
        "summary": None,
        "model_split": [],
        "history": [],
        "history_total": 0,
        "history_limit": HISTORY_ROW_LIMIT,
        "daily": [],
        "window_days": DEFAULT_WINDOW_DAYS,
    }


async def _view_data(request: Request) -> dict[str, Any]:
    """Build the template context off one threadpool hop; empty on failure."""
    reader = _delegation_reader(request)
    if reader is None:
        return _empty_context()
    try:
        root = Path(request.app.state.cfg.subagent_transcript_dir)
        return await run_in_threadpool(_gather, reader, root)
    except Exception:
        return _empty_context()


def _delegation_reader(request: Request) -> DelegationReader | None:
    """Lazily build (and cache on ``app.state``) the DelegationReader.

    Shares the ``app.state.delegation_reader`` slot with the dashboard's
    cost panel (MC-02) so both views ride one warm mtime-keyed parse
    cache instead of re-parsing day-files per page.
    """
    reader = getattr(request.app.state, "delegation_reader", None)
    if reader is None:
        try:
            reader = DelegationReader.from_config(request.app.state.cfg)
        except Exception:
            return None
        request.app.state.delegation_reader = reader
    return reader


def _pct(value: float, maximum: float) -> float:
    """Percentage of ``maximum``, rounded for stable inline styles."""
    if maximum <= 0:
        return 0.0
    return round(value / maximum * 100.0, 1)


def _gather(reader: DelegationReader, root: Path) -> dict[str, Any]:
    """Run every reader query and shape the page context (sync, threadpool).

    All percentages are computed here rather than in Jinja so the
    templates stay arithmetic-free and the bar widths are deterministic
    for tests.
    """
    runs = reader.runs()
    if not runs:
        return _empty_context()

    daily = reader.daily_rollup()
    weekly = reader.weekly_rollup()
    split = reader.model_split()

    # (1) Summary strip. daily_rollup/weekly_rollup key newest-first,
    # so the first bucket of each is today / the current ISO week.
    today_bucket = next(iter(daily.values()), None) or {"runs": 0, "cost_usd": 0.0}
    week_bucket = next(iter(weekly.values()), None) or {"runs": 0, "cost_usd": 0.0}
    # runs are newest-first, so Counter insertion order breaks ties in
    # favor of the most recently active slug.
    busiest = Counter(run.slug for run in runs).most_common(1)[0][0]
    summary = {
        "today_cost": today_bucket["cost_usd"],
        "week_cost": week_bucket["cost_usd"],
        "run_count": len(runs),
        "busiest": busiest,
    }

    # (2) Per-model split rows. model_split() already orders tiers by
    # descending cost; bar widths are % of the costliest tier. The raw
    # model IDs behind each tier surface via the row's title attr.
    max_tier_cost = max((b["cost_usd"] for b in split.values()), default=0.0)
    model_split = [
        {
            "tier": tier,
            "runs": bucket["runs"],
            "cost_usd": bucket["cost_usd"],
            "pct": _pct(bucket["cost_usd"], max_tier_cost),
            "models_title": "; ".join(
                f"{raw}: {m['runs']} runs, ${m['cost_usd']:.2f}"
                for raw, m in bucket["models"].items()
            ),
        }
        for tier, bucket in split.items()
    ]

    # (3) History rows, newest first, capped. The transcript path is
    # reconstructed from the runner's on-disk layout
    # (<root>/<slug>/<date>.jsonl) and rendered as mono text — the
    # dashboard does not serve transcript files.
    history = [
        {
            "slug": run.slug,
            "tier": run.model_tier,
            "model": run.model or "",
            "date": run.date,
            "ts": run.ts,
            "duration_seconds": run.duration_seconds,
            "cost_usd": run.cost_usd,
            "tool_call_count": run.tool_call_count,
            "audit_id": run.audit_id,
            "transcript_path": str(root / run.slug / f"{run.date}.jsonl"),
        }
        for run in runs[:HISTORY_ROW_LIMIT]
    ]

    # (4) Daily strip, oldest → newest (a left-to-right timeline; the
    # rollup itself keys newest-first). Bar heights are % of the
    # costliest day; an all-zero-cost window degrades to flat bars.
    max_day_cost = max((b["cost_usd"] for b in daily.values()), default=0.0)
    daily_cells = [
        {
            "date": day,
            "label": day[8:],  # day-of-month from YYYY-MM-DD
            "runs": bucket["runs"],
            "cost_usd": bucket["cost_usd"],
            "pct": _pct(bucket["cost_usd"], max_day_cost),
        }
        for day, bucket in reversed(list(daily.items()))
    ]

    return {
        "has_data": True,
        "summary": summary,
        "model_split": model_split,
        "history": history,
        "history_total": len(runs),
        "history_limit": HISTORY_ROW_LIMIT,
        "daily": daily_cells,
        "window_days": DEFAULT_WINDOW_DAYS,
    }
