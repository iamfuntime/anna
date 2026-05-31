"""Transcript readers.

The writer lives in :mod:`anna.log` so any subsystem can append. This module
holds readers used by the ``anna-logs --transcript`` CLI wrapper.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Iterator


def list_conversations(transcripts_dir: Path) -> list[str]:
    """Return the conversation directory names that have transcripts on disk."""
    if not transcripts_dir.is_dir():
        return []
    return sorted(d.name for d in transcripts_dir.iterdir() if d.is_dir())


def iter_transcript_lines(
    *,
    transcripts_dir: Path,
    conv_dir_name: str,
    days: int | None = None,
) -> Iterator[dict]:
    """Yield transcript lines for one conversation, newest day first.

    ``days`` bounds how far back to read. ``None`` means every file present.
    Handles both raw ``.jsonl`` files and gzipped ``.jsonl.gz`` archives.
    """
    conv_dir = transcripts_dir / conv_dir_name
    if not conv_dir.is_dir():
        return
    files = sorted(
        list(conv_dir.glob("*.jsonl")) + list(conv_dir.glob("*.jsonl.gz")),
        reverse=True,
    )
    if days is not None:
        files = files[:days]

    for path in files:
        if path.suffix == ".gz":
            opener = lambda p: gzip.open(p, "rt", encoding="utf-8")
        else:
            opener = lambda p: p.open("r", encoding="utf-8")
        try:
            with opener(path) as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
