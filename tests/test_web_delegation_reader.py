"""Tests for the Mission Control DelegationReader (subtask 4).

Fixture trees mirror the on-disk shape the sub-agent runner writes:
``<root>/<slug>/<YYYY-MM-DD>.jsonl`` with ``task``/``outbound``/``fail``
direction lines, where only ``outbound`` lines carry the cost trailer
(``cost_usd``, ``duration_seconds``, ``tool_calls``, ``model``,
``audit_id``) — see ``SubAgentRunner._write_transcript_line``.

Covered done-conditions: per-agent aggregation, per-model
(fable/opus/sonnet) split, daily/weekly buckets, window bounding,
missing-dir zero behavior, no-raise on malformed lines, and the
mtime+size-keyed per-file cache.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from anna_web.readers.delegation_reader import (
    DEFAULT_WINDOW_DAYS,
    DelegationReader,
    normalize_model,
)

# Fixed anchor so the tests never depend on the wall clock.
TODAY = date(2026, 6, 10)


def _outbound(
    *,
    ts: str = "2026-06-10T12:00:00+00:00",
    model: str | None = "claude-fable-5",
    cost: Any = 1.0,
    duration: Any = 10.0,
    tools: Any = ("Read", "Grep"),
    audit_id: str = "aid-1",
    **extra: Any,
) -> dict[str, Any]:
    """One outbound transcript record in the runner's on-disk shape."""
    record: dict[str, Any] = {
        "ts": ts,
        "direction": "outbound",
        "conv_key": f"subagent:fixture:{audit_id}",
        "text": "done",
        "audit_id": audit_id,
        "parent_conv": "user:seth",
        "duration_seconds": duration,
        "cost_usd": cost,
        "tool_calls": list(tools) if isinstance(tools, tuple) else tools,
    }
    if model is not None:
        record["model"] = model
    record.update(extra)
    return record


def _write_day(root: Path, slug: str, day: date, lines: list[Any]) -> Path:
    """Append fixture lines (dicts → JSON, str → verbatim) to a day-file."""
    path = root / slug / f"{day.isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        for line in lines:
            fp.write(line if isinstance(line, str) else json.dumps(line))
            fp.write("\n")
    return path


def test_reader_ignores_gzipped_day_files(tmp_path: Path) -> None:
    """The retention sweep gzips day-files at 30d; the reader reads only plain
    ``.jsonl`` within its 14-day window and must never open a ``.gz``. This
    pins that gzip-at-30d is safe — an archived sibling is not double-counted
    and a gzipped run never surfaces."""
    # A plain day-file the reader is expected to see.
    _write_day(tmp_path, "code-writer", TODAY, [_outbound(audit_id="live")])
    # A gzipped sibling carrying a valid outbound record. If the reader opened
    # it, "archived" would appear in the results.
    gz_path = tmp_path / "code-writer" / f"{TODAY.isoformat()}.jsonl.gz"
    with gzip.open(gz_path, "wt", encoding="utf-8") as fp:
        fp.write(json.dumps(_outbound(audit_id="archived")) + "\n")

    reader = DelegationReader(tmp_path)
    runs = reader.runs(today=TODAY)

    assert [run.audit_id for run in runs] == ["live"]


def test_per_agent_history_newest_first(tmp_path: Path) -> None:
    """history() groups by slug, newest run first, most-recent slug first,
    and extracts every trailer field."""
    _write_day(
        tmp_path,
        "code-writer",
        TODAY - timedelta(days=2),
        [_outbound(ts="2026-06-08T09:00:00+00:00", cost=0.5, audit_id="cw-old")],
    )
    _write_day(
        tmp_path,
        "code-writer",
        TODAY,
        [
            _outbound(ts="2026-06-10T08:00:00+00:00", cost=1.0, audit_id="cw-am"),
            _outbound(
                ts="2026-06-10T16:00:00+00:00",
                cost=2.0,
                duration=42.5,
                tools=("Read", "Edit", "Write"),
                audit_id="cw-pm",
            ),
        ],
    )
    _write_day(
        tmp_path,
        "threat-researcher",
        TODAY - timedelta(days=1),
        [_outbound(ts="2026-06-09T12:00:00+00:00", model="claude-opus-4-1", audit_id="tr-1")],
    )

    history = DelegationReader(tmp_path).history(today=TODAY)

    # Most-recently-active slug keys first.
    assert list(history) == ["code-writer", "threat-researcher"]
    assert [run.audit_id for run in history["code-writer"]] == ["cw-pm", "cw-am", "cw-old"]

    newest = history["code-writer"][0]
    assert newest.slug == "code-writer"
    assert newest.date == "2026-06-10"
    assert newest.ts == "2026-06-10T16:00:00+00:00"
    assert newest.model == "claude-fable-5"
    assert newest.model_tier == "fable"
    assert newest.cost_usd == pytest.approx(2.0)
    assert newest.duration_seconds == pytest.approx(42.5)
    assert newest.tool_call_count == 3
    assert newest.audit_id == "cw-pm"


def test_task_and_fail_lines_are_not_runs(tmp_path: Path) -> None:
    """Only outbound trailers count; task/fail direction lines are skipped."""
    _write_day(
        tmp_path,
        "general",
        TODAY,
        [
            {"ts": "2026-06-10T10:00:00+00:00", "direction": "task", "text": "do a thing"},
            _outbound(ts="2026-06-10T10:05:00+00:00", audit_id="ok-1"),
            {
                "ts": "2026-06-10T11:00:00+00:00",
                "direction": "fail",
                "text": "timeout after 300s",
                "kind": "timeout",
                "duration_seconds": 300.0,
            },
        ],
    )

    runs = DelegationReader(tmp_path).runs(today=TODAY)

    assert [run.audit_id for run in runs] == ["ok-1"]


def test_model_split_fable_opus_sonnet(tmp_path: Path) -> None:
    """Per-model split normalizes full IDs and bare aliases to tiers,
    keeps the raw strings, and orders tiers by descending cost."""
    _write_day(
        tmp_path,
        "code-writer",
        TODAY,
        [
            _outbound(model="claude-fable-5", cost=1.0),
            _outbound(model="claude-fable-5", cost=2.0),
            _outbound(model="claude-opus-4-1", cost=4.0),
        ],
    )
    _write_day(
        tmp_path,
        "planner",
        TODAY - timedelta(days=1),
        [
            _outbound(ts="2026-06-09T12:00:00+00:00", model="sonnet", cost=0.5),
            # Trailer predating the model field → "unknown" tier.
            _outbound(ts="2026-06-09T13:00:00+00:00", model=None, cost=0.25),
        ],
    )

    split = DelegationReader(tmp_path).model_split(today=TODAY)

    assert list(split) == ["opus", "fable", "sonnet", "unknown"]
    assert split["fable"]["runs"] == 2
    assert split["fable"]["cost_usd"] == pytest.approx(3.0)
    assert split["fable"]["models"] == {"claude-fable-5": {"runs": 2, "cost_usd": pytest.approx(3.0)}}
    assert split["opus"]["runs"] == 1
    assert split["opus"]["cost_usd"] == pytest.approx(4.0)
    assert "claude-opus-4-1" in split["opus"]["models"]
    assert split["sonnet"]["runs"] == 1
    assert split["sonnet"]["cost_usd"] == pytest.approx(0.5)
    assert split["unknown"]["runs"] == 1
    assert split["unknown"]["cost_usd"] == pytest.approx(0.25)


def test_normalize_model_labels() -> None:
    """Tier normalization: full IDs, bech-prefixed IDs, aliases, edge cases."""
    assert normalize_model("claude-fable-5") == "fable"
    assert normalize_model("us.anthropic.claude-fable-5-20260101-v1:0") == "fable"
    assert normalize_model("claude-opus-4-1") == "opus"
    assert normalize_model("claude-sonnet-4-5") == "sonnet"
    assert normalize_model("haiku") == "haiku"
    assert normalize_model("<cli-default>") == "unknown"
    assert normalize_model(None) == "unknown"
    assert normalize_model("") == "unknown"
    assert normalize_model("gpt-7-mega") == "other"


def test_daily_and_weekly_rollups(tmp_path: Path) -> None:
    """Daily buckets are zero-filled across the whole window; weekly
    buckets aggregate by ISO week."""
    in_week_day = TODAY - timedelta(days=1)  # 2026-06-09, same ISO week as TODAY
    prior_week_day = TODAY - timedelta(days=8)  # 2026-06-02, a prior ISO week
    _write_day(tmp_path, "a", TODAY, [_outbound(cost=1.0), _outbound(cost=2.0)])
    _write_day(
        tmp_path,
        "a",
        in_week_day,
        [_outbound(ts="2026-06-09T12:00:00+00:00", cost=4.0)],
    )
    _write_day(
        tmp_path,
        "b",
        prior_week_day,
        [_outbound(ts="2026-06-02T12:00:00+00:00", cost=8.0)],
    )

    reader = DelegationReader(tmp_path)
    daily = reader.daily_rollup(today=TODAY)

    # Every day of the default window is present, newest first.
    assert len(daily) == DEFAULT_WINDOW_DAYS
    assert list(daily)[0] == TODAY.isoformat()
    assert list(daily)[-1] == (TODAY - timedelta(days=DEFAULT_WINDOW_DAYS - 1)).isoformat()
    assert daily[TODAY.isoformat()] == {"runs": 2, "cost_usd": pytest.approx(3.0)}
    assert daily[in_week_day.isoformat()] == {"runs": 1, "cost_usd": pytest.approx(4.0)}
    assert daily[prior_week_day.isoformat()] == {"runs": 1, "cost_usd": pytest.approx(8.0)}
    # A day with no delegations is present and zeroed.
    quiet_day = (TODAY - timedelta(days=3)).isoformat()
    assert daily[quiet_day] == {"runs": 0, "cost_usd": 0.0}

    weekly = reader.weekly_rollup(today=TODAY)

    iso_now = TODAY.isocalendar()
    this_week = f"{iso_now.year}-W{iso_now.week:02d}"
    iso_prior = prior_week_day.isocalendar()
    prior_week = f"{iso_prior.year}-W{iso_prior.week:02d}"
    assert this_week != prior_week
    assert list(weekly)[0] == this_week
    assert weekly[this_week] == {"runs": 3, "cost_usd": pytest.approx(7.0)}
    assert weekly[prior_week] == {"runs": 1, "cost_usd": pytest.approx(8.0)}


def test_window_bounding(tmp_path: Path) -> None:
    """Only day-files dated inside the window are read; future-dated and
    non-day files are ignored."""
    _write_day(tmp_path, "a", TODAY, [_outbound(audit_id="in-today")])
    _write_day(
        tmp_path,
        "a",
        TODAY - timedelta(days=DEFAULT_WINDOW_DAYS - 1),
        [_outbound(ts="2026-05-28T12:00:00+00:00", audit_id="in-edge")],
    )
    _write_day(
        tmp_path,
        "a",
        TODAY - timedelta(days=DEFAULT_WINDOW_DAYS),
        [_outbound(ts="2026-05-27T12:00:00+00:00", audit_id="out-old")],
    )
    _write_day(
        tmp_path,
        "a",
        TODAY + timedelta(days=1),
        [_outbound(ts="2026-06-11T12:00:00+00:00", audit_id="out-future")],
    )
    # A non-day-named .jsonl never matches the <YYYY-MM-DD>.jsonl shape.
    (tmp_path / "a" / "notes.jsonl").write_text(
        json.dumps(_outbound(audit_id="out-name")) + "\n", encoding="utf-8"
    )

    reader = DelegationReader(tmp_path)

    default_ids = {run.audit_id for run in reader.runs(today=TODAY)}
    assert default_ids == {"in-today", "in-edge"}

    # A narrower per-call window excludes the day-13 edge file too.
    narrow_ids = {run.audit_id for run in reader.runs(window_days=7, today=TODAY)}
    assert narrow_ids == {"in-today"}


def test_missing_dir_zeroed_rollups_empty_history(tmp_path: Path) -> None:
    """A transcript root that does not exist (fresh install) degrades to
    empty history and zero-filled rollups without raising."""
    reader = DelegationReader(tmp_path / "transcripts" / "subagent")

    assert reader.runs(today=TODAY) == []
    assert reader.history(today=TODAY) == {}
    assert reader.model_split(today=TODAY) == {}

    daily = reader.daily_rollup(today=TODAY)
    assert len(daily) == DEFAULT_WINDOW_DAYS
    assert all(bucket == {"runs": 0, "cost_usd": 0.0} for bucket in daily.values())

    weekly = reader.weekly_rollup(today=TODAY)
    assert weekly  # ISO weeks of the window are present...
    assert all(bucket == {"runs": 0, "cost_usd": 0.0} for bucket in weekly.values())

    # Root pointing at a *file* degrades the same way (NotADirectoryError
    # is swallowed, not raised).
    stray = tmp_path / "stray.txt"
    stray.write_text("not a dir", encoding="utf-8")
    assert DelegationReader(stray).runs(today=TODAY) == []


def test_malformed_lines_skipped_no_raise(tmp_path: Path) -> None:
    """Garbage JSON, non-dict lines, and mistyped trailer fields are
    skipped or coerced; the good lines still parse."""
    _write_day(
        tmp_path,
        "a",
        TODAY,
        [
            "{this is not json",
            '["a", "json", "array"]',
            '"just a string"',
            "",
            # Mistyped trailer fields coerce to zero rather than raising.
            _outbound(
                ts="2026-06-10T10:00:00+00:00",
                audit_id="weird",
                cost="not-a-number",
                duration=None,
                tools="Read",
            ),
            _outbound(ts="2026-06-10T11:00:00+00:00", audit_id="good", cost=1.5),
            # Half-written trailing line from a concurrent daemon append.
            '{"ts": "2026-06-10T23:59:59+00:00", "direction": "outbound", "cost_us',
        ],
    )

    runs = DelegationReader(tmp_path).runs(today=TODAY)

    assert [run.audit_id for run in runs] == ["good", "weird"]
    good, weird = runs[0], runs[1]
    assert good.cost_usd == pytest.approx(1.5)
    assert weird.cost_usd == 0.0
    assert weird.duration_seconds == 0.0
    assert weird.tool_call_count == 0


def test_duration_ms_fallback(tmp_path: Path) -> None:
    """A trailer carrying duration_ms instead of duration_seconds converts."""
    record = _outbound(audit_id="ms-run")
    del record["duration_seconds"]
    record["duration_ms"] = 2500
    _write_day(tmp_path, "a", TODAY, [record])

    (run,) = DelegationReader(tmp_path).runs(today=TODAY)

    assert run.duration_seconds == pytest.approx(2.5)


def test_cache_keyed_on_mtime_and_size(tmp_path: Path) -> None:
    """Per-file cache: identical (mtime, size) serves the cached parse;
    a changed mtime or size re-parses."""
    path = _write_day(tmp_path, "a", TODAY, [_outbound(cost=1.25, audit_id="v1")])
    reader = DelegationReader(tmp_path)

    (first,) = reader.runs(today=TODAY)
    assert first.cost_usd == pytest.approx(1.25)
    stat = path.stat()

    # Rewrite with a same-byte-length payload and restore the exact
    # mtime: the stale cache entry is (correctly) served, proving the
    # cache key is (mtime, size) and not content.
    new_line = json.dumps(_outbound(cost=7.75, audit_id="v2")).replace("v2", "v1")
    assert len(new_line) + 1 == stat.st_size
    path.write_text(new_line + "\n", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    (cached,) = reader.runs(today=TODAY)
    assert cached.cost_usd == pytest.approx(1.25)

    # Bumping mtime invalidates and re-parses the rewritten content.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    (reparsed,) = reader.runs(today=TODAY)
    assert reparsed.cost_usd == pytest.approx(7.75)

    # A size change (appended run) also invalidates.
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(_outbound(cost=0.5, audit_id="v3")) + "\n")
    runs = reader.runs(today=TODAY)
    assert {run.audit_id for run in runs} == {"v1", "v3"}
