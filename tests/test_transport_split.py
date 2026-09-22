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
from anna.transports.fit import TRIM_NOTE, truncate_at_paragraph
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
# Slack: never more than ONE message per reply (2026-09-22). Over
# SLACK_MAX_CHARS the reply is tuned by one model call; if that fails or still
# does not fit it is truncated at a paragraph boundary with a trimmed note.
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


class _StubTuner:
    def __init__(self, *, result: str | None = None, fails: bool = False) -> None:
        self.calls: list[tuple[str, int]] = []
        self._result = result
        self._fails = fails

    async def __call__(self, text: str, limit: int) -> str:
        self.calls.append((text, limit))
        if self._fails:
            raise RuntimeError("model unavailable")
        assert self._result is not None
        return self._result


def _slack(tmp_path: Path, client: Any, tuner: Any = None) -> SlackAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    tp = ThreadParticipation(
        state_path=tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    )
    adapter = SlackAdapter(
        config=cfg, thread_participation=tp, tuner=tuner or _StubTuner(fails=True)
    )
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


async def test_under_limit_reply_passes_through_untouched(tmp_path: Path) -> None:
    client = _StubSlackClient()
    tuner = _StubTuner(result="should not be used")
    await _slack(tmp_path, client, tuner).send(
        OutboundMessage(conversation_key="slack:dm:U123", text="short reply")
    )
    assert [c["text"] for c in client.post_calls] == ["short reply"]
    assert tuner.calls == []
    assert client.upload_calls == []


@pytest.mark.parametrize(
    "conv_key", ["slack:dm:U123", "slack:ch:C0CHAN:1716000000.000100"]
)
async def test_over_limit_reply_is_tuned_once_and_posts_one_message(
    tmp_path: Path, conv_key: str
) -> None:
    assert len(_LONG_BODY) > SLACK_MAX_CHARS
    tuned = _HEADLINE + "\n\nAll 80 stale schedule entries fixed. Nothing for you to decide."
    client = _StubSlackClient()
    tuner = _StubTuner(result=tuned)
    await _slack(tmp_path, client, tuner).send(
        OutboundMessage(conversation_key=conv_key, text=_LONG_BODY)
    )
    assert len(tuner.calls) == 1
    sent_text, asked_limit = tuner.calls[0]
    assert sent_text == _LONG_BODY
    assert asked_limit < SLACK_MAX_CHARS
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["text"] == tuned
    assert client.upload_calls == []
    if conv_key.startswith("slack:ch:"):
        assert client.post_calls[0]["thread_ts"] == "1716000000.000100"
    else:
        assert "thread_ts" not in client.post_calls[0]


async def test_tune_failure_falls_back_to_paragraph_truncation_one_message(
    tmp_path: Path,
) -> None:
    client = _StubSlackClient()
    tuner = _StubTuner(fails=True)
    await _slack(tmp_path, client, tuner).send(
        OutboundMessage(conversation_key="slack:dm:U123", text=_LONG_BODY)
    )
    assert len(tuner.calls) == 1
    assert len(client.post_calls) == 1
    text = client.post_calls[0]["text"]
    assert len(text) <= SLACK_MAX_CHARS
    assert text.startswith(_HEADLINE)
    assert text.endswith(TRIM_NOTE)
    body = text[: -len(TRIM_NOTE)].rstrip()
    # Cut on a paragraph boundary: every kept paragraph is a whole original one.
    originals = set(_LONG_BODY.split("\n\n"))
    assert all(p in originals for p in body.split("\n\n"))


async def test_tuned_text_still_over_limit_falls_back_to_truncation(
    tmp_path: Path,
) -> None:
    client = _StubSlackClient()
    tuner = _StubTuner(result=_LONG_BODY)  # model ignored the limit
    await _slack(tmp_path, client, tuner).send(
        OutboundMessage(conversation_key="slack:dm:U123", text=_LONG_BODY)
    )
    assert len(tuner.calls) == 1
    assert len(client.post_calls) == 1
    assert len(client.post_calls[0]["text"]) <= SLACK_MAX_CHARS
    assert client.post_calls[0]["text"].endswith(TRIM_NOTE)


def test_truncate_at_paragraph_oversized_first_paragraph() -> None:
    text = "word " * 2000
    out = truncate_at_paragraph(text, 500)
    assert len(out) <= 500
    assert out.endswith(TRIM_NOTE)
    assert not out[: -len(TRIM_NOTE)].rstrip().endswith("wor")
