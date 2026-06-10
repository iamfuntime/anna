"""``/tasks`` routes for the config-gated TaskNote pipeline board (MC-09).

This router is NOT registered unconditionally in
:func:`anna_web.app.create_app` like its siblings — it mounts only
through the :mod:`anna_web.integrations` registry when the operator
flips ``integrations.obsidian.enabled`` + ``tasknotes_enabled`` (and
restarts anna-web). With defaults the module is never imported and
``/tasks`` 404s; that gating is MC-08's done condition and stays
pinned by tests/test_web_integrations.py.

Two endpoints, both GET-only (READ-ONLY v1 — the board observes the
vault, it never writes back):

* ``GET /tasks`` — the full page: four kanban columns (Open / In
  progress / Review / Done) plus a quiet fold-out for notes whose
  status didn't match any bucket.
* ``GET /tasks/board`` — the columns partial the page's htmx poll
  re-fetches every 15s (same polling idiom as the schedule board's
  ``/schedules/board``, MC-06).

All data comes from :class:`TaskNoteReader`, which is synchronous and
disk-bound, so both routes call it via ``run_in_threadpool``.

Read-path contract (mission-control plan, "graceful degradation"):
reader construction failure, an unset/missing ``tasknotes_path``, or
an unexpected exception mid-scan all degrade to the empty context and
the template renders the "No task data yet" state. The page never
500s.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from anna_web.readers.tasknote_reader import (
    DONE_COLUMN_CAP,
    TaskNote,
    TaskNoteReader,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Priorities rendered with the warn badge; anything else non-normal
# (e.g. "low") gets the idle badge. "normal" renders no badge at all.
_ESCALATED_PRIORITIES = frozenset({"high", "urgent", "critical"})


@router.get("", response_class=HTMLResponse)
async def get_tasks_board(request: Request) -> Response:
    """Render the TaskNote pipeline board page."""
    context = await _view_data(request)
    context["active_nav"] = "tasks"
    return request.app.state.templates.TemplateResponse(
        request,
        "tasks_board.html",
        context,
    )


@router.get("/board", response_class=HTMLResponse)
async def get_tasks_board_partial(request: Request) -> Response:
    """Columns partial, swapped in by the page's 15s htmx poll."""
    context = await _view_data(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "_tasks_board.html",
        context,
    )


# ---------------------------------------------------------------------------
# Data helpers — all fail-soft, none raise into the route.
# ---------------------------------------------------------------------------


def _empty_context() -> dict[str, Any]:
    """The no-data shape: the template renders 'No task data yet'."""
    return {
        "has_data": False,
        "columns": [],
        "other": [],
        "done_total": 0,
        "done_limit": DONE_COLUMN_CAP,
    }


async def _view_data(request: Request) -> dict[str, Any]:
    """Build the template context off one threadpool hop; empty on failure."""
    reader = _tasknote_reader(request)
    if reader is None:
        return _empty_context()
    try:
        return await run_in_threadpool(_gather, reader, date.today())
    except Exception:
        return _empty_context()


def _tasknote_reader(request: Request) -> TaskNoteReader | None:
    """Lazily build (and cache on ``app.state``) the TaskNoteReader.

    Cached so the reader's mtime-keyed parse cache survives across the
    15s polls instead of re-parsing every note per request.
    ``from_config`` returning ``None`` (gate not passing — only
    reachable if the router was mounted by hand) is not cached; the
    cheap re-probe per request keeps the failure mode simple.
    """
    reader = getattr(request.app.state, "tasknote_reader", None)
    if reader is None:
        try:
            reader = TaskNoteReader.from_config(request.app.state.cfg)
        except Exception:
            return None
        if reader is None:
            return None
        request.app.state.tasknote_reader = reader
    return reader


def _age_label(created: str, today: date) -> str:
    """Compact age chip text from an ISO created date: "today" / "12d"."""
    if len(created) < 10:
        return ""
    try:
        created_day = date.fromisoformat(created[:10])
    except ValueError:
        return ""
    days = (today - created_day).days
    return "today" if days <= 0 else f"{days}d"


def _card(note: TaskNote, today: date) -> dict[str, Any]:
    """Shape one note for task_card.html (templates stay logic-light)."""
    priority = note.priority
    return {
        "title": note.title,
        "filename": note.filename,
        "status": note.status,
        "bucket": note.bucket,
        "assignee": note.assignee,
        "priority": priority,
        "show_priority": bool(priority) and priority != "normal",
        "priority_class": (
            "badge-warn" if priority in _ESCALATED_PRIORITIES else "badge-idle"
        ),
        "age": _age_label(note.created, today),
        "created": note.created,
    }


def _gather(reader: TaskNoteReader, today: date) -> dict[str, Any]:
    """Run the reader scan and shape the page context (sync, threadpool)."""
    board = reader.board()
    if board is None:
        return _empty_context()
    columns = [
        {"key": key, "label": label, "tasks": [_card(n, today) for n in notes]}
        for key, label, notes in (
            ("open", "Open", board.open),
            ("in-progress", "In progress", board.in_progress),
            ("review", "Review", board.review),
            ("done", "Done", board.done),
        )
    ]
    return {
        "has_data": board.total > 0,
        "columns": columns,
        "other": [_card(n, today) for n in board.other],
        "done_total": board.done_total,
        "done_limit": DONE_COLUMN_CAP,
    }
