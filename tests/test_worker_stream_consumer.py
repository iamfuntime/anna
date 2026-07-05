"""Validate the worker's single owned message-stream consumer (stale-turn fix).

The SDK client exposes ONE buffered message stream shared by every
``receive_response()`` caller. Before the fix, an unsolicited turn (the CLI
injects a ``<task-notification>`` user message when a background agent
finishes while the worker is idle, and the model replies) buffered unread in
that stream; the next live drain delivered the STALE reply first, then broke
on the stale ResultMessage — every turn thereafter ran one-turn-behind until
restart.

Now one long-lived consumer task owns the stream: live turns read a per-turn
queue, and idle-time (unsolicited) turns are delivered immediately through
the guarded send path with their ResultMessage discarded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import ConversationWorker
from anna.transports.base import InboundEvent, OutboundMessage

CONV_KEY = "slack:channel:dm:U123"

_CLOSE = object()


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    name: str = "fake_tool"


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


@dataclass
class _FakeUserMessage:
    content: Any
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    tool_use_result: dict[str, Any] | None = None


@dataclass
class _FakeSystemMessage:
    subtype: str
    data: dict[str, Any] = field(default_factory=dict)


class _StreamClient:
    """Fake SDK client with a single shared ``receive_messages`` stream.

    Mirrors the real client's shape: ONE buffered stream that every reader
    shares. ``push`` appends messages to the stream (the test playing the
    CLI); ``on_query`` scripts the response messages pushed when ``query``
    is called. Pushing an ``Exception`` instance makes the stream raise it
    (the consumer-crash scenario); ``_CLOSE`` ends the stream generator.
    """

    def __init__(self) -> None:
        self._stream: asyncio.Queue[Any] = asyncio.Queue()
        self.queries: list[str] = []
        self.on_query: Any = None

    def push(self, *msgs: Any) -> None:
        for msg in msgs:
            self._stream.put_nowait(msg)

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self.on_query is not None:
            self.push(*self.on_query(prompt))

    async def receive_messages(self):
        while True:
            msg = await self._stream.get()
            if msg is _CLOSE:
                return
            if isinstance(msg, Exception):
                raise msg
            yield msg

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
    """Swap the SDK message/block classes for our fakes so the worker's
    ``isinstance`` checks match the scripted content."""
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    monkeypatch.setattr(sdk, "UserMessage", _FakeUserMessage, raising=False)
    monkeypatch.setattr(sdk, "SystemMessage", _FakeSystemMessage, raising=False)
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
    text: str = "Do the thing.",
    future: asyncio.Future[str] | None = None,
) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U123",
        sender_display="Seth",
        text=text,
        is_dm=True,
        is_thread=False,
        completion_future=future,
    )


def _notification_user_message() -> _FakeUserMessage:
    return _FakeUserMessage(
        content=(
            "<task-notification>Background task agent-42 completed. "
            "Output: /tmp/agent-42.md</task-notification>"
        )
    )


def _reply(*texts: str) -> list[Any]:
    """One AssistantMessage per text, then the closing ResultMessage."""
    msgs: list[Any] = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text=t)]) for t in texts
    ]
    msgs.append(_FakeResultMessage())
    return msgs


async def _spin(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unsolicited_turn_delivers_immediately_while_idle(tmp_path: Path) -> None:
    """(a) An unsolicited turn completing while idle is delivered via the
    guarded send path right away; its ResultMessage is discarded so the next
    live turn is unaffected."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(_notification_user_message(), *_reply("bg agent finished: report ready"))
        await _spin()

        assert [m.text for m in sent] == ["bg agent finished: report ready"]
        assert sent[0].conversation_key == CONV_KEY

        # Next live turn must see ONLY its own messages — the unsolicited
        # ResultMessage was discarded, not left to terminate this drain.
        client.on_query = lambda prompt: _reply("live reply")
        await worker._handle(_make_event())
        assert [m.text for m in sent] == [
            "bg agent finished: report ready",
            "live reply",
        ]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_live_turn_unchanged_through_queue(tmp_path: Path) -> None:
    """(b) A normal live turn routes through the per-turn queue and delivers
    exactly its own reply — no behavior change."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _reply("hello world")
    try:
        await worker._handle(_make_event())
        assert [m.text for m in sent] == ["hello world"]
        assert len(client.queries) == 1
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_regression_unsolicited_turn_does_not_offset_live_turn(
    tmp_path: Path,
) -> None:
    """(c) The one-turn-behind regression: an unsolicited turn's messages are
    already buffered in the stream when a live turn starts. The live turn
    must receive ONLY its own reply; the stale text is delivered separately
    through the idle path and its ResultMessage never terminates the live
    drain."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        # Buffer a full unsolicited turn WITHOUT yielding to the event loop,
        # so the consumer has not processed it when the live turn begins —
        # the worst-case interleaving for the historic bug.
        client.push(_notification_user_message(), *_reply("STALE background reply"))

        client.on_query = lambda prompt: _reply("FRESH live reply")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await worker._handle(_make_event(future=future))

        assert future.done()
        assert future.result() == "FRESH live reply"
        # The stale unsolicited text is delivered as its own message, not as
        # any turn's reply.
        assert [m.text for m in sent] == ["STALE background reply"]

        # And the turn AFTER that is also unaffected (no lingering offset).
        client.on_query = lambda prompt: _reply("second live reply")
        await worker._handle(_make_event())
        assert [m.text for m in sent] == [
            "STALE background reply",
            "second live reply",
        ]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_background_completion_mid_turn_delivered_after(tmp_path: Path) -> None:
    """(d) A background task completing mid-turn: the CLI serializes turns,
    so the notification turn's messages arrive after the live turn's
    ResultMessage. They must be delivered by the idle path afterward."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        *_reply("live reply"),
        _notification_user_message(),
        *_reply("bg done while you were talking"),
    ]
    try:
        await worker._handle(_make_event())
        await _spin()
        assert [m.text for m in sent] == [
            "live reply",
            "bg done while you were talking",
        ]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_post_result_leftovers_without_marker_reroute_to_idle(
    tmp_path: Path,
) -> None:
    """(d, variant) Trailing messages that land in the turn queue after its
    own ResultMessage (no injected user message to flag them) are re-routed
    to the idle path by ``_end_turn`` — nothing is lost at the boundary."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        *_reply("live reply"),
        *_reply("trailing unsolicited reply"),
    ]
    try:
        await worker._handle(_make_event())
        await _spin()
        assert [m.text for m in sent] == [
            "live reply",
            "trailing unsolicited reply",
        ]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_consumer_exception_surfaces_and_recovers(tmp_path: Path) -> None:
    """(e) A stream exception mid-turn fails that turn through the existing
    receive-error path, and the next turn restarts the consumer — the worker
    keeps functioning."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [RuntimeError("stream boom")]
    try:
        await worker._handle(_make_event())
        assert len(sent) == 1
        assert "stream boom" in sent[0].text

        # Recovery: the fake's raising generator is dead, but the next turn
        # re-ensures a fresh consumer over the same stream.
        client.on_query = lambda prompt: _reply("recovered reply")
        await worker._handle(_make_event())
        assert sent[-1].text == "recovered reply"
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_idle_consumer_exception_is_retried(tmp_path: Path) -> None:
    """(e, variant) An exception while NO turn is active is logged and the
    consumer retries the stream, so a later unsolicited turn still delivers."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(RuntimeError("idle stream hiccup"))
        client.push(*_reply("after the hiccup"))
        # The retry path sleeps 1s between attempts; poll briefly for the
        # delivery instead of a fixed spin.
        for _ in range(300):
            if sent:
                break
            await asyncio.sleep(0.01)
        assert [m.text for m in sent] == ["after the hiccup"]
        assert worker._consumer_task is not None and not worker._consumer_task.done()
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_clean_stream_end_mid_turn_fails_turn_not_hangs(tmp_path: Path) -> None:
    """A CLEAN stream end mid-turn (the CLI dying closes the stream with an
    end control message, no exception) must fail the turn through the
    existing receive-error path instead of hanging the drain forever."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    # Some narration, then the stream ends cleanly — NO ResultMessage.
    client.on_query = lambda prompt: [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="partial...")]),
        _CLOSE,
    ]
    try:
        await asyncio.wait_for(worker._handle(_make_event()), timeout=5.0)
        assert len(sent) >= 1
        assert "SDK message stream ended mid-turn" in sent[-1].text

        # And the worker keeps functioning: the next turn replaces the dead
        # consumer and completes normally.
        client.on_query = lambda prompt: _reply("recovered reply")
        await asyncio.wait_for(worker._handle(_make_event()), timeout=5.0)
        assert sent[-1].text == "recovered reply"
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_wedged_unsolicited_open_reset_on_consumer_replacement(
    tmp_path: Path,
) -> None:
    """A half-observed unsolicited turn (opened, never closed) whose consumer
    died must not divert the next live turn: replacing the dead consumer
    resets ``_unsolicited_open`` / ``_idle_chunks``."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        # Open an unsolicited turn (assistant text, no Result), then end the
        # stream cleanly while idle — the consumer dies with the flag wedged.
        client.push(
            _FakeAssistantMessage(content=[_FakeTextBlock(text="orphaned half-turn")]),
            _CLOSE,
        )
        await _spin()
        assert worker._unsolicited_open is True
        assert worker._consumer_task is not None and worker._consumer_task.done()

        # The next live turn replaces the consumer, resets the wedge, and
        # completes — without the reset it would hang on the idle diversion.
        client.on_query = lambda prompt: _reply("live reply")
        await asyncio.wait_for(worker._handle(_make_event()), timeout=5.0)
        assert worker._unsolicited_open is False
        # The orphaned chunk was discarded, never delivered.
        assert [m.text for m in sent] == ["live reply"]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_wedged_unsolicited_open_reset_on_idle_retry(tmp_path: Path) -> None:
    """An idle stream error mid-unsolicited-turn re-creates the stream
    generator; the half-tracked state is dropped so a later complete
    unsolicited turn delivers ONLY its own text."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _FakeAssistantMessage(content=[_FakeTextBlock(text="orphaned half-turn")]),
            RuntimeError("idle stream hiccup"),
        )
        client.push(*_reply("fresh unsolicited reply"))
        # The retry path sleeps 1s between attempts; poll for the delivery.
        for _ in range(300):
            if sent:
                break
            await asyncio.sleep(0.01)
        assert [m.text for m in sent] == ["fresh unsolicited reply"]
        assert worker._unsolicited_open is False
        assert worker._idle_chunks == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_turn_message_timeout_backstop(tmp_path: Path, monkeypatch) -> None:
    """The per-message ``wait_for`` bound fails a silent turn through the
    existing receive-error path instead of hanging (backstop for any hang
    path the ``_StreamError`` sentinel machinery misses)."""
    import anna.runtime.worker as worker_mod

    monkeypatch.setattr(worker_mod, "_TURN_MESSAGE_TIMEOUT_SECONDS", 0.05)

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    # query() pushes nothing: the consumer stays healthy but silent, so only
    # the timeout can end the drain.
    try:
        await asyncio.wait_for(worker._handle(_make_event()), timeout=5.0)
        assert len(sent) == 1
        assert "no SDK message within" in sent[0].text
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_checkpoint_summary_drains_through_queue(tmp_path: Path) -> None:
    """(f) The closeout checkpoint summary drains via the per-turn queue."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _reply("summary of the conversation")
    try:
        summary = await worker._ask_checkpoint_summary()
        assert summary == "summary of the conversation"
        assert sent == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_regenerate_scheduled_reply_drains_through_queue(tmp_path: Path) -> None:
    """(f) The scheduled-turn regeneration path drains via the per-turn queue."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _reply("clean regenerated reply")
    try:
        regenerated = await worker._regenerate_scheduled_reply(
            _make_event(), "correction prompt"
        )
        assert regenerated == "clean regenerated reply"
        assert sent == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_system_task_notification_is_observational_only(tmp_path: Path) -> None:
    """A ``task_notification`` SystemMessage while idle neither opens an
    unsolicited turn nor delivers anything, and the next live turn is
    unaffected (it can never wedge routing away from a live turn)."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _FakeSystemMessage(
                subtype="task_notification",
                data={"task_id": "t1", "status": "completed"},
            )
        )
        await _spin()
        assert sent == []
        assert worker._unsolicited_open is False

        client.on_query = lambda prompt: _reply("live reply")
        await worker._handle(_make_event())
        assert [m.text for m in sent] == ["live reply"]
    finally:
        await worker._stop_stream_consumer()


class _TrackedCloseClient(_StreamClient):
    """`_StreamClient` that counts ``__aexit__`` (disconnect) calls.

    ``fail_close`` makes the disconnect raise, for the swallow-and-log
    teardown path.
    """

    def __init__(self, *, fail_close: bool = False) -> None:
        super().__init__()
        self.aexit_calls = 0
        self.fail_close = fail_close

    async def __aexit__(self, *_a):
        self.aexit_calls += 1
        if self.fail_close:
            raise RuntimeError("transport already gone")
        return None


def _closeout_ready_worker(
    tmp_path: Path, client: _TrackedCloseClient
) -> ConversationWorker:
    """Worker wired to ``client`` with a scripted closeout-summary reply."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client.on_query = lambda prompt: _reply("closeout summary")
    worker._client = client
    worker._ensure_stream_consumer()
    return worker


@pytest.mark.asyncio
async def test_stop_disconnects_sdk_client(tmp_path: Path) -> None:
    """(g) Worker closeout must terminate the SDK client: stop() awaits the
    client's ``__aexit__`` (which ends the bundled ``claude`` subprocess)
    exactly once and clears the handle."""
    client = _TrackedCloseClient()
    worker = _closeout_ready_worker(tmp_path, client)

    await asyncio.wait_for(worker.stop(), timeout=10)

    assert client.aexit_calls == 1
    assert worker._client is None
    assert worker._consumer_task is None
    # Idempotent: a second stop() (idle-watcher / router-shutdown race)
    # neither double-disconnects nor raises.
    await asyncio.wait_for(worker.stop(), timeout=10)
    assert client.aexit_calls == 1


@pytest.mark.asyncio
async def test_idle_close_stop_inside_watcher_task_disconnects_client(
    tmp_path: Path,
) -> None:
    """(g) THE subprocess-leak regression. In production the idle watcher
    task itself awaits the router callback, which awaits worker.stop() — so
    stop() runs INSIDE the task registered as ``_idle_task``. Cancelling
    that task from within left its ``cancelling()`` count raised, which
    ``_stop_stream_consumer`` re-raised as CancelledError out of _closeout,
    skipping ``_close_client`` — the checkpoint landed but the SDK's
    ``claude`` subprocess leaked, one per closed worker."""
    client = _TrackedCloseClient()
    worker = _closeout_ready_worker(tmp_path, client)

    async def _idle_watcher_fires() -> None:
        # Shape of _idle_watch -> router _idle_close_callback -> stop().
        await worker.stop()

    watcher = asyncio.create_task(_idle_watcher_fires())
    worker._idle_task = watcher

    # Before the fix the task died with CancelledError here and the client
    # was never disconnected.
    await asyncio.wait_for(asyncio.shield(watcher), timeout=10)

    assert client.aexit_calls == 1
    assert worker._client is None
    assert worker._closed_out is True


@pytest.mark.asyncio
async def test_stop_disconnects_client_when_checkpoint_write_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """(g, robustness) A checkpoint-write crash inside _closeout must not
    skip the disconnect: stop() logs closeout_failed and still awaits
    ``__aexit__`` exactly once."""
    import anna.runtime.worker as worker_mod

    def _boom(**_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worker_mod, "write_checkpoint", _boom)

    client = _TrackedCloseClient()
    worker = _closeout_ready_worker(tmp_path, client)

    await asyncio.wait_for(worker.stop(), timeout=10)

    assert client.aexit_calls == 1
    assert worker._client is None
    assert worker._closed_out is True


@pytest.mark.asyncio
async def test_stop_swallows_client_disconnect_failure(tmp_path: Path) -> None:
    """(g, robustness) A failing disconnect is logged, not raised: stop()
    completes normally and drops the client handle."""
    client = _TrackedCloseClient(fail_close=True)
    worker = _closeout_ready_worker(tmp_path, client)

    await asyncio.wait_for(worker.stop(), timeout=10)

    assert client.aexit_calls == 1
    assert worker._client is None


@pytest.mark.asyncio
async def test_unsolicited_turn_with_empty_text_sends_nothing(tmp_path: Path) -> None:
    """An unsolicited turn that produced no text (e.g. tool-only) delivers
    nothing — the ResultMessage is still discarded silently."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _notification_user_message(),
            _FakeAssistantMessage(content=[_FakeToolUseBlock()]),
            _FakeResultMessage(),
        )
        await _spin()
        assert sent == []

        client.on_query = lambda prompt: _reply("live reply")
        await worker._handle(_make_event())
        assert [m.text for m in sent] == ["live reply"]
    finally:
        await worker._stop_stream_consumer()
