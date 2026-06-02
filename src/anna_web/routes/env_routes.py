"""``/env`` route surface for the ANNA web dashboard.

Subtask 8 of the Phase 2.5 buildout. This module owns the four HTTP
endpoints the operator hits when editing ``$ANNA_HOME/.env`` through
the web surface:

* ``GET /env`` — masked list of documented variables, plus a free-form
  "extras" section for any keys the operator set outside the
  documented allow-list.
* ``GET /env/{key}/reveal`` — plain-text response with the actual
  value. The ``static/app.js`` reveal-toggle handler from subtask 6
  fetches this on click and stuffs the response into the input.
* ``POST /env`` — write or update one variable. HTMX partial swap on
  success; 422 + toast on a typo against the documented allow-list;
  500 + generic toast on anything else.
* ``DELETE /env/{key}`` — remove a variable. 204 + empty body so HTMX
  ``hx-swap="delete"`` removes the row in place.

All four endpoints pull the :class:`anna_web.env_store.EnvStore`
singleton off ``request.app.state.env_store`` — the factory in
:mod:`anna_web.app` constructs it once per process.

Audit-event emission for ``audit.web.dashboard.secret_*`` is OUT OF
SCOPE for this subtask. Subtask 12 wires
:func:`anna.log.audit_event` into the route layer; the ``actor``
placeholders below are the seam those calls will hang off.

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md``, "Architecture →
EnvStore — secrets handling" and "HTMX patterns" for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

router = APIRouter(prefix="/env", tags=["env"])


@dataclass(frozen=True)
class _ExtraVar:
    """Stand-in for a :class:`DocumentedVar` for non-documented keys.

    The ``env_row.html`` partial reads ``name``, ``label``, ``kind``,
    and ``description`` off whatever variable record it's handed. For
    documented keys that's a real :class:`DocumentedVar`; for extras
    we synthesize this lightweight stub with ``kind="secret"`` so the
    operator's manual additions are masked by default (safer fallback
    — assume sensitive until proven otherwise).
    """

    name: str
    label: str
    kind: str = "secret"
    description: str = ""


def _render_row(
    request: Request,
    var: object,
    *,
    has_value: bool,
    value: str = "",
    is_extra: bool = False,
) -> HTMLResponse:
    """Render one ``env_row.html`` row as a standalone HTML response.

    Shared between the row-rendering branches in ``GET /env`` (via the
    template's ``include`` mechanism) and the ``POST /env`` happy path
    (where we return only the updated row for HTMX ``outerHTML`` swap).
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "env_row.html",
        {
            "var": var,
            "has_value": has_value,
            "value": value,
            "is_extra": is_extra,
        },
    )


def _render_toast(request: Request, level: str, message: str) -> str:
    """Render the ``_toast.html`` partial to a string for inline embed.

    The success/error/server-error branches in ``POST /env`` return a
    row partial OR a 422/500 status plus the toast partial. Centralized
    here so future copy-edits to the toast shape land in one place.
    """
    templates = request.app.state.templates
    template = templates.env.get_template("_toast.html")
    return template.render(level=level, message=message)


@router.get("", response_class=HTMLResponse)
async def get_env_form(request: Request) -> Response:
    """Render the masked-list editor at ``GET /env``.

    Two sections: documented variables first (one row per
    :class:`DocumentedVar` from the env store) then extras (every key
    in the dotenv that's not in the documented allow-list).

    Masking discipline: documented secrets render with ``value=""``
    plus ``data-has-value`` so the HTML response never carries the
    secret. Documented text vars render their value visibly. Extras
    are treated as secrets by default — operator's manual additions
    might be anything, so default to safe.
    """
    env_store = request.app.state.env_store
    templates = request.app.state.templates

    documented = env_store.documented_vars()
    documented_names = {v.name for v in documented}
    current = env_store.load()

    # Build per-row context tuples: (var, has_value, value).
    # Documented secrets: value blanked at render time. Documented text
    # vars: value passed through visible.
    documented_rows = []
    for var in documented:
        actual = current.get(var.name)
        has_value = actual is not None and actual != ""
        # Text vars expose the value; secret vars never do (the input
        # template branches on ``var.kind`` and ignores ``value`` for
        # secrets, but we don't even hand it the secret here as
        # defense-in-depth against future template edits).
        row_value = actual or "" if var.kind == "text" else ""
        documented_rows.append(
            {"var": var, "has_value": has_value, "value": row_value, "is_extra": False}
        )

    # Extras: any key currently set that the documented list doesn't
    # cover. Synthesize an _ExtraVar so the partial's interface stays
    # uniform; mark is_extra=True so the template renders the remove
    # button and the hidden ``is_extra`` form field.
    extra_rows = []
    for name, _value in current.items():
        if name in documented_names:
            continue
        extra_rows.append(
            {
                "var": _ExtraVar(name=name, label=name),
                "has_value": True,
                "value": "",  # always masked
                "is_extra": True,
            }
        )

    return templates.TemplateResponse(
        request,
        "env_form.html",
        {
            "documented_rows": documented_rows,
            "extra_rows": extra_rows,
        },
    )


@router.get("/{key}/reveal", response_class=PlainTextResponse)
async def reveal_env(key: str, request: Request) -> Response:
    """Return the raw value for one key as ``text/plain``.

    The ``static/app.js`` reveal handler calls this via ``fetch()`` and
    stuffs the response into the masked input. We accept both
    documented and extra keys — the operator clicked reveal, they get
    the value if it exists. 404 if the key is unset.

    NOTE: audit emit lands in subtask 12. When it does, this is the
    seam — emit ``audit.web.dashboard.secret_reveal`` with the key
    name and ``actor="operator"`` (NEVER the value).
    """
    env_store = request.app.state.env_store
    value = env_store.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"unknown env key: {key!r}")
    # actor placeholder; audit emit lands in subtask 12.
    _actor = "operator"  # noqa: F841
    return PlainTextResponse(value)


@router.post("", response_class=HTMLResponse)
async def post_env(
    request: Request,
    key: str = Form(...),
    value: str = Form(""),
    is_extra: str | None = Form(None),
) -> Response:
    """Write or update one variable.

    Body shape (URL-encoded form): ``key=<KEY>&value=<VALUE>`` plus an
    optional ``is_extra=true`` checkbox that flips the EnvStore's
    ``allow_unknown`` guard for the free-form extras section.

    Success: 200 + the updated row rendered via ``env_row.html`` for
    HTMX ``outerHTML`` swap, plus an embedded success toast.

    Unknown key without ``is_extra``: 422 + error toast. The EnvStore
    raises :class:`ValueError` in this case; we map it to the typo
    surface the operator most likely meant.

    Anything else: 500 + generic toast. Defensive arm so a dotenv-
    library blow-up surfaces as an actionable message rather than a
    bare traceback in the operator's browser console.

    NOTE: audit emit lands in subtask 12. ``audit.web.dashboard.secret_write``
    fires here with key + actor + NEVER the value.
    """
    env_store = request.app.state.env_store
    allow_unknown = _is_extra_truthy(is_extra)
    # actor placeholder; audit emit lands in subtask 12.
    _actor = "operator"  # noqa: F841

    try:
        env_store.set(key, value, allow_unknown=allow_unknown)
    except ValueError:
        toast = _render_toast(
            request,
            "error",
            f"Unknown key {key!r} — check Extras to add.",
        )
        return HTMLResponse(toast, status_code=422)
    except Exception:  # pragma: no cover - defensive
        toast = _render_toast(
            request,
            "error",
            "Could not write the value. See server logs.",
        )
        return HTMLResponse(toast, status_code=500)

    # Resolve the variable record for the response render. Documented
    # keys come back as a real DocumentedVar so the row carries label
    # + description; extras get the stub.
    documented = {v.name: v for v in env_store.documented_vars()}
    if key in documented:
        var: object = documented[key]
        is_extra_render = False
    else:
        var = _ExtraVar(name=key, label=key)
        is_extra_render = True

    kind = getattr(var, "kind", "secret")
    has_value = bool(value)
    # Text vars surface the value back to the operator; secrets stay
    # masked even right after a write — the operator just typed it, no
    # need to echo it back.
    row_value = value if kind == "text" else ""

    row_html = _render_row(
        request,
        var,
        has_value=has_value,
        value=row_value,
        is_extra=is_extra_render,
    ).body.decode("utf-8")
    toast = _render_toast(request, "success", f"Saved {key}.")
    return HTMLResponse(row_html + toast)


@router.delete("/{key}")
async def delete_env(key: str, request: Request) -> Response:
    """Remove a variable; 204 + empty body for HTMX ``hx-swap="delete"``.

    ``allow_unknown=True`` by default because the operator is
    explicitly removing the key — if it's an extra that the documented
    allow-list never knew about, we still let it be removed. The
    EnvStore's ``unset_key`` is a no-op when the key is absent so we
    don't need a pre-flight existence check.

    NOTE: audit emit lands in subtask 12. ``audit.web.dashboard.secret_delete``
    fires here with key + actor.
    """
    env_store = request.app.state.env_store
    # actor placeholder; audit emit lands in subtask 12.
    _actor = "operator"  # noqa: F841
    env_store.delete(key, allow_unknown=True)
    return Response(status_code=204)


def _is_extra_truthy(raw: str | None) -> bool:
    """Coerce the ``is_extra`` form field to a bool.

    HTML checkboxes round-trip as either ``"on"``, ``"true"``, or the
    literal field-value attribute. Accept the common variants; treat
    missing/empty/falsy as False so an absent checkbox in the
    documented-row form doesn't accidentally flip the allow-unknown
    guard off.
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"on", "true", "1", "yes"}
