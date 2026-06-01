"""Validate the worker resolves a caller-supplied completion_future.

The Phase 2 scheduler dispatches synthetic events with a future attached so
the scheduler can receive the worker's final assistant message in-process
and route it to a destination different from the conv_key's natural target.
Transport-originated events have no future and fall through to the normal
send-back path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker
from anna.transports.base import InboundEvent, OutboundMessage


CONV_KEY = "schedule:test-job:2026-06-08"


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


class _RaiseOnQuery:
    """Fake SDK that raises on query() to exercise the failure path."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        raise self._exc

    async def receive_response(self):
        if False:
            yield None

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


class _FakeReplyClient:
    """Fake SDK that yields ``reply`` and a ResultMessage on receive_response."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text=self._reply)])
        yield _FakeResultMessage()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    yield


def _make_worker(tmp_path: Path, send_target: list[OutboundMessage]) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _send(msg: OutboundMessage) -> None:
        send_target.append(msg)

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_send,
    )


def _make_event(future: asyncio.Future[str] | None) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="anna.scheduler",
        sender_display="ANNA Scheduler",
        text="Compose a brief.",
        is_dm=False,
        is_thread=False,
        completion_future=future,
    )


@pytest.mark.asyncio
async def test_completion_future_receives_reply(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeReplyClient(reply="Morning brief output.")

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future))

    assert future.done()
    assert future.result() == "Morning brief output."
    # The future short-circuit means the send callback is NOT invoked.
    assert sent == []


@pytest.mark.asyncio
async def test_completion_future_exception_on_sdk_error(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _RaiseOnQuery(RuntimeError("rate limit"))

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future))

    assert future.done()
    with pytest.raises(RuntimeError, match=r"rate limit"):
        future.result()
    assert sent == []  # no fallback send when scheduler is waiting


@pytest.mark.asyncio
async def test_no_future_falls_through_to_send(tmp_path: Path) -> None:
    """Transport-originated events have no future; worker hits the normal send."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeReplyClient(reply="Hi from ANNA.")

    await worker._handle(_make_event(future=None))
    assert len(sent) == 1
    assert sent[0].text == "Hi from ANNA."
    assert sent[0].conversation_key == CONV_KEY


@pytest.mark.asyncio
async def test_no_client_sets_future_exception(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = None

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future))
    assert future.done()
    with pytest.raises(RuntimeError, match=r"no SDK client"):
        future.result()


# ---------------------------------------------------------------------------
# Phase 2 §5 subtask 3: streaming-output subscriber on InboundEvent.
# ---------------------------------------------------------------------------


class _FakeMultiBlockClient:
    """Fake SDK that yields multiple TextBlocks across AssistantMessages.

    Each entry in ``blocks_per_message`` becomes one AssistantMessage with
    that list of text fragments as its content. A trailing ResultMessage is
    emitted so ``_handle`` exits the receive loop.
    """

    def __init__(self, blocks_per_message: list[list[str]]) -> None:
        self._blocks_per_message = blocks_per_message
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        for fragments in self._blocks_per_message:
            yield _FakeAssistantMessage(
                content=[_FakeTextBlock(text=f) for f in fragments]
            )
        yield _FakeResultMessage()

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


def _make_event_with_subscriber(
    subscriber: "Any | None",
    *,
    future: asyncio.Future[str] | None = None,
) -> InboundEvent:
    return InboundEvent(
        transport="cli",
        conversation_key=CONV_KEY,
        sender_id="anna.cli",
        sender_display="ANNA CLI",
        text="Stream me something.",
        is_dm=False,
        is_thread=False,
        completion_future=future,
        stream_subscriber=subscriber,
    )


@pytest.mark.asyncio
async def test_stream_subscriber_none_preserves_buffered_path(tmp_path: Path) -> None:
    """A worker handling an event with stream_subscriber=None behaves
    identically to the existing buffered finalize path. No deltas, no
    awaits to nowhere; the single OutboundMessage carries the full reply.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeMultiBlockClient(
        blocks_per_message=[["Hello ", "from ", "ANNA."]]
    )

    await worker._handle(_make_event_with_subscriber(subscriber=None))

    # Buffered finalize: one send, full concatenated reply.
    assert len(sent) == 1
    assert sent[0].conversation_key == CONV_KEY
    # ``reply_chunks`` joins on newlines; three blocks within one
    # AssistantMessage → "Hello \nfrom \nANNA." then ``strip()``.
    assert sent[0].text == "Hello \nfrom \nANNA."


@pytest.mark.asyncio
async def test_stream_subscriber_receives_one_call_per_textblock(tmp_path: Path) -> None:
    """A worker with a recording subscriber receives one call per TextBlock,
    in order, AND the buffered finalize still lands on the send callback.
    Slack/Telegram parity (the audit/transcript path) is preserved while
    the CLI adapter sees live deltas.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    blocks = [["First ", "chunk."], ["Second ", "chunk."]]
    worker._client = _FakeMultiBlockClient(blocks_per_message=blocks)

    recorded: list[str] = []

    async def subscriber(text: str) -> None:
        recorded.append(text)

    await worker._handle(_make_event_with_subscriber(subscriber=subscriber))

    # One call per TextBlock, in the order yielded.
    assert recorded == ["First ", "chunk.", "Second ", "chunk."]

    # Buffered finalize still lands on the send callback.
    assert len(sent) == 1
    assert sent[0].conversation_key == CONV_KEY
    assert sent[0].text == "First \nchunk.\nSecond \nchunk."


@pytest.mark.asyncio
async def test_stream_subscriber_exception_does_not_abort_buffered_finalize(
    tmp_path: Path,
) -> None:
    """A subscriber that raises mid-turn is isolated: the worker logs a
    warning, continues iterating, and the buffered finalize still lands.
    A misbehaving CLI client must never break the Slack/Telegram path.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    blocks = [["Alpha.", "Beta.", "Gamma."]]
    worker._client = _FakeMultiBlockClient(blocks_per_message=blocks)

    seen: list[str] = []

    async def flaky(text: str) -> None:
        seen.append(text)
        if text == "Beta.":
            raise RuntimeError("subscriber boom")

    await worker._handle(_make_event_with_subscriber(subscriber=flaky))

    # All three blocks were dispatched to the subscriber (loop continued
    # past the raise).
    assert seen == ["Alpha.", "Beta.", "Gamma."]

    # Buffered finalize lands on the send callback with the full reply.
    assert len(sent) == 1
    assert sent[0].text == "Alpha.\nBeta.\nGamma."
