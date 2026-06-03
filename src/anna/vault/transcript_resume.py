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
) -> list[dict]:
    """Return transcript lines newer than ``since_mtime``, bounded.

    Reads the newest one or two daily JSONL files for ``conv_key``, parses
    each line as JSON (skipping malformed lines defensively), and keeps
    only lines whose ``ts`` is strictly newer than ``since_mtime``. If
    ``since_mtime`` is None the whole tail is kept.

    The surviving lines are then trimmed from the END to at most
    ``max_turns`` inbound-anchored exchanges (an inbound line starts a new
    exchange; the following outbound lines belong to it) and to at most
    ``max_tokens`` of accumulated text, keeping the most recent window and
    trimming oldest first. Returned in chronological order.
    """
    base_dir = transcripts_dir / _safe_key(conv_key)
    if not base_dir.is_dir():
        return []

    # Newest one or two daily files. Names are YYYY-MM-DD.jsonl, so a
    # reverse lexical sort is also chronological.
    daily = sorted(base_dir.glob("*.jsonl"), reverse=True)[:2]
    # Read oldest-of-the-two first so the combined stream is chronological.
    daily.reverse()

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
            if since_mtime is not None:
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
