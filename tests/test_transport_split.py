"""Unit tests for the transport-level message length splitter.

``split_message_text`` (transports/base.py) chunks an oversized outbound
message under each transport's character cap on a safe boundary — newline
first, then any whitespace, then a hard cut as a last resort — so a drip,
the final flush, or a pre-existing long single reply is delivered as a
sequence of messages instead of being rejected (Telegram) or degraded
(Slack). Per the Inbox/2026-06-04 periodic-flush plan, decision C.
"""

from __future__ import annotations

import pytest

from anna.transports.base import (
    SLACK_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    split_message_text,
)


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
