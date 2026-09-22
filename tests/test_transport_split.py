"""Unit tests for the transport-level message length splitter.

``split_message_text`` (transports/base.py) chunks an oversized outbound
message under each transport's character cap on a safe boundary — newline
first, then any whitespace, then a hard cut as a last resort — so a drip,
the final flush, or a pre-existing long single reply is delivered as a
sequence of messages instead of being rejected (Telegram) or degraded
(Slack). Per the Inbox/2026-06-04 periodic-flush plan, decision C.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.transports.base import (
    SLACK_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    OutboundMessage,
    split_headline,
    split_message_text,
    split_on_paragraphs,
)
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation


def test_under_limit_returns_single_chunk() -> None:
    assert split_message_text("hello", 100) == ["hello"]


def test_exactly_at_limit_returns_single_chunk() -> None:
    text = "x" * 10
    assert split_message_text(text, 10) == [text]


def test_empty_string_returns_single_chunk() -> None:
    assert split_message_text("", 10) == [""]


def test_splits_on_newline_boundary() -> None:
    # Two lines, each 6 chars incl newline; limit 8 forces a split and the
    # newline is the preferred boundary (and is consumed, not re-emitted).
    text = "aaaaa\nbbbbb"
    chunks = split_message_text(text, 8)
    assert chunks == ["aaaaa", "bbbbb"]
    assert "".join(chunks) == text.replace("\n", "")


def test_splits_on_whitespace_when_no_newline() -> None:
    text = "alpha beta gamma"
    chunks = split_message_text(text, 11)
    # Boundary at the last space <= 11 ("alpha beta" is 10).
    assert chunks == ["alpha beta", "gamma"]


def test_hard_cut_when_no_whitespace() -> None:
    text = "x" * 25
    chunks = split_message_text(text, 10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]
    assert "".join(chunks) == text


def test_multi_chunk_ordering_preserved() -> None:
    text = "one\ntwo\nthree\nfour"
    chunks = split_message_text(text, 7)
    # Reassembling (newlines were the cut points) preserves token order.
    assert chunks == ["one\ntwo", "three", "four"]


def test_every_chunk_within_limit() -> None:
    text = ("word " * 500).strip()
    chunks = split_message_text(text, 40)
    assert len(chunks) > 1
    assert all(len(c) <= 40 for c in chunks)


def test_telegram_limit_splits_long_message() -> None:
    text = "y" * (TELEGRAM_MAX_CHARS + 100)
    chunks = split_message_text(text, TELEGRAM_MAX_CHARS)
    assert len(chunks) == 2
    assert all(len(c) <= TELEGRAM_MAX_CHARS for c in chunks)


def test_slack_limit_is_conservative() -> None:
    assert SLACK_MAX_CHARS < TELEGRAM_MAX_CHARS
    text = "z" * (SLACK_MAX_CHARS + 1)
    chunks = split_message_text(text, SLACK_MAX_CHARS)
    assert len(chunks) == 2


def test_zero_limit_rejected() -> None:
    with pytest.raises(ValueError):
        split_message_text("anything", 0)


# ---------------------------------------------------------------------------
# Slack: over-SLACK_MAX_CHARS replies go out as sequential paragraph-split
# messages, first in channel, rest in thread (2026-09-22). No file upload.
# ---------------------------------------------------------------------------

_HEADLINE = "Audit finished: 80 findings, all fixed."
_LONG_BODY = _HEADLINE + "\n\n" + "\n\n".join(
    f"Finding {i:02d}: the schedule store entry was stale and has been fixed."
    for i in range(80)
)


class _StubSlackClient:
    def __init__(self, *, upload_fails: bool = False) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self._upload_fails = upload_fails

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.post_calls.append(kwargs)
        ts = f"1716832700.{len(self.post_calls):06d}"
        return {"ok": True, "channel": "D0DMCHAN1", "ts": ts}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        if self._upload_fails:
            raise RuntimeError("missing_scope: files:write")
        self.upload_calls.append(kwargs)
        return {"ok": True}


def _slack(tmp_path: Path, client: Any) -> SlackAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    tp = ThreadParticipation(
        state_path=tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    )
    adapter = SlackAdapter(config=cfg, thread_participation=tp)
    adapter._client = client  # type: ignore[attr-defined]
    return adapter


def test_split_on_paragraphs_keeps_every_paragraph_whole() -> None:
    chunks = split_on_paragraphs(_LONG_BODY, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n\n".join(chunks) == _LONG_BODY


def test_split_headline_on_an_oversized_first_paragraph_loses_nothing() -> None:
    text = "word " * 1000
    head, rest = split_headline(text, 500)
    assert len(head) <= 500
    assert head + " " + rest == text


async def test_short_slack_reply_is_one_post_no_thread(tmp_path: Path) -> None:
    client = _StubSlackClient()
    await _slack(tmp_path, client).send(
        OutboundMessage(conversation_key="slack:dm:U123", text="short reply")
    )
    assert [c["text"] for c in client.post_calls] == ["short reply"]
    assert client.upload_calls == []


async def test_over_limit_reply_posts_sequential_paragraph_messages_no_file(
    tmp_path: Path,
) -> None:
    """2026-09-22: no ``full-reply.md`` upload and no "see thread" note. The
    reply goes out as normal messages split on paragraphs: first chunk is the
    main message, the rest follow in its thread, in order, losing nothing."""
    assert len(_LONG_BODY) > SLACK_MAX_CHARS
    client = _StubSlackClient()
    await _slack(tmp_path, client).send(
        OutboundMessage(conversation_key="slack:dm:U123", text=_LONG_BODY)
    )

    assert client.upload_calls == []
    first, *replies = client.post_calls
    assert replies
    assert "thread_ts" not in first
    assert first["text"].startswith(_HEADLINE)
    assert "Finding 00" in first["text"]
    assert all(len(c["text"]) <= SLACK_MAX_CHARS for c in client.post_calls)
    assert all(r["thread_ts"] == "1716832700.000001" for r in replies)
    assert not any("in thread" in c["text"] for c in client.post_calls)
    assert [c["text"] for c in client.post_calls] == split_on_paragraphs(
        _LONG_BODY, SLACK_MAX_CHARS
    )
    assert "\n\n".join(c["text"] for c in client.post_calls) == _LONG_BODY


async def test_over_limit_reply_in_a_channel_thread_stays_in_that_thread(
    tmp_path: Path,
) -> None:
    client = _StubSlackClient()
    await _slack(tmp_path, client).send(
        OutboundMessage(
            conversation_key="slack:ch:C0CHAN:1716000000.000100", text=_LONG_BODY
        )
    )
    assert {c["thread_ts"] for c in client.post_calls} == {"1716000000.000100"}
    assert len(client.post_calls) >= 2
    assert client.upload_calls == []
