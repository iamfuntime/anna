"""Resume-from-transcript helpers.

When a worker resumes a conversation, the two newest *checkpoints* are
injected into the system prompt. Checkpoints are written at graceful
closeout, so a hard crash / OOM-kill / ``kill -9`` that never ran
closeout leaves the post-checkpoint turns stranded in the JSONL
transcript only.

This module folds a bounded RAW tail of that transcript into the resume
block when the transcript is newer than the latest checkpoint. It is
pure and synchronous — no SDK dependency, no LLM call — so it can run on
the spawn hot path. Dedup is automatic: only lines strictly newer than
the latest checkpoint's mtime survive.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from anna.core.identity import count_tokens
from anna.vault.checkpoint import list_recent_checkpoints


def latest_checkpoint_mtime(vault_root: Path, conv_key: str) -> float | None:
    """Return the mtime of the newest checkpoint, or None if none exist."""
    recent = list_recent_checkpoints(
        vault_root=vault_root,
        conversation_key=conv_key,
        limit=1,
    )
    if not recent:
        return None
    return recent[0].stat().st_mtime


_COVERAGE_SEQUENCE_RE = re.compile(r"^coverage_end_sequence:\s*(\d+)\s*$", re.MULTILINE)
_COVERAGE_EVENT_RE = re.compile(r"^coverage_end_event_id:\s*([0-9a-f]{32})\s*$", re.MULTILINE)


def latest_checkpoint_coverage(vault_root: Path, conv_key: str) -> int | None:
    """Return validated explicit checkpoint coverage, never a file timestamp.

    Checkpoints predating coverage metadata remain useful prompt context but
    are deliberately treated as covering nothing.  This also makes copied or
    restored file mtimes irrelevant to transcript recovery.
    """
    for path in list_recent_checkpoints(vault_root=vault_root, conversation_key=conv_key, limit=1000):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sequence = _COVERAGE_SEQUENCE_RE.search(text)
        event_id = _COVERAGE_EVENT_RE.search(text)
        if sequence is not None and event_id is not None:
            value = int(sequence.group(1))
            if value > 0:
                return value
    return None


def validated_checkpoint_coverage(
    vault_root: Path,
    transcripts_dir: Path,
    conv_key: str,
) -> int | None:
    """Return coverage only when its checkpoint event still exists in JSONL."""
    base_dir = transcripts_dir / _safe_key(conv_key)
    for path in list_recent_checkpoints(vault_root=vault_root, conversation_key=conv_key, limit=1000):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sequence = _COVERAGE_SEQUENCE_RE.search(text)
        event_id = _COVERAGE_EVENT_RE.search(text)
        if sequence is None or event_id is None:
            continue
        value = int(sequence.group(1))
        if value <= 0:
            continue
        for transcript in sorted(base_dir.glob("*.jsonl")):
            try:
                raw_lines = transcript.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in raw_lines:
                try:
                    obj = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if (
                    isinstance(obj, dict)
                    and obj.get("sequence") == value
                    and obj.get("event_id") == event_id.group(1)
                ):
                    return value
    return None


def has_uncovered_event_before(
    transcripts_dir: Path,
    conv_key: str,
    sequence: int,
    covered_through_sequence: int | None,
) -> bool:
    """Whether a new worker would skip durable history before ``sequence``.

    This is intentionally conservative: a legacy/no-sequence line or an
    ordered event outside validated coverage prevents a new closeout summary
    from advancing the cursor. The raw tail remains available instead.
    """
    base_dir = transcripts_dir / _safe_key(conv_key)
    floor = covered_through_sequence or 0
    if not base_dir.is_dir():
        return False
    for path in sorted(base_dir.glob("*.jsonl")):
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in raw_lines:
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            event_sequence = obj.get("sequence")
            if not isinstance(event_sequence, int):
                return True
            if floor < event_sequence < sequence:
                return True
    return False


def _safe_key(conv_key: str) -> str:
    # Mirror the transcript writer's transform in anna.log._transcript_dir_for.
    return conv_key.replace(":", "-").replace("/", "_")


def _parse_ts(value: object) -> float | None:
    """Parse an ISO-8601 ``ts`` into an epoch float, or None if unparseable.

    Transcript timestamps are written with a trailing ``Z`` (e.g.
    ``2026-06-03T01:10:55.232Z``); ``datetime.fromisoformat`` only learned
    ``Z`` in 3.11, so normalise it to ``+00:00`` defensively.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def transcript_tail_since(
    transcripts_dir: Path,
    conv_key: str,
    since_mtime: float | None,
    max_turns: int,
    max_tokens: int,
    *,
    covered_through_sequence: int | None = None,
) -> list[dict]:
    """Return explicitly uncovered transcript lines, bounded.

    Reads the newest one or two daily JSONL files for ``conv_key``, parses
    each line as JSON (skipping malformed lines defensively), and keeps
    only events whose stable ``sequence`` is above
    ``covered_through_sequence``. Legacy JSONL events without a sequence are
    never filtered. ``since_mtime`` is retained only for callers of the old
    standalone helper; runtime recovery must pass explicit coverage instead.

    The surviving lines are then trimmed from the END to at most
    ``max_turns`` inbound-anchored exchanges (an inbound line starts a new
    exchange; the following outbound lines belong to it) and to at most
    ``max_tokens`` of accumulated text, keeping the most recent window and
    trimming oldest first. Returned in chronological order.
    """
    base_dir = transcripts_dir / _safe_key(conv_key)
    if not base_dir.is_dir():
        return []

    # Read every retained JSONL day. An old event without validated coverage
    # must remain retrievable; trimming happens below, not by silently
    # excluding whole days.
    daily = sorted(base_dir.glob("*.jsonl"))

    lines: list[dict] = []
    for path in daily:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in raw.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            sequence = obj.get("sequence")
            if covered_through_sequence is not None and isinstance(sequence, int):
                if sequence <= covered_through_sequence:
                    continue
            elif covered_through_sequence is None and since_mtime is not None:
                # Compatibility path for direct legacy callers. The worker no
                # longer uses mtimes as a correctness cursor.
                ts = _parse_ts(obj.get("ts"))
                if ts is None or ts <= since_mtime:
                    continue
            lines.append(obj)

    if not lines:
        return []

    return _trim_tail(lines, max_turns, max_tokens)


def _trim_tail(lines: list[dict], max_turns: int, max_tokens: int) -> list[dict]:
    """Trim ``lines`` (chronological) to the most recent window.

    Caps to at most ``max_turns`` inbound-anchored exchanges and at most
    ``max_tokens`` of accumulated text, walking from newest to oldest and
    stopping once either budget is exhausted.
    """
    kept_reversed: list[dict] = []
    turns = 0
    tokens = 0
    for obj in reversed(lines):
        text = obj.get("text") or ""
        cost = count_tokens(text)
        is_inbound = obj.get("direction") == "inbound"

        # Once the turn cap is reached, every remaining (older) line belongs
        # to an excluded exchange — including any outbound lines that precede
        # the next inbound — so stop entirely rather than admitting orphans.
        if turns >= max_turns:
            break
        # Token cap: stop before admitting a line that would overflow, but
        # always keep at least one line so the tail is never spuriously empty.
        if kept_reversed and tokens + cost > max_tokens:
            break

        kept_reversed.append(obj)
        tokens += cost
        if is_inbound:
            turns += 1

    kept_reversed.reverse()
    return kept_reversed


def render_tail_block(tail: list[dict]) -> str:
    """Render a compact markdown block from a transcript tail.

    Returns "" for an empty tail. Otherwise a heading followed by one line
    per entry: inbound -> ``**you:** <text>``, outbound -> ``**anna:**
    <text>``. Any single message longer than ~400 chars is truncated with
    an ellipsis.
    """
    if not tail:
        return ""

    out: list[str] = ["# Unsaved conversation tail (since last checkpoint)", ""]
    for obj in tail:
        text = (obj.get("text") or "").strip()
        if len(text) > 400:
            text = text[:400].rstrip() + "..."
        if obj.get("direction") == "inbound":
            out.append(f"**you:** {text}")
        else:
            out.append(f"**anna:** {text}")
    return "\n".join(out)
