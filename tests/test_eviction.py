"""Validate that eviction archives evicted content to vault/Identity/."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.core.eviction import evict_if_over_cap, perform_eviction
from anna.core.identity import CORE_FILES, CoreFile


def test_eviction_writes_archive_and_rewrites_core(tmp_path: Path) -> None:
    core_dir = tmp_path / "core"
    audit_dir = tmp_path / "audit"
    vault_root = tmp_path / "vault"
    core_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)

    core_path = core_dir / "MEMORY.md"
    core_path.write_text("keep me\n\nevict me too long\n", encoding="utf-8")

    archive_path = perform_eviction(
        which=CoreFile.MEMORY,
        core_dir=core_dir,
        vault_root=vault_root,
        keep_text="keep me\n",
        evict_text="evict me too long\n",
        reason="trimming verbose preference",
        session_close_conv="test:conv:1",
        audit_dir=audit_dir,
        fsync_on_write=False,
    )

    # Core file is rewritten with only the kept content.
    assert core_path.read_text(encoding="utf-8") == "keep me\n"

    # Archive lives under vault/Identity/ with today's date in the filename.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert archive_path.parent == vault_root / "Identity"
    assert today in archive_path.name
    assert "MEMORY-archive-" in archive_path.name

    archive_text = archive_path.read_text(encoding="utf-8")
    assert "evict me too long" in archive_text
    assert "reason:" in archive_text  # frontmatter present

    # Audit event was emitted to the daily jsonl file.
    audit_files = list(audit_dir.glob("audit-*.jsonl"))
    assert audit_files, "expected an audit file to be created"
    lines = audit_files[0].read_text(encoding="utf-8").strip().splitlines()
    audit_records = [json.loads(line) for line in lines]
    eviction_records = [r for r in audit_records if r.get("event") == "audit.eviction"]
    assert eviction_records
    rec = eviction_records[-1]
    assert rec["file"] == "MEMORY.md"
    assert rec["reason"] == "trimming verbose preference"
    assert rec["session_close_conv"] == "test:conv:1"


def test_eviction_appends_to_existing_archive(tmp_path: Path) -> None:
    core_dir = tmp_path / "core"
    audit_dir = tmp_path / "audit"
    vault_root = tmp_path / "vault"
    core_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)

    core_path = core_dir / "MEMORY.md"
    core_path.write_text("first content\n", encoding="utf-8")
    perform_eviction(
        which=CoreFile.MEMORY,
        core_dir=core_dir,
        vault_root=vault_root,
        keep_text="kept\n",
        evict_text="first eviction\n",
        reason="first",
        session_close_conv="test:conv:1",
        audit_dir=audit_dir,
        fsync_on_write=False,
    )

    archive_path = perform_eviction(
        which=CoreFile.MEMORY,
        core_dir=core_dir,
        vault_root=vault_root,
        keep_text="kept twice\n",
        evict_text="second eviction\n",
        reason="second",
        session_close_conv="test:conv:2",
        audit_dir=audit_dir,
        fsync_on_write=False,
    )

    text = archive_path.read_text(encoding="utf-8")
    assert "first eviction" in text
    assert "second eviction" in text


def test_eviction_sweep_iterates_cadence(tmp_path: Path) -> None:
    """The closeout sweep iterates CORE_FILES.keys() (worker.py:619).

    CADENCE.md must be a member of that registry so the sweep picks it up
    with no further wiring; verify both registry membership and that an
    under-cap CADENCE.md is a clean no-op under evict_if_over_cap.
    """
    # Registry membership — matches the iteration shape in
    # ConversationWorker._closeout (worker.py:619).
    assert CoreFile.CADENCE in CORE_FILES
    assert CoreFile.CADENCE in list(CORE_FILES.keys())
    spec = CORE_FILES[CoreFile.CADENCE]
    assert spec.name == "CADENCE.md"
    assert spec.token_cap == 1000

    # Functional sweep over a small CADENCE.md is a no-op (under cap, so
    # the SDK is never consulted and the archive path stays None).
    core_dir = tmp_path / "core"
    audit_dir = tmp_path / "audit"
    vault_root = tmp_path / "vault"
    core_dir.mkdir(parents=True)
    audit_dir.mkdir(parents=True)
    (core_dir / "CADENCE.md").write_text("short cadence rules\n", encoding="utf-8")

    result = asyncio.run(
        evict_if_over_cap(
            which=CoreFile.CADENCE,
            core_dir=core_dir,
            vault_root=vault_root,
            sdk_client=None,
            session_close_conv="test:conv:cadence",
            audit_dir=audit_dir,
            fsync_on_write=False,
        )
    )
    assert result is None
