"""Audit event emission for the web dashboard.

Wraps :func:`anna.log.audit_event` with dashboard-specific defaults
(``actor="operator"``, ``via="web"``, ``request_id`` pulled from
``request.state`` when the same-origin middleware has tagged it) so
route handlers can emit events with one short call.

Critical rule: secret VALUES never appear in audit payloads. The
EnvStore ``set``/``delete``/``reveal`` paths emit events with the key
name only; the value is dropped at this layer rather than relied upon
at the route layer. This module is the load-bearing safety net: any
caller that accidentally passes a value-shaped field name gets it
stripped (and a warning logged) before the record is handed to
:func:`anna.log.audit_event`. Documented here as the load-bearing
safety property so future refactors do not relax it.

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md`` —
"Architecture → Audit events" — for the full set of
``audit.web.dashboard.*`` event names the dashboard emits.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger


# Module-level constant: any field whose key matches one of these
# names is stripped (and a warning logged) before the record reaches
# :func:`anna.log.audit_event`. This is belt-and-suspenders against a
# future refactor of a route handler that accidentally forwards a
# secret value into the audit payload. The route layer's primary
# discipline is to never construct such a field in the first place;
# this set is the safety net that catches the regression.
RESERVED_VALUE_FIELDS: frozenset[str] = frozenset(
    {"value", "secret_value", "password", "token"}
)

_EVENT_PREFIX = "audit.web.dashboard."

_log = get_logger("anna.web.audit")


def _resolve_audit_dir(request: Request | None) -> Any:
    """Pull the audit directory from the request's app state.

    The app factory parks the loaded :class:`AnnaConfig` on
    ``app.state.cfg`` at construction. Routes receive a :class:`Request`,
    which carries ``request.app`` (the FastAPI instance), so we can
    derive the audit dir without dragging a config dependency through
    every emit call site.

    Returns ``None`` when no request is supplied (e.g. boot/shutdown
    events fired from the lifespan handler before any request lands).
    The caller deals with the ``None`` branch by writing into a
    pre-resolved default; see :func:`emit`.
    """
    if request is None:
        return None
    cfg = getattr(request.app.state, "cfg", None)
    if isinstance(cfg, AnnaConfig):
        return cfg.audit_dir
    return None


def _strip_reserved_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop any field whose key is in :data:`RESERVED_VALUE_FIELDS`.

    Emits a warning per stripped field so a regression is loud in the
    operational stream — silently dropping would still meet the
    no-secrets-in-audit guarantee but would hide the bug that asked us
    to drop the field in the first place.
    """
    cleaned: dict[str, Any] = {}
    for key, value in fields.items():
        if key in RESERVED_VALUE_FIELDS:
            _log.warning(
                "audit.web.dashboard.reserved_field_stripped",
                field=key,
            )
            continue
        cleaned[key] = value
    return cleaned


def emit(
    event: str,
    *,
    request: Request | None = None,
    actor: str = "operator",
    audit_dir: Any = None,
    cfg: AnnaConfig | None = None,
    **fields: Any,
) -> None:
    """Emit one dashboard audit event.

    Parameters
    ----------
    event:
        Short tag like ``"config_write"``. The module prefixes
        ``"audit.web.dashboard."`` automatically so callers never type
        the prefix and we cannot drift on it.
    request:
        Optional :class:`fastapi.Request`. When supplied, the wrapper
        pulls ``audit_dir`` from ``request.app.state.cfg.audit_dir``
        and includes ``request_id`` (if the same-origin middleware
        tagged it on ``request.state``) in the payload.
    actor:
        Defaults to ``"operator"`` since the v1 dashboard has no other
        actor model. Callers can override (e.g. for system-driven
        events from the lifespan handler).
    audit_dir:
        Explicit override for the audit directory. Required when
        ``request`` and ``cfg`` are both ``None`` — e.g. boot/shutdown
        events emitted from the entry point or the lifespan handler
        before the first request lands.
    cfg:
        Explicit :class:`AnnaConfig` override. Used by boot/shutdown
        emits that have a cfg in hand but no Request.
    **fields:
        Arbitrary metadata stamped on the event. Field names that
        appear in :data:`RESERVED_VALUE_FIELDS` are dropped (with a
        warning) before the call reaches :func:`anna.log.audit_event`.
        Callers are responsible for not constructing such fields in
        the first place — this strip is a safety net, not a primary
        defense.
    """
    cleaned = _strip_reserved_fields(fields)

    # Stamp the canonical via="web" marker. Operator-facing audit logs
    # filter on this field to slice dashboard activity from daemon
    # activity without parsing event names.
    cleaned.setdefault("via", "web")

    # request_id from middleware-tagged state, if any.
    if request is not None:
        request_id = getattr(request.state, "request_id", None)
        if request_id is not None:
            cleaned.setdefault("request_id", request_id)

    # Resolve audit_dir: explicit override > cfg.audit_dir > request.app.state.cfg.audit_dir.
    resolved_dir = audit_dir
    if resolved_dir is None and cfg is not None:
        resolved_dir = cfg.audit_dir
    if resolved_dir is None:
        resolved_dir = _resolve_audit_dir(request)
    if resolved_dir is None:
        # Defensive: no anchor to write under. Log a warning and bail
        # rather than crash the route. The operational stream still
        # carries the event via the log mirror inside audit_event,
        # but with no path we cannot land the JSONL row, so we surface
        # this as a soft failure here. Use ``event_name`` rather than
        # ``event`` to avoid structlog's reserved-keyword collision.
        _log.warning(
            "audit.web.dashboard.no_audit_dir",
            event_name=event,
        )
        return

    audit_event(
        _EVENT_PREFIX + event,
        audit_dir=resolved_dir,
        actor=actor,
        **cleaned,
    )
