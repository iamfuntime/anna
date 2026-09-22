"""Fit an over-long Slack reply into ONE message (2026-09-22).

The operator does not want replies split across several Slack messages, and
does not want them blindly truncated either. When a rendered reply is over
``SLACK_MAX_CHARS`` the Slack adapter makes ONE extra model call (same model
and auth the daemon already uses) asking for a tighter rewrite that keeps the
result and any decision the operator must make, cutting process, recap and
secondary detail first. If that rewrite still does not fit, or the call fails,
the reply is truncated cleanly at a paragraph boundary with a short note.
Either way exactly one message goes out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from anna.log import get_logger
from anna.transports.base import split_message_text

# ``tuner(text, limit) -> rewritten text``. Raising, or returning something
# empty or still over the limit, triggers the truncation fallback.
Tuner = Callable[[str, int], Awaitable[str]]

TRIM_NOTE = "_(trimmed to fit one message)_"

# Headroom between the length we ask the model for and the hard limit, so a
# slightly-over rewrite (or mrkdwn normalization growing it) still fits.
TUNE_HEADROOM = 300

TUNE_TIMEOUT_S = 120.0


def tune_instruction(limit: int) -> str:
    return (
        f"Rewrite this to fit under {limit} characters. Keep the result and "
        "any decision Seth must make. Cut process, recap and secondary detail "
        "first. Same voice, Slack mrkdwn."
    )


def truncate_at_paragraph(text: str, limit: int, note: str = TRIM_NOTE) -> str:
    """Cut ``text`` to ``<= limit`` chars at a paragraph boundary, add ``note``.

    Whole ``\\n\\n``-separated paragraphs are kept greedily. If even the first
    paragraph does not fit, it is cut at the last line/word boundary instead
    (``split_message_text``). The note is charged against the budget, so the
    result is always ``<= limit``.
    """
    if len(text) <= limit:
        return text
    suffix = "\n\n" + note
    budget = limit - len(suffix)
    if budget <= 0:
        return note[:limit]
    kept = ""
    for para in text.split("\n\n"):
        candidate = f"{kept}\n\n{para}" if kept else para
        if len(candidate) > budget:
            break
        kept = candidate
    if not kept.strip():
        kept = split_message_text(text, budget)[0]
    return kept.rstrip() + suffix


async def fit_one_message(
    text: str,
    limit: int,
    tuner: Tuner | None,
    *,
    normalize: Callable[[str], str] = lambda s: s,
) -> tuple[str, str]:
    """Return ``(text_to_post, outcome)`` with ``len(text_to_post) <= limit``.

    ``outcome`` is ``"fits"`` (untouched), ``"tuned"`` (model rewrite fit) or
    ``"truncated"`` (fallback). The tuner is called at most once.
    """
    if len(text) <= limit:
        return text, "fits"
    log = get_logger("anna.transport.fit")
    if tuner is not None:
        target = max(limit - TUNE_HEADROOM, limit // 2)
        try:
            tuned = await asyncio.wait_for(tuner(text, target), TUNE_TIMEOUT_S)
            tuned = normalize((tuned or "").strip())
            if tuned and len(tuned) <= limit:
                log.info(
                    "channel.reply.tuned",
                    original_length=len(text),
                    tuned_length=len(tuned),
                )
                return tuned, "tuned"
            log.warning(
                "channel.reply.tune_over_limit",
                original_length=len(text),
                tuned_length=len(tuned),
            )
        except Exception as exc:
            log.warning(
                "channel.reply.tune_failed",
                original_length=len(text),
                error=f"{type(exc).__name__}: {exc}",
            )
    return truncate_at_paragraph(text, limit), "truncated"


def build_sdk_tuner(config: Any) -> Tuner:
    """A tuner that makes one tool-less call on the daemon's own model/auth.

    Mirrors the main worker's SDK environment (isolated ``CLAUDE_CONFIG_DIR``,
    shared credentials in max mode, no inherited settings) and model /
    fallback model, with no tools and a single turn.
    """

    async def _tune(text: str, limit: int) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        env = {"CLAUDE_CONFIG_DIR": str(config.claude_runtime_dir)}
        if config.auth.mode == "max":
            env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(
                config.claude_securestorage_dir
            )
        options = ClaudeAgentOptions(
            system_prompt=(
                "You edit a Slack reply that is too long for one message. "
                "Output ONLY the rewritten reply text, nothing else."
            ),
            setting_sources=[],
            env=env,
            tools=[],
            allowed_tools=[],
            max_turns=1,
            model=config.runtime.model,
            fallback_model=config.runtime.fallback_model,
        )
        prompt = f"{tune_instruction(limit)}\n\n---\n{text}\n---"
        parts: list[str] = []
        result_text: str | None = None
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError(f"tune call errored: {msg.result!r}")
                result_text = msg.result
        out = "".join(parts).strip() or (result_text or "").strip()
        if not out:
            raise RuntimeError("tune call returned no text")
        return out

    return _tune
