"""``/tasks`` route for the config-gated TaskNote pipeline board.

This router is NOT registered unconditionally in
:func:`anna_web.app.create_app` like its siblings — it mounts only
through the :mod:`anna_web.integrations` registry when the operator
flips ``integrations.obsidian.enabled`` + ``tasknotes_enabled`` (and
restarts anna-web). With defaults the module is never imported and
``/tasks`` 404s; that gating is the mission-control plan's subtask-8
done condition.

GET-only: the board is a read-only observability surface, exempt from
:class:`anna_web.middleware.SameOriginMiddleware` (which only gates
mutating methods) — same posture as ``/healthz``. Subtask 9 lands the
TaskNote reader and replaces the empty ``columns`` context with the
open/in-progress/review/done columns parsed from vault frontmatter;
the fail-soft contract carries forward (missing or invalid
``tasknotes_path`` renders the empty state, never a 500).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_class=HTMLResponse)
async def get_tasks_board(request: Request) -> Response:
    """Render the TaskNote board (empty state until subtask 9 lands the reader)."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "tasks_board.html",
        {"columns": []},
    )
