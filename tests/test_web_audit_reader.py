"""Tests for ``anna_web.readers.audit_reader.AuditReader`` (MC-03).

The load-bearing claims, per the Mission Control plan:

1. Newest-first ordering across the midnight boundary — today's tail
   precedes yesterday's, and files older than yesterday are ignored.
2. ``event_prefix`` filters by name prefix (e.g. ``"audit.subagent."``).
3. Missing audit dir (or files) → ``[]``, never an exception.
4. Malformed JSON lines are skipped without raising; neighbors survive.
5. Both bounds are honored: the per-file line cap and the per-file
   byte cap (including dropping the torn first line of a truncated
   tail).
6. The ``(mtime_ns, size)``-keyed cache serves an unchanged file
   without re-reading it — proven behaviorally by rewriting a file's
   bytes while restoring its exact mtime and size, then observing the
   stale (cached) parse.

All tests use ``tmp_path`` fixture JSONL so the operator's real
``~/anna/audit`` is never touched, and pass a fixed ``now`` into
``read()`` so the today/yesterday window is deterministic regardless
of when the suite runs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from anna_web.readers.audit_reader import AuditEvent, AuditReader

# Fixed clock: every test reads "as of" this moment, so the window is
# always audit-2026-06-10.jsonl + audit-2026-06-09.jsonl.
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    """Per-test audit directory, empty until a test seeds files."""
    d = tmp_path / "audit"
    d.mkdir()
    return d


def _record(event: str, ts: str, **fields: Any) -> dict[str, Any]:
    """One audit line shaped like the writer's output (anna.log.audit_event)."""
    return {
        "ts": ts,
        "level": "INFO",
        "event": event,
        "actor": "anna",
        "conv_key": None,
        **fields,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Append-only file shape: one JSON object per line, oldest first."""
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _day_file(audit_dir: Path, day: Any) -> Path:
    return audit_dir / f"audit-{day.isoformat()}.jsonl"


# ---------------------------------------------------------------------------
# 1. Newest-first ordering across the day boundary.
# ---------------------------------------------------------------------------


def test_newest_first_across_day_boundary(audit_dir: Path) -> None:
    """Today's events come first (reversed), then yesterday's (reversed)."""
    _write_jsonl(
        _day_file(audit_dir, YESTERDAY),
        [
            _record("audit.schedule.fire", "2026-06-09T22:00:00.000Z"),
            _record("audit.schedule.complete", "2026-06-09T23:59:00.000Z"),
        ],
    )
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [
            _record("audit.subagent.spawn", "2026-06-10T00:01:00.000Z"),
            _record("audit.subagent.complete", "2026-06-10T00:05:00.000Z"),
        ],
    )

    events = AuditReader(audit_dir).read(now=NOW)

    assert [e.event for e in events] == [
        "audit.subagent.complete",
        "audit.subagent.spawn",
        "audit.schedule.complete",
        "audit.schedule.fire",
    ]
    assert [e.ts for e in events] == sorted((e.ts for e in events), reverse=True)


def test_files_older_than_yesterday_are_ignored(audit_dir: Path) -> None:
    """The window is exactly two days; a two-day-old file never loads."""
    two_days_ago = TODAY - timedelta(days=2)
    _write_jsonl(
        _day_file(audit_dir, two_days_ago),
        [_record("audit.schedule.fire", "2026-06-08T12:00:00.000Z")],
    )
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [_record("audit.subagent.spawn", "2026-06-10T01:00:00.000Z")],
    )

    events = AuditReader(audit_dir).read(now=NOW)

    assert [e.event for e in events] == ["audit.subagent.spawn"]


def test_event_object_shape(audit_dir: Path) -> None:
    """ts/event are promoted; everything else lands in the fields dict."""
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [
            _record(
                "audit.subagent.complete",
                "2026-06-10T09:00:00.000Z",
                slug="threat-researcher",
                cost_usd=0.42,
                tool_calls=7,
                duration_seconds=79.99,
            )
        ],
    )

    (event,) = AuditReader(audit_dir).read(now=NOW)

    assert isinstance(event, AuditEvent)
    assert event.ts == "2026-06-10T09:00:00.000Z"
    assert event.event == "audit.subagent.complete"
    assert event.fields["slug"] == "threat-researcher"
    assert event.fields["cost_usd"] == 0.42
    assert event.fields["tool_calls"] == 7
    assert event.fields["actor"] == "anna"
    # Promoted keys are not duplicated inside fields.
    assert "ts" not in event.fields
    assert "event" not in event.fields


# ---------------------------------------------------------------------------
# 2. Event-prefix filtering.
# ---------------------------------------------------------------------------


def test_event_prefix_filter(audit_dir: Path) -> None:
    """Only events whose name starts with the prefix survive, order kept."""
    _write_jsonl(
        _day_file(audit_dir, YESTERDAY),
        [_record("audit.subagent.spawn", "2026-06-09T23:00:00.000Z")],
    )
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [
            _record("audit.schedule.fire", "2026-06-10T01:00:00.000Z"),
            _record("audit.subagent.complete", "2026-06-10T02:00:00.000Z"),
            _record("audit.checkpoint.written", "2026-06-10T03:00:00.000Z"),
        ],
    )

    events = AuditReader(audit_dir).read(now=NOW, event_prefix="audit.subagent.")

    assert [e.event for e in events] == [
        "audit.subagent.complete",
        "audit.subagent.spawn",
    ]


def test_event_prefix_with_no_matches_returns_empty(audit_dir: Path) -> None:
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [_record("audit.schedule.fire", "2026-06-10T01:00:00.000Z")],
    )

    assert AuditReader(audit_dir).read(now=NOW, event_prefix="audit.subagent.") == []


# ---------------------------------------------------------------------------
# 3. Missing dir / missing files → [].
# ---------------------------------------------------------------------------


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    """A box whose daemon never wrote audit yet: no dir, no exception."""
    assert AuditReader(tmp_path / "nonexistent").read(now=NOW) == []


def test_empty_dir_returns_empty(audit_dir: Path) -> None:
    assert AuditReader(audit_dir).read(now=NOW) == []


def test_unreadable_file_returns_empty(audit_dir: Path) -> None:
    """A path that stats but cannot be opened as a file degrades to [].

    Uses a directory squatting on the expected filename — ``stat()``
    succeeds, ``open()`` raises ``OSError`` — to exercise the no-raise
    contract on the read path itself.
    """
    _day_file(audit_dir, TODAY).mkdir()

    assert AuditReader(audit_dir).read(now=NOW) == []


# ---------------------------------------------------------------------------
# 4. Malformed lines are skipped; the reader never raises.
# ---------------------------------------------------------------------------


def test_malformed_lines_skipped_without_raising(audit_dir: Path) -> None:
    """Garbage between two good lines: both good lines survive."""
    good_old = _record("audit.schedule.fire", "2026-06-10T01:00:00.000Z")
    good_new = _record("audit.schedule.complete", "2026-06-10T02:00:00.000Z")
    path = _day_file(audit_dir, TODAY)
    path.write_text(
        json.dumps(good_old)
        + "\n"
        + '{"ts": "2026-06-10T01:30:00.000Z", "event": "audit.torn.wri'  # torn write
        + "\n"
        + "not json at all\n"
        + "\n"  # blank line
        + json.dumps(good_new)
        + "\n",
        encoding="utf-8",
    )

    events = AuditReader(audit_dir).read(now=NOW)

    assert [e.event for e in events] == [
        "audit.schedule.complete",
        "audit.schedule.fire",
    ]


def test_non_object_json_lines_skipped(audit_dir: Path) -> None:
    """Valid JSON that is not an object (list/scalar) is not an event."""
    path = _day_file(audit_dir, TODAY)
    path.write_text(
        "[1, 2, 3]\n42\n"
        + json.dumps(_record("audit.schedule.fire", "2026-06-10T01:00:00.000Z"))
        + "\n",
        encoding="utf-8",
    )

    events = AuditReader(audit_dir).read(now=NOW)

    assert [e.event for e in events] == ["audit.schedule.fire"]


# ---------------------------------------------------------------------------
# 5. Byte and line caps.
# ---------------------------------------------------------------------------


def test_line_cap_keeps_newest(audit_dir: Path) -> None:
    """max_lines=3 over a 10-line file → the 3 newest lines only."""
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [
            _record("audit.schedule.fire", f"2026-06-10T0{i}:00:00.000Z", seq=i)
            for i in range(10)
        ],
    )

    events = AuditReader(audit_dir, max_lines=3).read(now=NOW)

    assert [e.fields["seq"] for e in events] == [9, 8, 7]


def test_read_limit_caps_total_across_files(audit_dir: Path) -> None:
    """read(limit=N) bounds the combined result, not just one file."""
    _write_jsonl(
        _day_file(audit_dir, YESTERDAY),
        [_record("audit.schedule.fire", "2026-06-09T22:00:00.000Z", seq=0)],
    )
    _write_jsonl(
        _day_file(audit_dir, TODAY),
        [
            _record("audit.schedule.fire", f"2026-06-10T0{i}:00:00.000Z", seq=i)
            for i in range(1, 4)
        ],
    )

    events = AuditReader(audit_dir).read(now=NOW, limit=2)

    assert [e.fields["seq"] for e in events] == [3, 2]


def test_byte_cap_tails_file_and_drops_torn_first_line(audit_dir: Path) -> None:
    """A byte cap smaller than the file reads only the tail.

    The cap lands mid-line, so the truncated first line of the chunk
    must be dropped, not parsed — only complete newest lines survive.
    """
    records = [
        _record("audit.schedule.fire", f"2026-06-10T0{i}:00:00.000Z", seq=i)
        for i in range(10)
    ]
    path = _day_file(audit_dir, TODAY)
    _write_jsonl(path, records)

    # Cap to roughly the last three lines plus a partial fourth.
    lines = path.read_bytes().splitlines(keepends=True)
    cap = len(lines[-1]) + len(lines[-2]) + len(lines[-3]) + len(lines[-4]) // 2
    events = AuditReader(audit_dir, max_bytes=cap).read(now=NOW)

    assert [e.fields["seq"] for e in events] == [9, 8, 7]


def test_byte_cap_smaller_than_one_line_returns_empty(audit_dir: Path) -> None:
    """Degenerate cap: nothing complete fits in the window → no events."""
    path = _day_file(audit_dir, TODAY)
    _write_jsonl(path, [_record("audit.schedule.fire", "2026-06-10T01:00:00.000Z")])

    assert AuditReader(audit_dir, max_bytes=10).read(now=NOW) == []


# ---------------------------------------------------------------------------
# 6. mtime+size-keyed cache.
# ---------------------------------------------------------------------------


def test_cache_hit_on_unchanged_mtime_and_size(audit_dir: Path) -> None:
    """An unchanged (mtime, size) serves the cached parse — no re-read.

    Proven behaviorally: rewrite the file with different same-length
    bytes, restore the original mtime exactly, and observe that the
    second read still returns the ORIGINAL events. Only a cache hit
    can explain that.
    """
    path = _day_file(audit_dir, TODAY)
    _write_jsonl(path, [_record("audit.schedule.fire", "2026-06-10T01:00:00.000Z")])
    original_stat = path.stat()

    reader = AuditReader(audit_dir)
    first = reader.read(now=NOW)
    assert [e.event for e in first] == ["audit.schedule.fire"]

    # Same byte length, different content ("fire" -> "wire"), same mtime.
    _write_jsonl(path, [_record("audit.schedule.wire", "2026-06-10T01:00:00.000Z")])
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns

    second = reader.read(now=NOW)

    assert [e.event for e in second] == ["audit.schedule.fire"]


def test_cache_invalidated_on_append(audit_dir: Path) -> None:
    """An append changes the size, so the new tail is picked up."""
    path = _day_file(audit_dir, TODAY)
    _write_jsonl(path, [_record("audit.schedule.fire", "2026-06-10T01:00:00.000Z")])

    reader = AuditReader(audit_dir)
    assert len(reader.read(now=NOW)) == 1

    with path.open("a", encoding="utf-8") as fp:
        fp.write(
            json.dumps(_record("audit.schedule.complete", "2026-06-10T02:00:00.000Z"))
            + "\n"
        )

    events = reader.read(now=NOW)

    assert [e.event for e in events] == [
        "audit.schedule.complete",
        "audit.schedule.fire",
    ]


def test_cache_entry_dropped_when_file_disappears(audit_dir: Path) -> None:
    """A file that vanishes between polls stops serving cached events."""
    path = _day_file(audit_dir, TODAY)
    _write_jsonl(path, [_record("audit.schedule.fire", "2026-06-10T01:00:00.000Z")])

    reader = AuditReader(audit_dir)
    assert len(reader.read(now=NOW)) == 1

    path.unlink()

    assert reader.read(now=NOW) == []
