"""Validate the audit file rolls daily and retention sweeps prune old files."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.log import audit_event, sweep_audit_retention


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
