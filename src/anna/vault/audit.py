"""Audit log readers.

The append-only writer lives in :mod:`anna.log.audit_event`. This module
exposes readers used by the ``anna-logs --audit`` CLI wrapper.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


def list_audit_files(audit_dir: Path) -> list[Path]:
    if not audit_dir.is_dir():
        return []
    return sorted(audit_dir.glob("audit-*.jsonl"), reverse=True)


def iter_audit_events(
    *,
    audit_dir: Path,
    since: datetime | None = None,
    event_filter: str | None = None,
) -> Iterator[dict]:
    """Yield audit events newest first.

    ``since`` bounds the time range. ``event_filter`` filters by exact event
    name match.
    """
    files = list_audit_files(audit_dir)
    if since is not None:
        # Audit files are named audit-YYYY-MM-DD.jsonl; trim to ones whose
        # date is on or after ``since``.
        cutoff_date = since.date()
        files = [
            p for p in files
            if _date_from_audit_name(p.name) >= cutoff_date
        ]

    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fp:
                lines = fp.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_filter and rec.get("event") != event_filter:
                continue
            if since is not None:
                ts_str = rec.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < since:
                    continue
            yield rec


def _date_from_audit_name(name: str) -> datetime.date:
    # name is "audit-YYYY-MM-DD.jsonl"
    stem = name.removeprefix("audit-").removesuffix(".jsonl")
    try:
        return datetime.strptime(stem, "%Y-%m-%d").date()
    except ValueError:
        # Anything unparseable sorts last by returning the epoch date.
        return datetime(1970, 1, 1).date()
