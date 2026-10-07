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

It also serves the LIVE-worker half of the same problem. The resume block
is built once, at spawn, so a post delivered into the conversation from
outside its worker (a scheduled job, ``slack_post`` — see
:mod:`anna.runtime.delivered_posts`) while that worker is already running
never reaches the session. :func:`delivered_posts_since` finds those rows
past a per-worker high-water mark and :func:`render_delivered_posts` renders
a bounded listing the worker prepends to the next operator turn.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from anna.core.identity import count_tokens
from anna.vault.checkpoint import list_recent_checkpoints

# Bounds on the per-turn listing of posts delivered into a live conversation
# (:func:`render_delivered_posts`): at most this many posts and this many
# characters of post text, newest kept. A day of unanswered heartbeat alerts
# must not be able to bury the operator's message.
DELIVERED_POSTS_MAX = 5
DELIVERED_POSTS_MAX_CHARS = 2000


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
    lines: list[dict] = []
    for obj in _recent_lines(transcripts_dir, conv_key):
        if since_mtime is not None:
            ts = _parse_ts(obj.get("ts"))
            if ts is None or ts <= since_mtime:
                continue
        lines.append(obj)

    if not lines:
        return []

    return _trim_tail(lines, max_turns, max_tokens)


def _recent_lines(transcripts_dir: Path, conv_key: str) -> list[dict]:
    """Parsed lines from the newest one or two daily files, chronological.

    Malformed lines and non-object JSON are skipped defensively; an
    unreadable file is skipped whole. Returns [] when the conversation has
    no transcript directory.
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
            lines.append(obj)
    return lines


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


def _outbound_label(obj: dict) -> str:
    """Speaker label for an outbound transcript line.

    A conversational reply carries no ``source`` and renders as ``anna``.
    A post delivered INTO this conversation from outside its own worker
    (see :mod:`anna.runtime.delivered_posts`) is tagged with its origin, and
    the label says so — the model must be able to tell "I posted this from
    a scheduled job" from "I said this in reply to you":

    * ``schedule:<id>`` -> ``anna (scheduled: <id>)``
    * ``slack_post:<caller conv_key>`` -> ``anna (slack_post from <caller>)``
    * any other non-empty source -> ``anna (<source>)``
    """
    source = obj.get("source")
    if not isinstance(source, str) or not source.strip():
        return "anna"
    source = source.strip()
    kind, _, detail = source.partition(":")
    if kind == "schedule" and detail:
        return f"anna (scheduled: {detail})"
    if kind == "slack_post" and detail:
        return f"anna (slack_post from {detail})"
    return f"anna ({source})"


def render_tail_block(tail: list[dict]) -> str:
    """Render a compact markdown block from a transcript tail.

    Returns "" for an empty tail. Otherwise a heading followed by one line
    per entry: inbound -> ``**you:** <text>``, outbound -> ``**anna:**
    <text>``. An outbound line carrying a ``source`` (a scheduled post or a
    ``slack_post`` delivered into this conversation) names its origin in
    the label, e.g. ``**anna (scheduled: heartbeat):** <text>`` — see
    :func:`_outbound_label`. Any single message longer than ~400 chars is
    truncated with an ellipsis.
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
            out.append(f"**{_outbound_label(obj)}:** {text}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Posts delivered into a LIVE conversation
# ---------------------------------------------------------------------------


def latest_ts(lines: list[dict]) -> float | None:
    """Newest parseable ``ts`` among ``lines`` as an epoch float, or None."""
    stamps = [ts for ts in (_parse_ts(obj.get("ts")) for obj in lines) if ts is not None]
    return max(stamps) if stamps else None


def delivered_posts_since(
    transcripts_dir: Path,
    conv_key: str,
    since_ts: float,
    *,
    exclude_sources: tuple[str, ...] = (),
) -> list[dict]:
    """Return posts delivered into ``conv_key`` strictly after ``since_ts``.

    A delivered post is an ``outbound`` transcript line carrying a non-empty
    ``source`` — written by :func:`anna.runtime.delivered_posts.record_delivered_post`
    for a message that reached the conversation from outside its own worker.
    Conversational replies (no ``source``) and inbound lines never match, so
    nothing the session said or was told itself comes back.

    ``exclude_sources`` drops exact ``source`` matches; a worker passes its
    own ``slack_post:<conv_key>`` so a post it made itself, which its session
    already knows about, is not echoed back to it. Reads the same newest
    one-or-two daily files as :func:`transcript_tail_since`. Chronological.
    """
    posts: list[dict] = []
    for obj in _recent_lines(transcripts_dir, conv_key):
        if obj.get("direction") != "outbound":
            continue
        source = obj.get("source")
        if not isinstance(source, str) or not source.strip():
            continue
        if source in exclude_sources:
            continue
        ts = _parse_ts(obj.get("ts"))
        if ts is None or ts <= since_ts:
            continue
        posts.append(obj)
    return posts


def _format_ts(value: object) -> str:
    """``2026-10-07T15:00:28.412Z`` -> ``2026-10-07 15:00:28Z`` (UTC)."""
    ts = _parse_ts(value)
    if ts is None:
        return "unknown time"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def render_delivered_posts(
    posts: list[dict],
    *,
    max_posts: int = DELIVERED_POSTS_MAX,
    max_chars: int = DELIVERED_POSTS_MAX_CHARS,
) -> tuple[list[str], int]:
    """Render delivered posts as bounded listing lines, newest kept.

    Returns ``(lines, omitted)``. Each kept post is one entry
    ``[<UTC time>] <label>: <text>`` using the same origin label as the
    resume tail (:func:`_outbound_label`), in chronological order. The
    window is filled from the NEWEST post backwards and stops at
    ``max_posts`` posts or ``max_chars`` characters of post text, whichever
    comes first; the newest post is always kept, cut to ``max_chars`` with
    an ellipsis if it alone is over budget. When older posts fall outside
    the window, the first line is a one-line omission marker and ``omitted``
    is their count. An empty ``posts`` returns ``([], 0)``.
    """
    if not posts:
        return [], 0

    kept_reversed: list[tuple[dict, str]] = []
    chars = 0
    for obj in reversed(posts):
        text = (obj.get("text") or "").strip()
        if not kept_reversed:
            if len(text) > max_chars:
                text = text[:max_chars].rstrip() + "..."
        elif len(kept_reversed) >= max_posts or chars + len(text) > max_chars:
            break
        kept_reversed.append((obj, text))
        chars += len(text)

    omitted = len(posts) - len(kept_reversed)
    lines: list[str] = []
    if omitted:
        noun = "post" if omitted == 1 else "posts"
        lines.append(f"({omitted} earlier {noun} omitted)")
    for obj, text in reversed(kept_reversed):
        lines.append(f"[{_format_ts(obj.get('ts'))}] {_outbound_label(obj)}: {text}")
    return lines, omitted
