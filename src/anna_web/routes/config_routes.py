"""``/config`` routes for the ANNA web dashboard (subtask 7).

Three routes:

* ``GET /config`` — grid of every top-level section on
  :class:`anna.config.AnnaConfig`. Each card links to the per-section
  editor below.
* ``GET /config/{section}`` — schema-driven form for one section.
  Uses :func:`anna_web.schema.describe` to walk the pydantic tree,
  then renders ``config_section.html`` which recursively includes
  ``config_field.html`` for each input.
* ``POST /config/{section}`` — parse form body into a nested dict
  shaped like the section, hand off to
  :meth:`anna_web.config_store.ConfigStore.write_section`. On
  success returns the re-rendered form with a success toast (HTMX
  ``outerHTML`` swap target). On
  :class:`pydantic.ValidationError` returns 422 with the same form
  re-rendered, errors mapped onto the offending inputs by dotted
  ``loc`` path.

Same-origin enforcement is wired in :func:`anna_web.app.create_app`
via :class:`anna_web.middleware.SameOriginMiddleware`; mutating
requests without a matching ``Origin`` header are 403'd before they
reach the handlers in this module.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from anna.config import AnnaConfig
from anna_web import audit as web_audit
from anna_web.schema import FieldKind, FormField, describe


router = APIRouter(prefix="/config", tags=["config"])


# Top-level sections the dashboard exposes. anna_home is derived from
# environment, not YAML, so it's excluded.
def _allowed_sections() -> list[str]:
    return [name for name in AnnaConfig.model_fields if name != "anna_home"]


def _humanize(name: str) -> str:
    """snake_case → ``Snake Case``. Mirrors :func:`anna_web.schema._humanize`."""
    return " ".join(part.capitalize() for part in name.split("_"))


def _section_field(cfg: AnnaConfig, section: str) -> FormField:
    """Run ``describe(AnnaConfig, cfg)`` and pluck out the named section.

    Raises HTTPException(404) when the section name is not a top-level
    field on :class:`AnnaConfig`.
    """
    if section not in _allowed_sections():
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    for field in describe(AnnaConfig, cfg):
        if field.name == section:
            return field
    # Unreachable in practice — describe always emits a row per
    # non-excluded model field. Defensive 404 for the impossible
    # mismatch case.
    raise HTTPException(status_code=404, detail=f"unknown section: {section}")


# ---------------------------------------------------------------------------
# GET /config — section index
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def get_config_index(request: Request) -> Response:
    """List every top-level section as a card grid linking to the editor."""
    store = request.app.state.config_store
    cfg = store.load_validated()

    sections: list[dict[str, Any]] = []
    for field in describe(AnnaConfig, cfg):
        # Field-count hint per the plan. For FIELDSET sections the
        # child count is the natural number; for REPEATED_FIELDSET
        # (identities) report the item count + " item(s)"; for the
        # rare scalar top-level (none today, but future-proof)
        # report 1.
        if field.kind is FieldKind.FIELDSET:
            count = len(field.children)
            hint = f"{count} field{'' if count == 1 else 's'}"
        elif field.kind is FieldKind.REPEATED_FIELDSET:
            count = len(field.children)
            hint = f"{count} item{'' if count == 1 else 's'}"
        else:
            hint = field.kind.value
        sections.append(
            {
                "name": field.name,
                "label": _humanize(field.name),
                "hint": hint,
            }
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "config_index.html",
        {"sections": sections},
    )


# ---------------------------------------------------------------------------
# GET /config/{section} — editor for one section
# ---------------------------------------------------------------------------


@router.get("/{section}", response_class=HTMLResponse)
async def get_config_section(request: Request, section: str) -> Response:
    """Render the schema-driven form for one section."""
    store = request.app.state.config_store
    cfg = store.load_validated()
    field = _section_field(cfg, section)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "config_section.html",
        {
            "section": section,
            "section_label": _humanize(section),
            "field": field,
            "errors": {},
            "toast": None,
        },
    )


# ---------------------------------------------------------------------------
# POST /config/{section} — write
# ---------------------------------------------------------------------------


@router.post("/{section}", response_class=HTMLResponse)
async def post_config_section(request: Request, section: str) -> Response:
    """Validate + persist one section.

    Form keys use the dotted-path convention from
    :func:`anna_web.schema.describe`: ``web.enabled``, ``web.host``,
    ``identities[0].canonical``, etc. The handler strips the
    ``{section}.`` prefix and rebuilds the nested payload before
    handing off to
    :meth:`anna_web.config_store.ConfigStore.write_section`.

    Same-origin enforcement runs in middleware ahead of this handler.
    """
    if section not in _allowed_sections():
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")

    form = await request.form()
    # The form is multi-valued for repeated text inputs (``foo[]`` keyed
    # multiple times). Convert to a list-per-key snapshot up front so the
    # downstream parser doesn't need a Request object.
    items: list[tuple[str, str]] = []
    for key in form.keys():
        values = form.getlist(key)
        for v in values:
            # Form values arrive as str or UploadFile. We never accept
            # file uploads on this surface, so coerce to str defensively.
            items.append((key, str(v)))

    store = request.app.state.config_store
    cfg = store.load_validated()
    section_field = _section_field(cfg, section)

    payload = _build_payload(section, section_field, items)

    templates = request.app.state.templates

    try:
        new_cfg = await store.write_section(section, payload, actor="operator")
    except ValidationError as exc:
        # Re-render the form with inline errors. Map pydantic's loc
        # tuples back to the dotted paths the template's <small
        # data-field="..."> slots key on. The first loc segment is the
        # section name; everything after is the in-section path.
        errors = _errors_by_path(section, exc)
        # Audit the failed validation so the operator can see which
        # section was poked and which field paths tripped. Pydantic
        # error messages are not secrets; we surface the dotted paths
        # only (no submitted values) so the audit row doesn't leak
        # whatever the operator typed into the form.
        web_audit.emit(
            "config_validate_failed",
            request=request,
            section=section,
            errors=sorted(errors.keys()),
        )
        # Re-describe with the *attempted* values so the operator sees
        # what they submitted, not the on-disk values. The simplest
        # way to do that is to model_validate the section payload on
        # its own schema with a permissive shim — but for v1 we just
        # re-describe the current cfg (untouched, because the write
        # failed) and let the inline errors call out the bad fields.
        cfg_for_render = store.load_validated()
        rendered_field = _section_field(cfg_for_render, section)
        ctx = {
            "section": section,
            "section_label": _humanize(section),
            "field": rendered_field,
            "errors": errors,
            "toast": {
                "level": "error",
                "message": f"Validation failed for {section}. Fix the highlighted fields.",
            },
        }
        return templates.TemplateResponse(
            request,
            "config_section.html",
            ctx,
            status_code=422,
        )
    except ValueError as exc:
        # Defensive: ConfigStore.write_section raises ValueError for
        # unknown sections. We already 404'd above, so this branch
        # should be unreachable. Surface as 422 with an error toast
        # rather than a 500.
        ctx = {
            "section": section,
            "section_label": _humanize(section),
            "field": section_field,
            "errors": {},
            "toast": {"level": "error", "message": str(exc)},
        }
        return templates.TemplateResponse(
            request,
            "config_section.html",
            ctx,
            status_code=422,
        )

    # Success: re-render with the freshly-validated cfg so the form
    # reflects what actually landed (pydantic may have coerced types or
    # filled defaults). HTMX swaps the form's outerHTML with this
    # response.
    new_field = _section_field(new_cfg, section)
    # Audit the successful write. Only the section name lands in the
    # payload — never the submitted values (a config section is a
    # mix of innocuous fields and credential fragments and we have no
    # clean per-field classifier; the audit row pairs with a
    # config_store-side commit on the YAML to reconstruct the change
    # if needed).
    web_audit.emit(
        "config_write",
        request=request,
        section=section,
    )
    ctx = {
        "section": section,
        "section_label": _humanize(section),
        "field": new_field,
        "errors": {},
        "toast": {
            "level": "success",
            "message": f"Saved {section}. Restart anna.service to apply.",
        },
    }
    return templates.TemplateResponse(
        request,
        "config_section.html",
        ctx,
    )


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------


def _build_payload(
    section: str,
    section_field: FormField,
    items: list[tuple[str, str]],
) -> Any:
    """Convert ``[(name, value), ...]`` form items into the section payload.

    The leading ``{section}.`` (or ``{section}`` for a top-level
    repeated list) is stripped from each key. The remaining
    dotted/indexed path is decomposed into segments and the value
    placed into the nascent tree.

    Returns either a dict (for FIELDSET-rooted sections like ``web``
    or ``runtime``) or a list (for REPEATED_FIELDSET-rooted sections
    like ``identities``).

    Checkbox-on-the-wire semantics: HTML checkboxes that are unchecked
    do NOT post a key. The walker therefore explicitly fills in
    ``False`` for any CHECKBOX FormField whose path is absent from the
    submitted form. ``value="on"`` (or any other truthy string) maps to
    ``True``. Plain text/number fields posted with an empty string are
    forwarded as ``None`` so pydantic's Optional-field defaults take
    effect rather than choking on ``""``.
    """
    # Index incoming items by their post-prefix path.
    by_path: dict[str, list[str]] = {}
    prefix_dot = f"{section}."
    prefix_bracket = f"{section}["
    for key, value in items:
        if key.startswith(prefix_dot):
            relative = key[len(prefix_dot):]
        elif key.startswith(prefix_bracket):
            relative = key[len(section):]  # keep the leading '['
        elif key == section:
            relative = ""
        else:
            # Anything that doesn't begin with the section prefix is
            # foreign (stray field from another form, a CSRF token a
            # future subtask adds, etc.). Skip silently — the write
            # path validates the assembled payload anyway.
            continue
        by_path.setdefault(relative, []).append(value)

    # Walk the FormField tree, pulling values out of by_path as we go.
    # Checkbox fields missing from the form become False.
    return _collect(section_field, by_path, in_section_path="")


def _collect(
    field: FormField,
    by_path: dict[str, list[str]],
    in_section_path: str,
) -> Any:
    """Recursive walker producing the payload value for ``field``.

    ``in_section_path`` is the path *relative to the section root*
    (matches the keys in ``by_path``). For the top-level call this is
    "" and we descend into the section's structure.
    """
    kind = field.kind

    if kind is FieldKind.FIELDSET:
        out: dict[str, Any] = {}
        for child in field.children:
            child_path = _join(in_section_path, child)
            out[child.name] = _collect(child, by_path, child_path)
        return out

    if kind is FieldKind.REPEATED_FIELDSET:
        # Top-level REPEATED_FIELDSET (e.g. identities): scan by_path
        # for indexed keys at this level, then for each index produce
        # a dict from the item_template.
        indices = _discover_indices(by_path, in_section_path)
        items: list[dict[str, Any]] = []
        template = field.item_template or []
        for idx in indices:
            row: dict[str, Any] = {}
            row_root = f"{in_section_path}[{idx}]"
            for child in template:
                child_path = f"{row_root}.{child.name}" if child.name else row_root
                row[child.name] = _collect(child, by_path, child_path)
            items.append(row)
        return items

    if kind is FieldKind.REPEATED_TEXT:
        # The template emits name="path[]" for each row. Multiple
        # values arrive under the same form key. Empty strings drop
        # so the operator can blank a row to remove it.
        raw = by_path.get(f"{in_section_path}[]", [])
        return [v for v in raw if v != ""]

    if kind is FieldKind.CHECKBOX:
        # Unchecked checkboxes don't post. Present-with-any-value → True.
        present = in_section_path in by_path
        if not present:
            return False
        raw = by_path[in_section_path][-1]
        return raw.lower() not in ("", "false", "0", "off")

    if kind is FieldKind.NUMBER:
        raw_list = by_path.get(in_section_path, [])
        if not raw_list:
            return None
        raw = raw_list[-1]
        if raw == "":
            return None
        # Let pydantic do the int/float coercion at validate time.
        # Forwarding the raw string keeps "70000" routable to the
        # right per-field ValidationError instead of failing here
        # with a ValueError the route doesn't unwrap.
        try:
            if "." in raw or "e" in raw.lower():
                return float(raw)
            return int(raw)
        except ValueError:
            return raw

    if kind is FieldKind.SELECT:
        raw_list = by_path.get(in_section_path, [])
        if not raw_list:
            return None
        return raw_list[-1]

    # TEXT / TEXTAREA / fallback
    raw_list = by_path.get(in_section_path, [])
    if not raw_list:
        return None
    raw = raw_list[-1]
    return raw if raw != "" else None


def _join(parent: str, child: FormField) -> str:
    """Compose the in-section path for ``child`` given its parent's path."""
    if not parent:
        return child.name
    # REPEATED_FIELDSET row children use an indexed parent (already in
    # parent); just append .name.
    return f"{parent}.{child.name}"


def _discover_indices(by_path: dict[str, list[str]], root: str) -> list[int]:
    """Scan ``by_path`` keys for ``{root}[N].`` segments, return sorted [N]."""
    prefix = f"{root}[" if root else "["
    seen: set[int] = set()
    for key in by_path:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        end = rest.find("]")
        if end == -1:
            continue
        try:
            seen.add(int(rest[:end]))
        except ValueError:
            continue
    return sorted(seen)


def _errors_by_path(section: str, exc: ValidationError) -> dict[str, str]:
    """Map pydantic ``loc`` tuples to dotted paths the template keys on.

    pydantic emits ``loc`` like ``("web", "port")`` for a section we
    re-validated through AnnaConfig. We re-stringify to the same shape
    the FormField.path attribute uses (``web.port``,
    ``identities[0].canonical``) so the template's
    ``{% if errors[field.path] %}`` lookup hits.
    """
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        if not loc:
            continue
        # Normalize loc to the FormField.path shape.
        parts: list[str] = []
        for seg in loc:
            if isinstance(seg, int):
                # Append to last entry as [N], not as its own segment.
                if parts:
                    parts[-1] = f"{parts[-1]}[{seg}]"
                else:
                    parts.append(f"[{seg}]")
            else:
                parts.append(str(seg))
        path = ".".join(parts)
        out[path] = err.get("msg", "invalid value")
    return out
