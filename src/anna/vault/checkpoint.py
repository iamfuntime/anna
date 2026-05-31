"""Per-conversation vault checkpoints.

Written at session close so the next worker that resumes the same
conversation key can read context from disk. Per v3 section 6, the worker
reads the two most recent checkpoints on resume.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def write_checkpoint(
    *,
    vault_root: Path,
    transport: str,
    conversation_key: str,
    summary: str,
    operator_short_name: str | None = None,
) -> Path:
    """Write a markdown checkpoint and return its path.

    Layout, matching the v3 vault sketch:

    ``Conversations/<transport>-<dm-or-ch>-<id>/<YYYY-MM-DD-HHMM>.md``
    """
    safe_key = conversation_key.replace(":", "-").replace("/", "_")
    base_dir = vault_root / "Conversations" / safe_key
    base_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d-%H%M")
    path = base_dir / f"{stamp}.md"

    frontmatter = (
        f"---\n"
        f"created: {now.strftime('%Y-%m-%d')}\n"
        f"tags:\n"
        f"  - domain/anna\n"
        f"  - type/checkpoint\n"
        f"transport: {transport}\n"
        f"conversation_key: {conversation_key}\n"
        f"---\n\n"
    )
    body = f"# Checkpoint\n\n{summary}\n"
    if operator_short_name:
        body += f"\nAddressed as: {operator_short_name}\n"

    path.write_text(frontmatter + body, encoding="utf-8")
    return path


def list_recent_checkpoints(
    *,
    vault_root: Path,
    conversation_key: str,
    limit: int = 2,
) -> list[Path]:
    safe_key = conversation_key.replace(":", "-").replace("/", "_")
    base_dir = vault_root / "Conversations" / safe_key
    if not base_dir.is_dir():
        return []
    files = sorted(base_dir.glob("*.md"), reverse=True)
    return files[:limit]
