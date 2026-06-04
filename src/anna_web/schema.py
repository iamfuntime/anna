"""Pydantic-model → form-descriptor introspection (subtask 5).

The web dashboard renders ``anna.yaml`` as a form by walking the
pydantic schema of :class:`anna.config.AnnaConfig` and emitting one
:class:`FormField` per writable model field. Subtask 6's templates
consume that list and turn each entry into the right ``<input>``
element. Subtask 7's ``/config`` route is the caller that stitches
the two together.

Public surface is a single function::

    describe(model, values=None) -> list[FormField]

``model`` is any pydantic ``BaseModel`` class. ``values`` carries
the *current* values to pre-fill the form with — either a model
instance, a plain ``dict`` (the shape ``model_dump()`` produces), or
``None`` for "render an empty form / item template".

Field-type mapping (per the Phase 2.5 plan, "Architecture →
Schema-driven form generation"):

============================  ===================================
Annotation                     :class:`FieldKind`
============================  ===================================
``bool``                       ``CHECKBOX``
``int`` / ``float``            ``NUMBER``
``str``                        ``TEXT`` (or ``TEXTAREA`` — see below)
``Literal[...]``               ``SELECT`` (options = literal members)
``list[str]``                  ``REPEATED_TEXT``
``list[BaseModel]``            ``REPEATED_FIELDSET``
``BaseModel`` (non-list)       ``FIELDSET``
``Optional[X]`` / ``X | None`` unwrapped to ``X``; ``required=False``
============================  ===================================

Multiline-text convention: a string field renders as a ``<textarea>``
when its pydantic ``Field`` declaration carries
``json_schema_extra={"widget": "textarea"}``. The dashboard does not
overload the description text for this signal — JSON schema extras
travel with the field metadata cleanly and survive ``model_dump()``
round-trips, which makes them the canonical hint.

Excluded fields: any model field whose :class:`pydantic.fields.FieldInfo`
has ``exclude=True`` is skipped. As a belt-and-suspenders measure for
:class:`AnnaConfig`'s derived ``anna_home`` path (which is computed
from environment variables, not loaded from YAML), the names in
:data:`EXCLUDED_FIELD_NAMES` are also skipped unconditionally.

Path strings: top-level fields get ``path = name``. Nested fieldsets
prefix the parent path with a dot, e.g. ``web.enabled``. Items inside
a repeated fieldset get an indexed segment, e.g.
``identities[0].slug``. The path is what subtask 7's POST handler uses
to address a field back to its model location.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

# Field names that are present on a model class but should NEVER render
# as form inputs. ``anna_home`` is the canonical example — it's a
# computed runtime path derived from ``$ANNA_HOME``, not a YAML key.
EXCLUDED_FIELD_NAMES: frozenset[str] = frozenset({"anna_home"})


class FieldKind(str, Enum):
    """Discriminator the template layer switches on per row."""

    CHECKBOX = "checkbox"
    NUMBER = "number"
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    REPEATED_TEXT = "repeated_text"
    REPEATED_FIELDSET = "repeated_fieldset"
    FIELDSET = "fieldset"


@dataclass(frozen=True)
class FormField:
    """One row in the rendered config form.

    Attributes:
        name: The pydantic field name on the model (e.g. ``"enabled"``).
        path: Dotted path from the root (e.g. ``"web.enabled"`` or
            ``"identities[0].slug"``). Used by the POST handler to
            re-address values back to model attributes.
        label: Human-readable label derived from ``name`` by replacing
            underscores with spaces and title-casing each token.
        kind: The :class:`FieldKind` discriminator.
        value: The current value for the field, or ``None`` when no
            values were supplied. For ``REPEATED_FIELDSET`` the value
            is the list of underlying model/dict items.
        description: Help text from the field's ``Field(description=...)``.
        required: True when the annotation is non-optional.
        options: For ``SELECT``, the list of allowed Literal members
            stringified. Empty list otherwise.
        children: For ``FIELDSET``, the recursively-described inner
            fields. For ``REPEATED_FIELDSET``, one inner ``FormField``
            list per current item (already populated with values).
        item_template: For ``REPEATED_FIELDSET`` only — the shape of a
            single empty item. The template uses this for the "+ add
            row" UX so the new row has the right inputs.
        extra: The field's ``json_schema_extra`` dict (empty when unset).
            Carries opt-in rendering hints that travel with the pydantic
            field — e.g. ``{"widget": "textarea"}`` or
            ``{"warn_if_non_loopback": True}`` for the web.host safety
            affordance — without overloading ``description``.
    """

    name: str
    path: str
    label: str
    kind: FieldKind
    value: Any = None
    description: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    children: list["FormField"] = field(default_factory=list)
    item_template: list["FormField"] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def describe(
    model: type[BaseModel],
    values: dict[str, Any] | BaseModel | None = None,
) -> list[FormField]:
    """Walk ``model``'s pydantic schema, return one FormField per field.

    ``values`` pre-fills the ``value`` attribute on each returned
    :class:`FormField`. Accepts a model instance, a dict (the shape
    ``model_dump()`` produces), or ``None`` (renders an empty form).

    Excluded fields (``FieldInfo.exclude=True`` OR a name in
    :data:`EXCLUDED_FIELD_NAMES`) are skipped entirely — they neither
    appear in the returned list nor in any nested ``children``.

    The returned list preserves pydantic's declaration order so the
    rendered form matches the schema source.
    """
    return _describe_model(model, values, parent_path="")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _describe_model(
    model: type[BaseModel],
    values: dict[str, Any] | BaseModel | None,
    *,
    parent_path: str,
) -> list[FormField]:
    """Recursive walker. ``parent_path`` is "" at the root."""
    out: list[FormField] = []
    for name, info in model.model_fields.items():
        if _is_excluded(name, info):
            continue
        current = _read_value(values, name)
        path = f"{parent_path}.{name}" if parent_path else name
        out.append(_describe_field(name, info, current, path))
    return out


def _describe_field(
    name: str,
    info: FieldInfo,
    current: Any,
    path: str,
) -> FormField:
    """Build a single FormField for one pydantic field."""
    annotation, optional = _unwrap_optional(info.annotation)
    # ``is_required()`` is pydantic's authoritative answer. We still
    # respect the unwrapped-optional signal because ``X | None = None``
    # is reported required=False there too, and the override below
    # keeps a non-optional-but-defaulted field marked required-by-type
    # (Optional[X] sets required=False, a plain default does not).
    required = info.is_required() and not optional
    description = info.description or ""
    label = _humanize(name)
    extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Nested BaseModel (non-list)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        children = _describe_model(annotation, current, parent_path=path)
        return FormField(
            name=name,
            path=path,
            label=label,
            kind=FieldKind.FIELDSET,
            value=current,
            description=description,
            required=required,
            children=children,
            extra=extra,
        )

    # Literal[...] → SELECT
    if origin is Literal:
        options = [str(m) for m in args]
        return FormField(
            name=name,
            path=path,
            label=label,
            kind=FieldKind.SELECT,
            value=current,
            description=description,
            required=required,
            options=options,
            extra=extra,
        )

    # list[X]
    if origin in (list, tuple):
        inner = args[0] if args else str
        # list[BaseModel] → REPEATED_FIELDSET
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            item_template = _describe_model(inner, None, parent_path="")
            items = current if isinstance(current, list) else []
            children: list[FormField] = []
            for idx, item in enumerate(items):
                item_path = f"{path}[{idx}]"
                # The "row" itself isn't a FormField; we model each row
                # as a FIELDSET so the template can render it uniformly
                # with the standalone nested-model case.
                row_children = _describe_model(inner, item, parent_path=item_path)
                children.append(
                    FormField(
                        name=str(idx),
                        path=item_path,
                        label=f"{label} #{idx + 1}",
                        kind=FieldKind.FIELDSET,
                        value=item,
                        description="",
                        required=False,
                        children=row_children,
                    )
                )
            return FormField(
                name=name,
                path=path,
                label=label,
                kind=FieldKind.REPEATED_FIELDSET,
                value=items,
                description=description,
                required=required,
                children=children,
                item_template=item_template,
                extra=extra,
            )
        # list[str] (or any scalar list) → REPEATED_TEXT
        return FormField(
            name=name,
            path=path,
            label=label,
            kind=FieldKind.REPEATED_TEXT,
            value=current if isinstance(current, list) else [],
            description=description,
            required=required,
            extra=extra,
        )

    # Scalar types — order matters because bool is a subclass of int.
    if annotation is bool:
        return FormField(
            name=name,
            path=path,
            label=label,
            kind=FieldKind.CHECKBOX,
            value=current,
            description=description,
            required=required,
            extra=extra,
        )
    if annotation in (int, float):
        return FormField(
            name=name,
            path=path,
            label=label,
            kind=FieldKind.NUMBER,
            value=current,
            description=description,
            required=required,
            extra=extra,
        )
    if annotation is str:
        kind = (
            FieldKind.TEXTAREA
            if extra.get("widget") == "textarea"
            else FieldKind.TEXT
        )
        return FormField(
            name=name,
            path=path,
            label=label,
            kind=kind,
            value=current,
            description=description,
            required=required,
            extra=extra,
        )

    # Anything else — fall back to TEXT and let the operator type
    # something. The schema validator at write time is the safety
    # net; we'd rather render a usable input than crash the form.
    return FormField(
        name=name,
        path=path,
        label=label,
        kind=FieldKind.TEXT,
        value=current,
        description=description,
        required=required,
        extra=extra,
    )


def _is_excluded(name: str, info: FieldInfo) -> bool:
    """Field-exclusion gate: hardcoded names plus ``exclude=True`` fields."""
    if name in EXCLUDED_FIELD_NAMES:
        return True
    # ``info.exclude`` exists on FieldInfo in pydantic v2 and is
    # ``None`` when not set.
    if getattr(info, "exclude", None) is True:
        return True
    return False


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Reduce ``Optional[X]`` / ``X | None`` to ``(X, True)``.

    Returns ``(annotation, False)`` for anything that does not contain
    ``NoneType`` in its Union args. Handles both
    ``typing.Optional[X]`` (which normalizes to ``Union[X, None]``) and
    PEP 604 ``X | None`` (``types.UnionType``).
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        # Multi-arg Unions without None pass through unchanged; we
        # don't currently render those as form inputs and the
        # fallback TEXT branch handles them.
        if len(args) == len(get_args(annotation)):
            return annotation, False
        # Union with None plus multiple non-None — pick the first
        # non-None and flag optional. The dashboard does not yet
        # support discriminated unions in forms.
        return args[0], True
    return annotation, False


def _humanize(name: str) -> str:
    """snake_case → ``Snake Case``."""
    return " ".join(part.capitalize() for part in name.split("_"))


def _read_value(values: dict[str, Any] | BaseModel | None, name: str) -> Any:
    """Pull the current value for ``name`` from ``values``, or None."""
    if values is None:
        return None
    if isinstance(values, BaseModel):
        return getattr(values, name, None)
    if isinstance(values, dict):
        return values.get(name)
    return None
