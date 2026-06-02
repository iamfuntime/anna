"""Cadence-visibility hooks bundle for the conversation worker.

Subtask 3 of the Cadence-Visibility Hooks plan (Inbox/2026-06-02).

This module defines the bundle the router passes into each
:class:`anna.runtime.worker.ConversationWorker` so the worker can fire
three independent visibility surfaces during ``_handle``:

* ``start`` / ``clear`` — per-transport "thinking" signal (Slack
  reaction, Telegram typing action, CLI socket frame). The router wires
  these from the corresponding :class:`anna.transports.base.ChannelAdapter`
  methods via a small factory closure; see ``router.py`` (subtask 12).
* ``lint`` — telemetry-only :class:`CadenceLinter` invoked after the
  worker assembles ``reply_text`` but before the outbound send.
* ``cadence_reminder_loader`` — zero-arg callable that returns the
  ``CADENCE.md`` core-file body so the worker can prepend it under a
  ``<system-reminder>`` block for buffered transports.

A default-noop :data:`NULL_VISIBILITY` instance lets the worker default
to today's behavior — the existing unit tests, the sub-agent runner,
and any future transport that opts out can pass this bundle without
needing to construct a stub.

The plan text labels the bundle a ``NamedTuple``; this module
implements it as a ``@dataclass(frozen=True)`` to match the rest of the
codebase (every other runtime bundle is a frozen dataclass) and to
keep the per-field type annotations grouped on the class body where
the noop wiring below can reference them via attribute access.

The :class:`CadenceLinter` is declared here as a forward-declaration
stub. Its implementation lands in subtask 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from anna.config import AnnaConfig
from anna.transports.base import InboundEvent, SignalHandle


class CadenceLinter:
    """Telemetry-only lint of outbound assistant text.

    Reads ``runtime.visibility.lint_patterns`` from config (a list of
    regex strings). On each :meth:`lint` call, runs the compiled
    patterns over ``text``; matches emit a ``worker.cadence_lint.warn``
    structured log + audit event with the matched phrase, conv_key, and
    transport. Never raises; never blocks delivery.

    Implementation lands in subtask 4 of the Cadence-Visibility Hooks
    plan. This forward-declaration stub exists so :data:`NULL_VISIBILITY`
    and the :class:`VisibilityCallbacks` type signature can reference
    the class today without introducing a circular import once the
    linter is filled in.
    """

    def __init__(self, *, config: AnnaConfig) -> None: ...

    def lint(self, text: str, *, transport: str, conv_key: str) -> None: ...


async def _noop_start(event: InboundEvent) -> SignalHandle | None:
    """Default ``VisibilityCallbacks.start`` — no signal posted."""

    return None


async def _noop_clear(handle: SignalHandle | None) -> None:
    """Default ``VisibilityCallbacks.clear`` — nothing to undo."""

    return None


@dataclass(frozen=True)
class VisibilityCallbacks:
    """Bundle of visibility hooks the worker calls during ``_handle``.

    Default-noop instance :data:`NULL_VISIBILITY` is returned when the
    router builds workers for transports/sessions where visibility is
    disabled, so unit tests and the sub-agent path skip every hook.
    """

    start: Callable[[InboundEvent], Awaitable[SignalHandle | None]]
    clear: Callable[[SignalHandle | None], Awaitable[None]]
    lint: CadenceLinter | None
    cadence_reminder_loader: Callable[[], str] | None


NULL_VISIBILITY: VisibilityCallbacks = VisibilityCallbacks(
    start=_noop_start,
    clear=_noop_clear,
    lint=None,
    cadence_reminder_loader=None,
)
