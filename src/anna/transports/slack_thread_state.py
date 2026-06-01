"""Persistence for the Slack thread-participation set.

When ANNA posts a reply in a Slack channel thread, that ``(channel_id,
thread_ts)`` pair is recorded as "participated". Subsequent operator
messages in the same thread route to her worker without requiring a
fresh ``@anna`` mention. The set is persisted to a JSON Lines file so
the participation survives process restarts — otherwise an operator
continuing a yesterday-thread would have to re-mention to wake her up.

The file is append-only on every mark. Loading is forgiving: blank
lines are skipped, malformed JSON lines are skipped with a WARNING log
(a process killed mid-write can leave a truncated final line), and a
missing file is fine. Compaction is a housekeeping concern handled
elsewhere.

State file format — one JSON object per line:

.. code-block:: json

    {"channel_id": "C0AFD2LM38R", "thread_ts": "1780340901.326149",
     "first_post_ts": "2026-06-01T15:58:42.000000+00:00"}

See ``Brain/TaskNotes/Tasks/ANNA Slack - Channel thread follow-up
without @mention.md`` for the design.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from anna.log import get_logger


class ThreadParticipation:
    """Tracks which ``(channel_id, thread_ts)`` pairs ANNA has posted in.

    Once ANNA posts in a thread, that thread is "participated" and
    subsequent operator messages in it should route to her without an
    ``@-mention``.

    State persists to a JSON Lines file so the participation set
    survives process restarts.
    """

    def __init__(self, *, state_path: Path) -> None:
        self._state_path = state_path
        self._log = get_logger("anna.transport.slack.threads")
        self._set: set[tuple[str, str]] = set()
        self._write_lock = asyncio.Lock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    async def load(self) -> None:
        """Read all entries from disk into the in-memory set. Idempotent.

        A missing file is fine — the set starts empty. Blank lines and
        lines that fail JSON-decode are skipped with a WARNING; the
        file is append-only, so a corrupted last line (process killed
        mid-write) shouldn't poison loading.
        """
        if not self._state_path.exists():
            self._set = set()
            return

        loaded: set[tuple[str, str]] = set()
        try:
            text = self._state_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._log.warning(
                "slack.thread_participation.read_failed",
                path=str(self._state_path),
                error=str(exc),
            )
            self._set = set()
            return

        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                self._log.warning(
                    "slack.thread_participation.bad_line",
                    path=str(self._state_path),
                    line_number=line_number,
                    error=str(exc),
                )
                continue
            channel_id = record.get("channel_id")
            thread_ts = record.get("thread_ts")
            if not isinstance(channel_id, str) or not isinstance(thread_ts, str):
                self._log.warning(
                    "slack.thread_participation.bad_record",
                    path=str(self._state_path),
                    line_number=line_number,
                )
                continue
            loaded.add((channel_id, thread_ts))

        self._set = loaded
        self._log.info(
            "slack.thread_participation.loaded",
            count=len(self._set),
            path=str(self._state_path),
        )

    async def mark(self, *, channel_id: str, thread_ts: str) -> None:
        """Add ``(channel_id, thread_ts)`` to the in-memory set and
        append a JSON line to the state file.

        No-op if already present in-memory; we don't re-append on every
        outbound. The asyncio lock serializes file writes so concurrent
        sends to different threads don't interleave bytes.
        """
        key = (channel_id, thread_ts)
        if key in self._set:
            return

        async with self._write_lock:
            # Re-check inside the lock to handle the rare race where two
            # coroutines see "not present" before either has appended.
            if key in self._set:
                return

            record = {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "first_post_ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(record, ensure_ascii=False)
                with self._state_path.open("a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
            except OSError as exc:
                self._log.error(
                    "slack.thread_participation.write_failed",
                    path=str(self._state_path),
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    error=str(exc),
                )
                # Don't add to the in-memory set on disk failure — we
                # want a restart to behave the same as the on-disk
                # state, and we want a retry on the next outbound to
                # have another shot.
                return

            self._set.add(key)
            self._log.debug(
                "slack.thread_participation.marked",
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

    def has(self, *, channel_id: str, thread_ts: str) -> bool:
        """Sync membership check — runs hot in the message-dispatch
        path, no I/O.
        """
        return (channel_id, thread_ts) in self._set
