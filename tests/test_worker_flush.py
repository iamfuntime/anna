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
    *,
    consolidate: bool = False,
    consolidate_scheduled: bool = True,
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    # Read at worker construction (no hot-reload), so set it before the
    # ConversationWorker(...) call below.
    cfg.runtime.visibility.consolidate_interactive_turns = consolidate
    # Scheduled-turn consolidation defaults ON here to mirror the production
    # config default (``consolidate_scheduled_turns: bool = True``): a
    # scheduled turn resolves its future with ONLY the terminal report. The
    # two legacy scheduler tests below pin it False to exercise the off
    # switch (full-narration concatenation).
    cfg.runtime.visibility.consolidate_scheduled_turns = consolidate_scheduled
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
    the send callback is never invoked. Pinned to the legacy off switch
    (``consolidate_scheduled=False``) so the future resolves with the FULL
    narration concatenation; the on/default terminal-only behavior is
    covered by ``test_scheduled_terminal_only_*`` below.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, consolidate_scheduled=False)
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


# ---------------------------------------------------------------------------
# Turn-consolidation mode (config: consolidate_interactive_turns)
#
# When ON, an interactive Slack/Telegram turn accumulates ALL narration and
# emits exactly ONE message at turn end — the tool-use-boundary flush is
# skipped AND the timed drip is never started. When OFF (default), the
# per-boundary flush contract above is preserved unchanged. The scheduler
# (``completion_future``) path stays consolidated regardless of the flag.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidate_on_single_message_across_tool_uses(tmp_path: Path) -> None:
    """Flag ON: narration interleaved across MULTIPLE ToolUseBlocks lands as
    exactly ONE consolidated turn-end message, and the timed-drip loop is
    never entered.

    Same block script as ``test_flush_at_tool_use_emits_three_messages`` (which
    yields three sends with the flag OFF); consolidation collapses it to one.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, consolidate=True)

    # Spy: prove the timed-drip task is never started under consolidation.
    # ``_handle`` only calls ``_periodic_flush_loop`` when
    # ``_periodic_flush_active`` is True, so a never-invoked spy is proof.
    loop_starts: list[int] = []
    orig_loop = worker._periodic_flush_loop

    async def _spy_loop(event, buffer):  # type: ignore[no-untyped-def]
        loop_starts.append(1)
        await orig_loop(event, buffer)

    worker._periodic_flush_loop = _spy_loop  # type: ignore[method-assign]

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

    # One message carrying the WHOLE turn's narration, joined in order.
    assert [m.text for m in sent] == ["a\nb\nc\nd"]
    # The drip loop was never entered, and the gate reports inactive.
    assert loop_starts == []
    assert worker._periodic_flush_active(_make_event()) is False


@pytest.mark.asyncio
async def test_consolidate_off_preserves_boundary_flush(tmp_path: Path) -> None:
    """Flag OFF (default): the per-boundary flush contract is unchanged.

    Explicit contrast to the ON case above — same block script yields one
    message per non-empty boundary plus the trailing final send.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, consolidate=False)
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


@pytest.mark.asyncio
async def test_consolidate_on_scheduler_path_still_consolidated(tmp_path: Path) -> None:
    """Flag ON does not disturb the scheduler path: a ``completion_future``
    turn still resolves with one consolidated string and sends nothing.

    The scheduler path was already consolidated (it skips the boundary flush
    branch), so the ``consolidate_interactive_turns`` flag is a no-op for it.
    Pinned to ``consolidate_scheduled=False`` so this isolates the
    INTERACTIVE flag's non-effect on the scheduler path (full concatenation);
    the scheduled terminal-only default is covered separately below.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(
        tmp_path, sent, consolidate=True, consolidate_scheduled=False
    )
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


# ---------------------------------------------------------------------------
# Scheduled-turn terminal-only capture (config: consolidate_scheduled_turns)
#
# When ON (the production default), a SCHEDULED / non-interactive turn
# (``completion_future`` set) resolves its future with ONLY the turn's
# TERMINAL assistant text — the text emitted after the LAST tool call — so
# mid-turn narration that accompanied tool calls never reaches the operator's
# DM. The ``[[ANNA_NO_OUTPUT]]`` quiet sentinel, when it IS the terminal text,
# survives verbatim (the scheduler then suppresses it downstream). A turn that
# narrated but ended on a tool call with no closing report resolves EMPTY so
# the scheduler's blank-output guard suppresses the tick. When OFF the legacy
# full-narration concatenation is preserved. Interactive turns are never
# touched by this flag regardless of its value.
#
# Real incident this locks down (2026-07-12, weekly-synthesis): the Notion
# Leads query was plan-gated and the turn narrated its workaround
# ("query is plan-gated… trying another way… writing the note now.") ABOVE
# the intended digest; only the digest should be posted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_terminal_only_strips_midturn_narration(tmp_path: Path) -> None:
    """Default (flag ON): a scheduled turn with mid-turn narration interleaved
    across tool calls resolves the future with ONLY the terminal report.

    Mirrors the 2026-07-12 weekly-synthesis incident block shape: two
    narration+tool-call boundaries followed by the intended digest. The
    narration is discarded; nothing is sent through the transport (the
    scheduler routes the future result itself).
    """
    sent: list[OutboundMessage] = []
    # No explicit flag → uses the helper/production default (ON).
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="Notion SQL query is plan-gated. Let me try another way."),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="Both query tools are plan-gated. Writing the synthesis note now."),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="Weekly synthesis: 3 leads advanced, 1 stalled."),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.done()
    assert future.result() == "Weekly synthesis: 3 leads advanced, 1 stalled."


@pytest.mark.asyncio
async def test_scheduled_terminal_only_multiblock_report_joined(tmp_path: Path) -> None:
    """Flag ON: multiple text blocks AFTER the last tool call are joined into
    the terminal report; earlier narration is still dropped.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="narration before the tool"),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="report line 1"),
            _FakeTextBlock(text="report line 2"),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.result() == "report line 1\nreport line 2"


@pytest.mark.asyncio
async def test_scheduled_terminal_quiet_sentinel_preserved(tmp_path: Path) -> None:
    """Flag ON: when the TERMINAL text is exactly the quiet sentinel, the
    future resolves with the bare sentinel (mid-turn narration dropped).

    The scheduler's per-line sentinel check then suppresses the post to
    nothing — that downstream suppression is exercised in the scheduler
    tests; here we lock down that the worker hands the bare sentinel across
    the future so that suppression still fires.
    """
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="Nothing new this tick, checking one more source."),
            _FakeToolUseBlock(),
            _FakeTextBlock(text=QUIET_SENTINEL),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.result() == QUIET_SENTINEL


@pytest.mark.asyncio
async def test_scheduled_narration_then_no_terminal_report_resolves_empty(
    tmp_path: Path,
) -> None:
    """Flag ON: a scheduled turn that narrated but ENDED on a tool call (no
    closing report) resolves the future with an EMPTY string.

    There is no terminal report to post, so the narration is discarded and the
    future resolves empty; the scheduler's blank-output guard suppresses the
    tick (a quiet success) rather than leaking the narration.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="Working on it — writing the note now."),
            _FakeToolUseBlock(),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.result() == ""


@pytest.mark.asyncio
async def test_scheduled_no_tools_terminal_equals_full_reply(tmp_path: Path) -> None:
    """Flag ON but no tool ran: the terminal report equals the whole reply, so
    a tool-free scheduled turn is byte-identical to the legacy capture.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="line one"),
            _FakeTextBlock(text="line two"),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.result() == "line one\nline two"


@pytest.mark.asyncio
async def test_scheduled_truly_empty_turn_keeps_no_response(tmp_path: Path) -> None:
    """Flag ON: a scheduled turn that emitted NO assistant text at all keeps
    the legacy "(no response)" placeholder (not an empty suppression).
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(
        blocks=[_FakeToolUseBlock(), _FakeToolUseBlock()]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.result() == "(no response)"


@pytest.mark.asyncio
async def test_scheduled_off_switch_keeps_full_narration(tmp_path: Path) -> None:
    """Flag OFF: the legacy full-narration concatenation is preserved — the
    future resolves with EVERY assistant text block joined in order.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, consolidate_scheduled=False)
    worker._client = _FakeBlocksClient(
        blocks=[
            _FakeTextBlock(text="narration"),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="the report"),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_event(future=future))

    assert sent == []
    assert future.result() == "narration\nthe report"


@pytest.mark.asyncio
async def test_interactive_unaffected_by_scheduled_flag(tmp_path: Path) -> None:
    """The scheduled flag being ON must NOT change interactive-turn behavior.

    Same block script as ``test_flush_at_tool_use_emits_three_messages`` with
    the scheduled flag ON (default) and NO ``completion_future``: the
    per-boundary interactive flush contract is unchanged — three sends.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, consolidate_scheduled=True)
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
