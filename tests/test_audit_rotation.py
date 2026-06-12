"""Validate the audit file rolls daily and retention sweeps prune old files."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.log import (
    audit_event,
    sweep_audit_retention,
    sweep_transcript_retention,
    sweep_voice_retention,
)


def test_audit_event_creates_daily_file(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_event(
        "audit.test.event",
        audit_dir=audit_dir,
        actor="anna",
        fsync_on_write=False,
        foo="bar",
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = audit_dir / f"audit-{today}.jsonl"
    assert path.exists()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "audit.test.event"
    assert record["actor"] == "anna"
    assert record["foo"] == "bar"


def test_audit_retention_sweep_deletes_old_files(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    # Create three fake daily files.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_file = audit_dir / f"audit-{today}.jsonl"
    new_file.write_text('{"event":"new"}\n', encoding="utf-8")

    old_file = audit_dir / "audit-2025-01-01.jsonl"
    old_file.write_text('{"event":"old"}\n', encoding="utf-8")
    very_old_file = audit_dir / "audit-2024-01-01.jsonl"
    very_old_file.write_text('{"event":"very_old"}\n', encoding="utf-8")

    # Backdate the mtime so the sweep treats them as old.
    long_ago = time.time() - 1000 * 86400
    os.utime(old_file, (long_ago, long_ago))
    os.utime(very_old_file, (long_ago, long_ago))

    deleted = sweep_audit_retention(audit_dir, retention_days=30)
    assert deleted == 2
    assert new_file.exists()
    assert not old_file.exists()
    assert not very_old_file.exists()


def test_audit_retention_zero_keeps_forever(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    old_file = audit_dir / "audit-2024-01-01.jsonl"
    old_file.write_text('{"event":"old"}\n', encoding="utf-8")
    long_ago = time.time() - 1000 * 86400
    os.utime(old_file, (long_ago, long_ago))

    deleted = sweep_audit_retention(audit_dir, retention_days=0)
    assert deleted == 0
    assert old_file.exists()


# ---------------------------------------------------------------------------
# Phase 2.5 voice-file retention sweep
# ---------------------------------------------------------------------------


def _make_voice_file(transcripts_dir: Path, conv_key: str, name: str) -> Path:
    """Create a fake persisted voice note under transcripts/voice/<conv>/."""
    conv_dir = transcripts_dir / "voice" / conv_key
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / name
    path.write_bytes(b"\x00\x01opus-bytes")
    return path


def test_voice_retention_deletes_old_and_keeps_recent(tmp_path: Path) -> None:
    transcripts_dir = tmp_path / "transcripts"

    fresh = _make_voice_file(transcripts_dir, "telegram-123", "msg-new.ogg")
    old = _make_voice_file(transcripts_dir, "telegram-123", "msg-old.ogg")
    old_other_conv = _make_voice_file(transcripts_dir, "slack-456", "msg-old.webm")

    # Backdate the two "old" files well past the 30-day window.
    long_ago = time.time() - 90 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(old_other_conv, (long_ago, long_ago))

    deleted = sweep_voice_retention(transcripts_dir, retention_days=30)

    assert deleted == 2
    assert fresh.exists()
    assert not old.exists()
    assert not old_other_conv.exists()


def test_voice_retention_zero_keeps_forever(tmp_path: Path) -> None:
    transcripts_dir = tmp_path / "transcripts"
    old = _make_voice_file(transcripts_dir, "telegram-123", "msg-old.ogg")
    long_ago = time.time() - 90 * 86400
    os.utime(old, (long_ago, long_ago))

    deleted = sweep_voice_retention(transcripts_dir, retention_days=0)

    assert deleted == 0
    assert old.exists()


def test_voice_retention_missing_dir_is_noop(tmp_path: Path) -> None:
    # transcripts/ exists but no voice/ subtree yet (no voice notes ever
    # persisted) — the sweep must not crash and must report zero deletions.
    transcripts_dir = tmp_path / "transcripts"
    transcripts_dir.mkdir()

    deleted = sweep_voice_retention(transcripts_dir, retention_days=30)

    assert deleted == 0


def test_voice_retention_ignores_non_voice_transcripts(tmp_path: Path) -> None:
    # An old JSONL transcript in a sibling conv dir under transcripts/ (not
    # under voice/) must be left untouched by the voice sweep — that subtree
    # is sweep_transcript_retention's job.
    transcripts_dir = tmp_path / "transcripts"
    conv_dir = transcripts_dir / "telegram-123"
    conv_dir.mkdir(parents=True)
    jsonl = conv_dir / "2024-01-01.jsonl"
    jsonl.write_text('{"event":"old"}\n', encoding="utf-8")
    long_ago = time.time() - 90 * 86400
    os.utime(jsonl, (long_ago, long_ago))

    old_voice = _make_voice_file(transcripts_dir, "telegram-123", "msg-old.ogg")
    os.utime(old_voice, (long_ago, long_ago))

    deleted = sweep_voice_retention(transcripts_dir, retention_days=30)

    assert deleted == 1
    assert jsonl.exists()
    assert not old_voice.exists()


# ---------------------------------------------------------------------------
# Transcript retention sweep — gzip at retention_days, delete at 3x. Must
# treat nested sub-agent day-files (transcripts/subagent/<slug>/<date>.jsonl)
# identically to one-level conversation files (transcripts/<conv>/<date>.jsonl).
# retention_days=30 -> gzip cutoff 30d, delete cutoff 90d.
# ---------------------------------------------------------------------------


def _make_transcript_file(
    transcripts_dir: Path,
    rel: str,
    *,
    age_days: float,
    content: bytes = b'{"direction":"task"}\n',
) -> Path:
    """Create transcripts/<rel> and backdate its mtime by age_days days."""
    path = transcripts_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if age_days:
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
    return path


def _gz_sibling(jsonl_path: Path) -> Path:
    """The .gz path the sweep writes for a given .jsonl day-file."""
    return jsonl_path.parent / (jsonl_path.name + ".gz")


def test_transcript_retention_gzips_old_conversation_file(tmp_path: Path) -> None:
    # Regression pin for the one-level layout: a conversation day-file older
    # than the gzip threshold (but younger than the delete threshold) is
    # gzipped in place, while a fresh sibling is left untouched. This is the
    # behavior that must NOT change.
    transcripts_dir = tmp_path / "transcripts"
    old = _make_transcript_file(
        transcripts_dir, "telegram-123/2026-04-01.jsonl", age_days=45
    )
    fresh = _make_transcript_file(
        transcripts_dir, "telegram-123/2026-06-12.jsonl", age_days=0
    )

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=30)

    assert (gzipped, deleted) == (1, 0)
    assert not old.exists()
    assert _gz_sibling(old).exists()
    assert fresh.exists()


def test_transcript_retention_deletes_old_conversation_gz(tmp_path: Path) -> None:
    # Regression pin: a one-level .gz older than the delete threshold (3x) is
    # removed; a fresh .gz is kept.
    transcripts_dir = tmp_path / "transcripts"
    old_gz = _make_transcript_file(
        transcripts_dir, "telegram-123/2026-01-01.jsonl.gz", age_days=120
    )
    fresh_gz = _make_transcript_file(
        transcripts_dir, "telegram-123/2026-06-01.jsonl.gz", age_days=5
    )

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=30)

    assert (gzipped, deleted) == (0, 1)
    assert not old_gz.exists()
    assert fresh_gz.exists()


def test_transcript_retention_gzips_nested_subagent_file(tmp_path: Path) -> None:
    # The bug fix: a nested sub-agent day-file two levels deep gets gzipped
    # at the same 30-day threshold as conversation files.
    transcripts_dir = tmp_path / "transcripts"
    nested = _make_transcript_file(
        transcripts_dir, "subagent/code-writer/2026-04-01.jsonl", age_days=45
    )

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=30)

    assert (gzipped, deleted) == (1, 0)
    assert not nested.exists()
    assert _gz_sibling(nested).exists()


def test_transcript_retention_deletes_nested_subagent_gz(tmp_path: Path) -> None:
    # The bug fix: a nested sub-agent .gz older than the delete threshold (3x)
    # is removed rather than accumulating forever.
    transcripts_dir = tmp_path / "transcripts"
    nested_gz = _make_transcript_file(
        transcripts_dir, "subagent/code-writer/2026-01-01.jsonl.gz", age_days=120
    )

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=30)

    assert (gzipped, deleted) == (0, 1)
    assert not nested_gz.exists()


def test_transcript_retention_leaves_fresh_files_untouched(tmp_path: Path) -> None:
    # Fresh files at both depths must be left in place (no gzip, no delete).
    transcripts_dir = tmp_path / "transcripts"
    conv_fresh = _make_transcript_file(
        transcripts_dir, "telegram-123/2026-06-12.jsonl", age_days=0
    )
    nested_fresh = _make_transcript_file(
        transcripts_dir, "subagent/code-writer/2026-06-12.jsonl", age_days=2
    )

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=30)

    assert (gzipped, deleted) == (0, 0)
    assert conv_fresh.exists()
    assert nested_fresh.exists()
    assert not _gz_sibling(conv_fresh).exists()
    assert not _gz_sibling(nested_fresh).exists()


def test_transcript_retention_empty_slug_dir_graceful(tmp_path: Path) -> None:
    # An empty subagent/<slug>/ directory (slug seen but no day-files written
    # yet) must not crash the sweep and must report zero work.
    transcripts_dir = tmp_path / "transcripts"
    (transcripts_dir / "subagent" / "code-writer").mkdir(parents=True)
    (transcripts_dir / "subagent" / "empty-other").mkdir(parents=True)

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=30)

    assert (gzipped, deleted) == (0, 0)
    assert (transcripts_dir / "subagent" / "code-writer").is_dir()


def test_transcript_retention_zero_keeps_forever(tmp_path: Path) -> None:
    transcripts_dir = tmp_path / "transcripts"
    old = _make_transcript_file(
        transcripts_dir, "subagent/code-writer/2025-01-01.jsonl", age_days=300
    )

    gzipped, deleted = sweep_transcript_retention(transcripts_dir, retention_days=0)

    assert (gzipped, deleted) == (0, 0)
    assert old.exists()
