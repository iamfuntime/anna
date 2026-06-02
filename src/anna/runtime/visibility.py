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

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.transports.base import InboundEvent, SignalHandle


# Cap matched substrings in the audit/log payload so a runaway capture
# (e.g. an unbounded ``.*`` user-edited pattern) cannot blow the audit
# line. The plan calls for an 80-character truncation.
_MATCH_SNIPPET_MAX = 80


class CadenceLinter:
    """Telemetry-only lint of outbound assistant text.

    Reads ``runtime.visibility.lint_patterns`` from config (a list of
    regex strings). Patterns are compiled once at init with
    :data:`re.IGNORECASE`; :meth:`lint` runs the cached patterns over
    ``text`` and emits a ``worker.cadence_lint.warn`` structured log +
    audit event per match. Never raises; never blocks delivery.

    Defense-in-depth: the config-load validator in
    :class:`anna.config.RuntimeVisibilityConfig` already rejects
    malformed regex at boot, so a :class:`re.error` here would mean
    something edited the config after load. We still wrap the compile
    in a ``ValueError`` mentioning the offending pattern so the failure
    mode is loud rather than a silent never-matching linter.
    """

    def __init__(self, *, config: AnnaConfig) -> None:
        patterns: list[tuple[str, re.Pattern[str]]] = []
        for pat in config.runtime.visibility.lint_patterns:
            try:
                patterns.append((pat, re.compile(pat, re.IGNORECASE)))
            except re.error as exc:
                raise ValueError(
                    f"CadenceLinter: invalid regex {pat!r}: {exc}"
                ) from exc
        self._patterns: list[tuple[str, re.Pattern[str]]] = patterns
        self._audit_dir = config.audit_dir
        self._fsync_on_write = config.logging.audit.fsync_on_write
        self._log = get_logger("anna.visibility.lint")

    def lint(self, text: str, *, transport: str, conv_key: str) -> None:
        """Scan ``text`` for cadence-pattern matches.

        Each match emits one ``worker.cadence_lint.warn`` structured log
        line AND one audit event of the same name carrying the source
        pattern string, the (truncated) matched substring, the conv_key,
        and the transport. Multiple matches in one text yield multiple
        distinct audit lines.

        The whole body is wrapped in a try/except that logs
        ``worker.cadence_lint.lint_failed`` at warning level on any
        unexpected error and swallows it. The linter is telemetry-only
        and MUST NOT block delivery.
        """
        try:
            for source, compiled in self._patterns:
                for match in compiled.finditer(text):
                    matched = match.group(0)
                    snippet = matched[:_MATCH_SNIPPET_MAX]
                    self._log.warning(
                        "worker.cadence_lint.warn",
                        pattern=source,
                        matched_substring=snippet,
                        conv_key=conv_key,
                        transport=transport,
                    )
                    audit_event(
                        "worker.cadence_lint.warn",
                        audit_dir=self._audit_dir,
                        actor="anna",
                        conv_key=conv_key,
                        fsync_on_write=self._fsync_on_write,
                        level="WARNING",
                        pattern=source,
                        matched_substring=snippet,
                        transport=transport,
                    )
        except Exception as exc:  # noqa: BLE001 — telemetry must never raise
            self._log.warning(
                "worker.cadence_lint.lint_failed",
                error=str(exc),
                conv_key=conv_key,
                transport=transport,
            )


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
