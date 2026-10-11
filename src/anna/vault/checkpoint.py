"""Per-conversation vault checkpoints.

Written at session close so the next worker that resumes the same
conversation key can read context from disk. Per v3 section 6, the worker
reads the two most recent checkpoints on resume.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anna.vault.paths import safe_conv_key


def write_checkpoint(
    *,
    vault_root: Path,
    transport: str,
    conversation_key: str,
    summary: str,
    operator_short_name: str | None = None,
    kind: str = "closeout",
    coverage_end_sequence: int | None = None,
    coverage_end_event_id: str | None = None,
    unsummarized_events: int = 0,
    legacy_cutoff_ts: float | None = None,
) -> Path:
    """Write a markdown checkpoint and return its path.

    Layout, matching the v3 vault sketch:

    ``Conversations/<transport>-<dm-or-ch>-<id>/<YYYY-MM-DD-HHMMSS>.md``

    The stamp is second-granular so that two writes in the same minute
    (e.g. a periodic checkpoint followed by a closeout) produce distinct
    files instead of colliding. The fixed-width zero-padded ``%H%M%S``
    keeps the lexicographic ordering used by ``list_recent_checkpoints``
    correct (later wall-clock time sorts after earlier).

    ``kind`` is recorded in the frontmatter as ``checkpoint_kind`` and
    defaults to ``"closeout"`` for backward compatibility; periodic
    checkpoints pass ``"periodic"``.
    """
    safe_key = safe_conv_key(conversation_key)
    base_dir = vault_root / "Conversations" / safe_key
    base_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    # Time is presentation metadata, not uniqueness.  A periodic checkpoint
    # and closeout can land in one tick, so give every completed write an ID.
    path = base_dir / f"{stamp}-{uuid.uuid4().hex}.md"

    coverage = ""
    if coverage_end_sequence is not None and coverage_end_sequence > 0 and coverage_end_event_id:
        coverage = (
            "coverage_version: 1\n"
            f"coverage_end_sequence: {coverage_end_sequence}\n"
            f"coverage_end_event_id: {coverage_end_event_id}\n"
            + (f"coverage_unsummarized_events: {unsummarized_events}\n" if unsummarized_events else "")
            # Migration marker: unsequenced (main-era) transcript lines at or before
            # this epoch are summarized. Only an explicit-coverage checkpoint carries it.
            + (f"coverage_legacy_cutoff_ts: {legacy_cutoff_ts:.6f}\n" if legacy_cutoff_ts and legacy_cutoff_ts > 0 else "")
        )
    frontmatter = (
        f"---\n"
        f"created: {now.strftime('%Y-%m-%d')}\n"
        f"tags:\n"
        f"  - domain/anna\n"
        f"  - type/checkpoint\n"
        f"transport: {transport}\n"
        f"conversation_key: {conversation_key}\n"
        f"checkpoint_kind: {kind}\n"
        # New-build provenance: a main-format checkpoint never has this key, so
        # a coverage-less periodic / failed-turn checkpoint is not mistaken for one.
        f"checkpoint_schema: 2\n"
        f"{coverage}"
        f"---\n\n"
    )
    body = f"# Checkpoint\n\n{summary}\n"
    if unsummarized_events:
        body += (f"\n> Note: {unsummarized_events} earlier transcript event(s) were never shown to the session that "
                 "wrote this checkpoint and are NOT summarized here; they remain in the raw transcript.\n")
    if operator_short_name:
        body += f"\nAddressed as: {operator_short_name}\n"

    # A resume reader must see either the old complete set or this complete
    # checkpoint, never a torn markdown file after interruption.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=base_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(frontmatter + body)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def list_recent_checkpoints(
    *,
    vault_root: Path,
    conversation_key: str,
    limit: int = 2,
) -> list[Path]:
    safe_key = safe_conv_key(conversation_key)
    base_dir = vault_root / "Conversations" / safe_key
    if not base_dir.is_dir():
        return []
    files = sorted(base_dir.glob("*.md"), reverse=True)
    return files[:limit]
