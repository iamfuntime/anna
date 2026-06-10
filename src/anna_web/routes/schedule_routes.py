"""Schedule routes for the ANNA web dashboard: run board + CRUD editor.

Originally subtask 9 of the Phase 2.5 buildout; the read side was
rebuilt into the Mission Control schedule run board in MC-06. All
endpoints render server-side HTML with HTMX swaps for partial updates:

* ``GET /schedules`` — the run board (MC-06): one dense row per
  schedule with run state and the computed next fire, live-refreshed
  by an htmx poll of the tbody partial.
* ``GET /schedules/board`` — the board's tbody partial the page polls
  every 10s.
* ``GET /schedules/new`` — empty create form.
* ``GET /schedules/{id}/edit`` — pre-populated edit form.
* ``POST /schedules`` — create handler. Returns the new row partial
  on success, the form re-rendered with inline errors on 422.
* ``PUT /schedules/{id}`` — update handler with the same success /
  error shape as create.
* ``DELETE /schedules/{id}`` — remove. Returns 204; the HTMX
  caller wipes the row client-side via ``hx-swap="delete"``.

All endpoints pull the adapter off ``request.app.state.schedule_store``
so tests can swap in a tmp-anna_home adapter without touching the
underlying store wiring. Board rows are built by
:mod:`anna_web.readers.schedule_board`, which mirrors the daemon
scheduler's next-fire semantics and is fail-soft (an unreadable
``schedules.yaml`` renders an empty board, a single bad cron degrades
only its own row).

The plan's HTMX patterns section is followed: ``hx-post`` for create,
``hx-put`` for update (FastAPI routes the method natively, no form
override needed), ``hx-delete`` with ``hx-confirm`` for the destroy
button, per-field error rendering on the same form template, and an
``every 10s`` poll on the board tbody (polling, not SSE — matching the
dashboard's activity panel idiom).

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md``, "Subtasks → 9"
for the editor design and the 2026-06-10 mission-control plan, MC-06,
for the board.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from pydantic import ValidationError

from anna.runtime.schedule_store import ScheduleValidationError
from anna.runtime.schedule_types import Schedule, ScheduleDestination
from anna_web import audit as web_audit
from anna_web.readers.schedule_board import build_row, load_board_rows

router = APIRouter(prefix="/schedules", tags=["schedules"])


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


def _coerce_bool(raw: str | None) -> bool:
    """Translate an HTML checkbox value into a bool.

    Unchecked checkboxes do not submit anything, so the form parser
    sees ``None``. Checked checkboxes typically submit ``on``; some
    HTMX flows submit the literal string ``true``. Treat any
    truthy-ish value as True, anything else as False.
    """
    if raw is None:
        return False
    return raw.lower() in {"on", "true", "1", "yes", "checked"}


def _build_schedule_payload(
    *,
    id: str,
    prompt: str,
    destination_transport: str,
    destination_channel: str,
    cron: str,
    natural_language: str | None,
    timezone_name: str,
    timeout_seconds: int,
    enabled: bool,
    created_at: datetime | None = None,
) -> Schedule:
    """Build a :class:`Schedule` from form fields.

    Pydantic raises :class:`ValidationError` on any type mismatch or
    constraint violation; the route handler catches and re-renders.
    ``created_at`` is generated server-side at create time and
    preserved at update time (passed by the update handler).
    """
    return Schedule(
        id=id.strip(),
        natural_language=(natural_language or None) or None,
        cron=cron.strip(),
        timezone=timezone_name.strip() or "UTC",
        prompt=prompt,
        destination=ScheduleDestination(
            transport=destination_transport,  # type: ignore[arg-type]
            channel=destination_channel.strip(),
        ),
        timeout_seconds=timeout_seconds,
        enabled=enabled,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _form_context(
    request: Request,
    *,
    schedule: Schedule | None,
    form_values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    mode: str = "create",
) -> dict[str, Any]:
    """Build the template context the form template renders against.

    The form template reads either an existing ``schedule`` (edit
    mode) or a dict of ``values`` (create mode / re-render on error)
    plus an optional ``errors`` map keyed by field name. Centralized
    here so the create and update handlers don't drift on context
    shape.
    """
    values: dict[str, Any] = {
        "id": "",
        "prompt": "",
        "destination_transport": "slack",
        "destination_channel": "",
        "cron": "",
        "natural_language": "",
        "timezone": "UTC",
        "timeout_seconds": 300,
        "enabled": True,
    }
    if schedule is not None:
        values.update(
            {
                "id": schedule.id,
                "prompt": schedule.prompt,
                "destination_transport": schedule.destination.transport,
                "destination_channel": schedule.destination.channel,
                "cron": schedule.cron,
                "natural_language": schedule.natural_language or "",
                "timezone": schedule.timezone,
                "timeout_seconds": schedule.timeout_seconds,
                "enabled": schedule.enabled,
            }
        )
    if form_values is not None:
        # Form re-render on validation error preserves what the user
        # typed; never silently drop their input.
        values.update(form_values)
    return {
        "mode": mode,
        "values": values,
        "errors": errors or {},
    }


def _validation_errors_to_dict(exc: ValidationError) -> dict[str, str]:
    """Flatten a pydantic ValidationError into a {field: message} map.

    The form template renders ``errors.<field>`` underneath each
    input. Pydantic returns a list of errors with a ``loc`` tuple;
    we collapse to the first hop of ``loc`` (or ``__all__`` for
    model-level failures).
    """
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        if not loc:
            key = "__all__"
        else:
            head = str(loc[0])
            # Map nested destination errors onto the form field names
            # the template actually renders so the inline error shows
            # up in the right place.
            if head == "destination":
                if len(loc) > 1 and str(loc[1]) == "channel":
                    key = "destination_channel"
                elif len(loc) > 1 and str(loc[1]) == "transport":
                    key = "destination_transport"
                else:
                    key = "destination_channel"
            else:
                key = head
        out[key] = err.get("msg", "Invalid value")
    return out


# ---------------------------------------------------------------------------
# Read routes
# ---------------------------------------------------------------------------


@router.get("", response_class=Response)
@router.get("/", response_class=Response)
async def list_schedules(request: Request) -> Response:
    """Render the schedule run board (MC-06)."""
    rows = await load_board_rows(request.app.state.schedule_store)
    return request.app.state.templates.TemplateResponse(
        request,
        "schedule_list.html",
        # active_nav drives the shell nav's aria-current highlighting
        # (base.html, MC-02).
        {"rows": rows, "active_nav": "schedules"},
    )


@router.get("/board", response_class=Response)
async def board_rows(request: Request) -> Response:
    """Board tbody partial — the target of the page's 10s htmx poll.

    Returns only the ``<tr>`` set (or the empty-state colspan row) so
    the poll swaps the tbody's innerHTML without re-rendering the page
    chrome. No path conflict with ``/{schedule_id}/edit``: that route
    needs a second segment.
    """
    rows = await load_board_rows(request.app.state.schedule_store)
    return request.app.state.templates.TemplateResponse(
        request,
        "_schedule_board_rows.html",
        {"rows": rows},
    )


@router.get("/new", response_class=Response)
async def new_schedule_form(request: Request) -> Response:
    """Empty create form."""
    return request.app.state.templates.TemplateResponse(
        request,
        "schedule_form.html",
        _form_context(request, schedule=None, mode="create"),
    )


@router.get("/{schedule_id}/edit", response_class=Response)
async def edit_schedule_form(request: Request, schedule_id: str) -> Response:
    """Edit form pre-populated with an existing schedule's values."""
    store = request.app.state.schedule_store
    schedule = await store.get(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "schedule_form.html",
        _form_context(request, schedule=schedule, mode="edit"),
    )


# ---------------------------------------------------------------------------
# Write routes
# ---------------------------------------------------------------------------


@router.post("", response_class=Response)
@router.post("/", response_class=Response)
async def create_schedule(
    request: Request,
    id: Annotated[str, Form()],
    prompt: Annotated[str, Form()],
    destination_transport: Annotated[str, Form()],
    destination_channel: Annotated[str, Form()],
    cron: Annotated[str, Form()] = "",
    natural_language: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form(alias="timezone")] = "UTC",
    timeout_seconds: Annotated[int, Form()] = 300,
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    """Validate the form, persist, return the new row on success."""
    store = request.app.state.schedule_store
    templates = request.app.state.templates
    submitted = {
        "id": id,
        "prompt": prompt,
        "destination_transport": destination_transport,
        "destination_channel": destination_channel,
        "cron": cron,
        "natural_language": natural_language,
        "timezone": timezone_name,
        "timeout_seconds": timeout_seconds,
        "enabled": _coerce_bool(enabled),
    }
    try:
        schedule = _build_schedule_payload(
            id=id,
            prompt=prompt,
            destination_transport=destination_transport,
            destination_channel=destination_channel,
            cron=cron,
            natural_language=natural_language or None,
            timezone_name=timezone_name,
            timeout_seconds=timeout_seconds,
            enabled=_coerce_bool(enabled),
        )
    except ValidationError as exc:
        ctx = _form_context(
            request,
            schedule=None,
            form_values=submitted,
            errors=_validation_errors_to_dict(exc),
            mode="create",
        )
        return templates.TemplateResponse(
            request, "schedule_form.html", ctx, status_code=422
        )

    try:
        created = await store.create(schedule)
    except ScheduleValidationError as exc:
        ctx = _form_context(
            request,
            schedule=None,
            form_values=submitted,
            errors={"__all__": str(exc)},
            mode="create",
        )
        return templates.TemplateResponse(
            request, "schedule_form.html", ctx, status_code=422
        )

    web_audit.emit(
        "schedule_create",
        request=request,
        schedule_id=created.id,
    )
    # The row partial renders board rows (MC-06), so the swap payload
    # carries the same computed columns (next fire, displays) the
    # board's poll would deliver.
    return templates.TemplateResponse(
        request,
        "schedule_row.html",
        {"row": build_row(created)},
        status_code=201,
    )


@router.put("/{schedule_id}", response_class=Response)
async def update_schedule(
    request: Request,
    schedule_id: str,
    prompt: Annotated[str, Form()],
    destination_transport: Annotated[str, Form()],
    destination_channel: Annotated[str, Form()],
    cron: Annotated[str, Form()] = "",
    natural_language: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form(alias="timezone")] = "UTC",
    timeout_seconds: Annotated[int, Form()] = 300,
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    """Validate + update + return the swapped-in row on success."""
    store = request.app.state.schedule_store
    templates = request.app.state.templates
    existing = await store.get(schedule_id)
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"Schedule '{schedule_id}' not found"
        )

    submitted = {
        "id": schedule_id,
        "prompt": prompt,
        "destination_transport": destination_transport,
        "destination_channel": destination_channel,
        "cron": cron,
        "natural_language": natural_language,
        "timezone": timezone_name,
        "timeout_seconds": timeout_seconds,
        "enabled": _coerce_bool(enabled),
    }
    try:
        schedule = _build_schedule_payload(
            id=schedule_id,
            prompt=prompt,
            destination_transport=destination_transport,
            destination_channel=destination_channel,
            cron=cron,
            natural_language=natural_language or None,
            timezone_name=timezone_name,
            timeout_seconds=timeout_seconds,
            enabled=_coerce_bool(enabled),
            created_at=existing.created_at,
        )
    except ValidationError as exc:
        ctx = _form_context(
            request,
            schedule=existing,
            form_values=submitted,
            errors=_validation_errors_to_dict(exc),
            mode="edit",
        )
        return templates.TemplateResponse(
            request, "schedule_form.html", ctx, status_code=422
        )

    try:
        updated = await store.update(schedule_id, schedule)
    except ScheduleValidationError as exc:
        ctx = _form_context(
            request,
            schedule=existing,
            form_values=submitted,
            errors={"__all__": str(exc)},
            mode="edit",
        )
        return templates.TemplateResponse(
            request, "schedule_form.html", ctx, status_code=422
        )

    web_audit.emit(
        "schedule_update",
        request=request,
        schedule_id=schedule_id,
    )
    return templates.TemplateResponse(
        request,
        "schedule_row.html",
        {"row": build_row(updated)},
        status_code=200,
    )


@router.delete("/{schedule_id}", response_class=Response)
async def delete_schedule(request: Request, schedule_id: str) -> Response:
    """Remove a schedule. Returns 204 on success, 404 if missing."""
    store = request.app.state.schedule_store
    try:
        await store.delete(schedule_id)
    except ScheduleValidationError as exc:
        # Underlying store raises a validation error for missing id;
        # map to 404 to match REST expectations.
        raise HTTPException(status_code=404, detail=str(exc)) from None
    web_audit.emit(
        "schedule_delete",
        request=request,
        schedule_id=schedule_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
