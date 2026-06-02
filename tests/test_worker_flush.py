"""Validate the worker flushes narration at tool-use boundaries.

When the model narrates -> uses a tool -> narrates -> uses a tool ->
narrates inside a single turn, Slack and Telegram users used to see one
consolidated wall-of-text at the end. The flush-at-tool-use behavior
turns that into three separate OutboundMessages that match the model's
natural cadence.

The scheduler-driven path (``event.completion_future`` set) is exempt:
scheduled jobs want one consolidated return value, not a stream.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import ConversationWorker
from anna.transports.base import InboundEvent, OutboundMessage


CONV_KEY = "slack:channel:dm:U123"


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    """Stand-in for claude_agent_sdk.ToolUseBlock.

    The worker only checks ``isinstance(block, ToolUseBlock)``; it never
    introspects fields, so an empty dataclass is enough.
    """

    name: str = "fake_tool"


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


class _FakeBlocksClient:
    """Fake SDK that yields a scripted sequence of blocks in one AssistantMessage.

    The receive_response coroutine emits one ``AssistantMessage`` whose
    content is exactly ``blocks``, then a trailing ``ResultMessage`` so
    ``_handle`` exits its receive loop.
    """

    def __init__(self, blocks: list[Any]) -> None:
        self._blocks = blocks
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        yield _FakeAssistantMessage(content=list(self._blocks))
        yield _FakeResultMessage()

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
    """Swap the SDK's block / message classes for our fakes so the
    ``isinstance`` checks inside ``_handle`` match the scripted content.
    """
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    yield


def _make_worker(
    tmp_path: Path,
    send_target: list[OutboundMessage],
) -> ConversationWorker:
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
        visibility=NULL_VISIBILITY,
    )


def _make_event(
    *,
    future: asyncio.Future[str] | None = None,
) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U123",
        sender_display="Seth",
        text="Do the thing.",
        is_dm=True,
        is_thread=False,
        completion_future=future,
    )


@pytest.mark.asyncio
async def test_flush_at_tool_use_emits_three_messages(tmp_path: Path) -> None:
    """``[Text("a"), Text("b"), ToolUse, Text("c"), ToolUse, Text("d")]``
    should produce three sends: "a\\nb", "c", "d".
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="a"),
            _FakeTextBlock(text="b"),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="c"),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="d"),
        ]
    )

    await worker._handle(_make_event())

    assert [m.text for m in sent] == ["a\nb", "c", "d"]
    for m in sent:
        assert m.conversation_key == CONV_KEY


@pytest.mark.asyncio
async def test_single_text_block_sends_once(tmp_path: Path) -> None:
    """A chain with no tool use sends exactly one consolidated message."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[_FakeTextBlock(text="hello world")]
    )

    await worker._handle(_make_event())

    assert len(sent) == 1
    assert sent[0].text == "hello world"


@pytest.mark.asyncio
async def test_empty_pending_buffer_is_skipped(tmp_path: Path) -> None:
    """``[Text(""), ToolUse, Text("real reply")]`` skips the empty flush.

    The empty pending buffer at the tool-use boundary contributes no
    OutboundMessage; only the trailing "real reply" lands.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text=""),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="real reply"),
        ]
    )

    await worker._handle(_make_event())

    assert len(sent) == 1
    assert sent[0].text == "real reply"


@pytest.mark.asyncio
async def test_scheduler_path_stays_consolidated(tmp_path: Path) -> None:
    """When ``completion_future`` is set the worker MUST NOT flush mid-turn.

    Scheduled jobs receive one consolidated string via the future and
    the send callback is never invoked.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="a"),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="b"),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.done()
    assert future.result() == "a\nb"


@pytest.mark.asyncio
async def test_tool_use_only_no_text_falls_back_to_no_response(tmp_path: Path) -> None:
    """``[ToolUse, ToolUse]`` (no text at all) sends the "(no response)" fallback.

    Both pending flushes are skipped (empty buffer); the trailing
    interactive send falls through to ``elif not reply_chunks`` and
    emits the legacy "(no response)" placeholder so the operator still
    sees something landed.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[_FakeToolUseBlock(), _FakeToolUseBlock()]
    )

    await worker._handle(_make_event())

    assert len(sent) == 1
    assert sent[0].text == "(no response)"
