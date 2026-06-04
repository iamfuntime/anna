"""Validate the worker hands inbound images to the SDK as a stream-json
user message (Phase 2.6).

When ``event.images`` is set, :meth:`ConversationWorker._handle` must call
``client.query`` with an ``AsyncIterable`` (not a plain string). The single
yielded dict must be a ``user`` message whose ``content`` is a list with a
leading non-empty text block followed by one base64 image block per
attachment, each carrying ``type``/``source.type``/``media_type``/``data``.

Mirrors :mod:`tests.test_worker_flush`: the SDK client is a fake that
captures the ``query`` argument so the test asserts on the prompt.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import ConversationWorker
from anna.transports.base import ImageAttachment, InboundEvent, OutboundMessage


CONV_KEY = "slack:dm:U123"


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


class _CapturingClient:
    """Fake SDK client that captures the ``query`` argument verbatim.

    ``query`` does NOT consume an async-iterable prompt, so the test can
    iterate it afterwards to inspect the stream-json payload.
    """

    def __init__(self, blocks: list[Any]) -> None:
        self._blocks = blocks
        self.queries: list[Any] = []

    async def query(self, prompt: Any) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        yield _FakeAssistantMessage(content=list(self._blocks))
        yield _FakeResultMessage()


@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    # ToolUseBlock is referenced by _handle's isinstance check; a distinct
    # sentinel class is enough since the scripted content never includes one.
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeResultMessage, raising=False)
    yield


def _make_worker(tmp_path: Path, sent: list[OutboundMessage]) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_send,
        visibility=NULL_VISIBILITY,
    )


@pytest.mark.asyncio
async def test_image_event_yields_stream_json_with_image_block(
    tmp_path: Path,
) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _CapturingClient(blocks=[_FakeTextBlock(text="ok")])

    raw = b"\x89PNG\r\n\x1a\n-image-bytes"
    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U123",
        sender_display="Seth",
        text="look at this",
        is_dm=True,
        is_thread=False,
        images=[ImageAttachment(media_type="image/png", data=raw)],
    )

    await worker._handle(event)

    # The captured prompt is an async-iterable, NOT a plain string.
    prompt = worker._client.queries[0]
    assert not isinstance(prompt, str)

    collected = [item async for item in prompt]
    assert len(collected) == 1

    msg = collected[0]
    assert msg["type"] == "user"
    content = msg["message"]["content"]

    # Leading non-empty text block carrying the operator's caption.
    assert content[0]["type"] == "text"
    assert content[0]["text"]
    assert "look at this" in content[0]["text"]

    # Exactly one image block with the full base64 source shape.
    image_blocks = [b for b in content if b.get("type") == "image"]
    assert len(image_blocks) == 1
    source = image_blocks[0]["source"]
    assert source["type"] == "base64"
    assert source["media_type"] == "image/png"
    assert source["data"] == base64.b64encode(raw).decode()


@pytest.mark.asyncio
async def test_text_only_event_uses_string_prompt(tmp_path: Path) -> None:
    """No images -> the worker keeps the byte-for-byte string query path."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _CapturingClient(blocks=[_FakeTextBlock(text="ok")])

    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U123",
        sender_display="Seth",
        text="plain text turn",
        is_dm=True,
        is_thread=False,
    )

    await worker._handle(event)

    prompt = worker._client.queries[0]
    assert isinstance(prompt, str)
    assert prompt == "plain text turn"
