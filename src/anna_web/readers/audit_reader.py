"""Bounded reverse-tail reader over the daemon's audit JSONL files.

The daemon (writer side: :func:`anna.log.audit_event`) appends one JSON
object per line to daily files at ``cfg.audit_dir / audit-YYYY-MM-DD.jsonl``.
Each line carries at least ``ts``, ``level``, ``event``, ``actor`` and
``conv_key`` plus event-specific fields (``slug``, ``model``, ``audit_id``,
``cost_usd``, ``tool_calls``, ``duration_seconds``, ...).

This reader is the dashboard's read side. Contract:

* **Bounded.** Only today's file and the prior day's are considered
  (covers the midnight boundary for "recent activity" panes). Each file
  is tailed from the end with both a byte cap and a line cap — a whole
  file is never loaded, no matter how big a busy day gets.
* **Newest first.** Within a file the writer appends chronologically,
  so reversed line order is newest-first; today's tail precedes
  yesterday's in the result.
* **Never raises.** Missing dir or file → ``[]``. Malformed JSON lines
  are skipped. Any unexpected failure is logged and surfaces as an
  empty list, never as an exception in a route handler.
* **Cheap when idle.** Parsed tails are cached per file keyed on
  ``(mtime_ns, size)``, so an unchanged file costs one ``stat()`` per
  poll. Appends change the size, which invalidates the entry.

Synchronous and pure stdlib by design: routes call this through
``run_in_threadpool``, so there is no async here, and the reader must
not drag web-framework imports into what is effectively a file tailer.

Note on filter-vs-cap ordering: the byte/line caps bound how far back
into each file we look *before* the event-prefix filter applies. On a
very busy day a sparse event family can fall off the back of the tail
window. That is the intended trade — the reader's job is "recent
activity, cheaply", not exhaustive history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from anna.log import get_logger

# Per-file tail bounds. 256 KiB / 500 lines comfortably covers a day's
# recent activity (real lines run ~150-900 bytes) while keeping the
# worst-case read small enough to be invisible in a threadpool.
DEFAULT_MAX_BYTES = 256 * 1024
DEFAULT_MAX_LINES = 500

# Default cap on the number of events one read() returns.
DEFAULT_LIMIT = 200

_log = get_logger("anna.web.audit_reader")


@dataclass(frozen=True)
class AuditEvent:
    """One parsed audit line.

    ``ts`` and ``event`` are promoted to attributes because every
    consumer needs them (ordering, filtering, display); everything else
    the writer stamped (``level``, ``actor``, ``conv_key``, event-specific
    fields) stays in ``fields`` untyped — the dashboard renders them
    generically and must not break when the daemon grows new fields.
    """

    ts: str
    event: str
    fields: dict[str, Any] = field(default_factory=dict)


class AuditReader:
    """Read recent audit events from today's and yesterday's JSONL files.

    Instances are cheap and hold only the parse cache; the dashboard
    keeps one per app on ``app.state`` so the cache survives across
    requests. Not thread-safe for concurrent ``read()`` calls in the
    sense of cache coherence, but a stale or doubly-computed cache
    entry is harmless — worst case is one redundant tail parse.
    """

    def __init__(
        self,
        audit_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> None:
        self._audit_dir = audit_dir
        self._max_bytes = max(1, max_bytes)
        self._max_lines = max(1, max_lines)
        # path str -> ((mtime_ns, size), parsed newest-first events)
        self._cache: dict[str, tuple[tuple[int, int], list[AuditEvent]]] = {}

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def read(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        event_prefix: str | None = None,
        now: datetime | None = None,
    ) -> list[AuditEvent]:
        """Return up to ``limit`` events, newest first.

        ``event_prefix`` keeps only events whose name starts with the
        prefix (e.g. ``"audit.subagent."``). ``now`` is injectable for
        tests; production callers omit it and get UTC now, matching the
        writer's UTC-dated filenames.

        Never raises: any failure degrades to fewer (possibly zero)
        events.
        """
        try:
            return self._read(limit=limit, event_prefix=event_prefix, now=now)
        except Exception:
            # Defensive backstop. The per-file paths below already
            # swallow the expected failure modes (missing file, bad
            # line); this catches the unexpected so a dashboard poll
            # can never 500 on the audit pane.
            _log.warning("audit_reader.read_failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read(
        self,
        *,
        limit: int,
        event_prefix: str | None,
        now: datetime | None,
    ) -> list[AuditEvent]:
        if limit <= 0:
            return []
        moment = now if now is not None else datetime.now(timezone.utc)
        out: list[AuditEvent] = []
        for path in self._candidate_paths(moment):
            for event in self._read_file(path):
                if event_prefix is not None and not event.event.startswith(event_prefix):
                    continue
                out.append(event)
                if len(out) >= limit:
                    return out
        return out

    def _candidate_paths(self, moment: datetime) -> list[Path]:
        """Today's file then yesterday's — newest-first file order."""
        today = moment.date()
        return [
            self._audit_dir / f"audit-{today.isoformat()}.jsonl",
            self._audit_dir / f"audit-{(today - timedelta(days=1)).isoformat()}.jsonl",
        ]

    def _read_file(self, path: Path) -> list[AuditEvent]:
        """Return the parsed newest-first tail of one file, via the cache."""
        try:
            st = path.stat()
        except OSError:
            # Missing file (or dir) — also drop any stale cache entry
            # so a deleted-and-recreated file cannot serve old events.
            self._cache.pop(str(path), None)
            return []
        key = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
        events = self._parse_tail(path, st.st_size)
        self._cache[str(path)] = (key, events)
        return events

    def _parse_tail(self, path: Path, size: int) -> list[AuditEvent]:
        """Read at most ``max_bytes`` from the end and parse newest-first.

        When the byte cap truncates the file, the first line of the
        chunk is almost certainly partial, so it is dropped rather than
        risk parsing half a record (a partial line that happens to be
        valid JSON would silently corrupt the result).
        """
        try:
            with path.open("rb") as fp:
                if size > self._max_bytes:
                    fp.seek(size - self._max_bytes)
                    chunk = fp.read(self._max_bytes)
                    newline = chunk.find(b"\n")
                    chunk = chunk[newline + 1 :] if newline >= 0 else b""
                else:
                    chunk = fp.read()
        except OSError:
            return []

        events: list[AuditEvent] = []
        for raw in reversed(chunk.splitlines()):
            if len(events) >= self._max_lines:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                # Malformed line (torn write, garbage, bad UTF-8 —
                # JSONDecodeError and UnicodeDecodeError are both
                # ValueError). Skip; the writer is append-only so
                # neighbors are unaffected.
                continue
            if not isinstance(record, dict):
                continue
            events.append(
                AuditEvent(
                    ts=str(record.pop("ts", "")),
                    event=str(record.pop("event", "")),
                    fields=record,
                )
            )
        return events
