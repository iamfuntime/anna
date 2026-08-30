"""Daemon-layer guard: no user-facing text on a notification-only turn.

2026-07-30 incident. ANNA launched 13 background sub-agents. Each completion
fired a task notification that re-invoked her, she narrated on every one, and
because the Slack transport is buffered the accumulated 184,673 characters
flushed as roughly 45 messages in one second. The operator had raised this
pattern about six times; prompt-layer rules kept failing, so the decision moved
into the daemon.

The rule: when the ONLY inbound that triggered a turn is a background-task /
agent completion notification, the turn still runs — tool calls execute, state
updates — and only the outbound post to the transport is dropped. The instant
ANY genuine user message is part of the turn's inbound, including one the
harness surfaces MID-TURN alongside a tool result, nothing is suppressed.

Covered here:
  a. notification-only turn -> text suppressed
  b. notification + mid-turn user message -> text NOT suppressed (the
     load-bearing case: silencing a real person is the worst failure of this
     change, so it is exercised on both turn shapes)
  c. interactive DM turn -> the guard is unreachable by construction
  d. multiple simultaneous notifications -> suppressed, ONE audit event per
     turn rather than one per notification
  e. tool calls and state still run on a suppressed turn
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import (
    ConversationWorker,
    _NotificationOnlyTurn,
    _operator_text_of,
)
from anna.transports.base import InboundEvent, OutboundMessage

CONV_KEY = "slack:channel:dm:U123"

SUPPRESS_EVENT = "audit.reply.notification_only_suppressed"

_CLOSE = object()


# ---------------------------------------------------------------------------
# SDK fakes (same shapes as test_worker_stream_consumer.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    name: str = "fake_tool"
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeToolResultBlock:
    """Stand-in for claude_agent_sdk.ToolResultBlock.

    Deliberately carries NO ``text`` attribute — that is what makes a tool
    result invisible to ``_operator_text_of`` in the real SDK too.
    """

    tool_use_id: str = "toolu_1"
    content: str | None = "ok"


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
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class _StreamClient:
    """Fake SDK client with a single shared ``receive_messages`` stream."""

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
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    monkeypatch.setattr(sdk, "UserMessage", _FakeUserMessage, raising=False)
    monkeypatch.setattr(sdk, "SystemMessage", _FakeSystemMessage, raising=False)
    yield


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_worker(
    tmp_path: Path,
    send_target: list[OutboundMessage],
    *,
    configure: Any = None,
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    # Applied BEFORE construction: the worker caches these flags in __init__
    # (the no-hot-reload contract), so mutating the config afterwards would
    # have no effect and the test would silently prove nothing.
    if configure is not None:
        configure(cfg)
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


def _operator_event(*, text: str = "Do the thing.") -> InboundEvent:
    """A genuine Slack DM from the operator — no notification marker anywhere."""
    return InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U123",
        sender_display="Seth",
        text=text,
        is_dm=True,
        is_thread=False,
    )


def _background_completion_event() -> InboundEvent:
    """The synthetic event ``deliver_background_completion`` injects when a
    detached delegation finishes. ``raw["background_delegation"]`` is the
    structural marker; only ANNA's own runtime ever writes it.
    """
    return InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="anna.subagent",
        sender_display="ANNA Sub-agent",
        text=(
            "Background delegation abc123 (researcher) finished. "
            "Here is the sub-agent's reply:\n\nAll six sources checked."
        ),
        is_dm=False,
        is_thread=False,
        raw={"background_delegation": True},
    )


def _notification_user_message(task_id: str = "agent-42") -> _FakeUserMessage:
    return _FakeUserMessage(
        content=(
            f"<task-notification>Background task {task_id} completed. "
            f"Output: /tmp/{task_id}.md</task-notification>"
        )
    )


def _system_notification() -> _FakeSystemMessage:
    """The shape the 2026-07-30 flood actually arrived in: twelve of these on
    one idle worker, then one enormous accumulated reply."""
    return _FakeSystemMessage(subtype="task_notification", status="completed")


def _reply(*texts: str) -> list[Any]:
    msgs: list[Any] = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text=t)]) for t in texts
    ]
    msgs.append(_FakeResultMessage())
    return msgs


def _narrated_reply(*narration: str, terminal: str | None = None) -> list[Any]:
    """The realistic shape of a completion-triggered turn.

    Mid-turn ``narration`` blocks, a real tool call, then (optionally) the
    closing TERMINAL report. The tool call is what makes the two separable:
    everything before the LAST tool call is narration, everything after it is
    the report. ``terminal=None`` models a turn that ended ON its tool call
    with no closing report — nothing deliverable.
    """
    msgs: list[Any] = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text=t)]) for t in narration
    ]
    msgs.append(_FakeAssistantMessage(content=[_FakeToolUseBlock()]))
    if terminal is not None:
        msgs.append(_FakeAssistantMessage(content=[_FakeTextBlock(text=terminal)]))
    msgs.append(_FakeResultMessage())
    return msgs


async def _spin(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _audit_records(cfg: AnnaConfig) -> list[dict]:
    out: list[dict] = []
    audit_dir = cfg.audit_dir
    if not audit_dir.exists():
        return out
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _suppression_rows(cfg: AnnaConfig) -> list[dict]:
    return [r for r in _audit_records(cfg) if r.get("event") == SUPPRESS_EVENT]


# ---------------------------------------------------------------------------
# _operator_text_of: the load-bearing discriminator
# ---------------------------------------------------------------------------


def test_operator_text_of_ignores_task_notification() -> None:
    assert _operator_text_of(_notification_user_message()) == ""


def test_operator_text_of_ignores_system_reminder_only() -> None:
    msg = _FakeUserMessage(
        content="<system-reminder>\nCadence: be brief.\n</system-reminder>"
    )
    assert _operator_text_of(msg) == ""


def test_operator_text_of_ignores_tool_result_blocks() -> None:
    # A pure tool-result user message carries no text blocks at all.
    msg = _FakeUserMessage(
        content=[_FakeToolResultBlock()],
        tool_use_result={"ok": True},
        parent_tool_use_id="toolu_1",
    )
    assert _operator_text_of(msg) == ""


def test_operator_text_of_finds_text_alongside_tool_result() -> None:
    """THE case. The harness surfaces a real person's message inside a running
    turn by appending a text block to the user message carrying a tool result —
    and ``tool_use_result`` being set must NOT disqualify it."""
    msg = _FakeUserMessage(
        content=[
            _FakeToolResultBlock(),
            _FakeTextBlock(text="actually hold on, do the other one first"),
        ],
        tool_use_result={"ok": True},
        parent_tool_use_id="toolu_1",
    )
    assert _operator_text_of(msg) == "actually hold on, do the other one first"


def test_operator_text_of_finds_text_beside_notification_block() -> None:
    msg = _FakeUserMessage(
        content=(
            "<task-notification>agent-42 done</task-notification>\n"
            "and while you're there, check the calendar"
        )
    )
    assert _operator_text_of(msg) == "and while you're there, check the calendar"


def test_operator_text_of_empty_for_empty_message() -> None:
    assert _operator_text_of(_FakeUserMessage(content="")) == ""
    assert _operator_text_of(_FakeUserMessage(content=[])) == ""


# ---------------------------------------------------------------------------
# _NotificationOnlyTurn ledger semantics
# ---------------------------------------------------------------------------


def test_ledger_inert_without_sources() -> None:
    assert _NotificationOnlyTurn(turn_id="t1").suppressing is False


def test_ledger_suppresses_with_source() -> None:
    ledger = _NotificationOnlyTurn(turn_id="t1")
    ledger.note_source("system_task_notification")
    assert ledger.suppressing is True


def test_ledger_user_inbound_latch_wins_and_never_unlatches() -> None:
    ledger = _NotificationOnlyTurn(turn_id="t1")
    ledger.note_source("system_task_notification")
    ledger.note_user_inbound()
    assert ledger.suppressing is False
    # More notifications after a person spoke must not re-arm the guard.
    ledger.note_source("task_notification_user")
    assert ledger.suppressing is False


def test_ledger_dedupes_repeated_sources() -> None:
    ledger = _NotificationOnlyTurn(turn_id="t1")
    for _ in range(13):
        ledger.note_source("system_task_notification")
    assert ledger.sources == ["system_task_notification"]


# ---------------------------------------------------------------------------
# (a) notification-only turn -> text suppressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_notification_only_turn_narration_suppressed(tmp_path: Path) -> None:
    """PATH B — the incident path, and 26 of the 29 observed real cases.

    A system task notification wakes an idle worker; the model narrates, calls
    a tool, then writes its report. The narration never reaches Slack and the
    REPORT does. Before 2026-08-30 both were dropped, which is what made the
    guard feel like a black hole.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(),
            *_narrated_reply(
                "Checking what agent 4 produced...",
                terminal="Sub-agent 4 finished: all six sources check out.",
            ),
        )
        await _spin()

        assert [m.text for m in sent] == [
            "Sub-agent 4 finished: all six sources check out."
        ]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert rows[0]["sources"] == ["system_task_notification"]
        assert rows[0]["delivery_outcome"] == "delivered"
        # The withheld blob is still recorded whole, and the head preview still
        # shows the narration it starts with.
        assert rows[0]["preview"].startswith("Checking what agent 4 produced")
        assert rows[0]["turn_id"]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_dispatched_background_completion_narration_suppressed(
    tmp_path: Path,
) -> None:
    """PATH A — the router injects a synthetic event for a finished background
    delegation. Same split: narration dropped, terminal report delivered."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _narrated_reply(
        "Got the sub-agent's report, logging it.",
        terminal="Researcher done: the migration window is 03:00-05:00 UTC.",
    )

    try:
        await worker._handle(_background_completion_event())

        assert [m.text for m in sent] == [
            "Researcher done: the migration window is 03:00-05:00 UTC."
        ]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert rows[0]["sources"] == ["background_delegation"]
        assert rows[0]["delivery_outcome"] == "delivered"
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_no_response_fallback_also_suppressed(tmp_path: Path) -> None:
    """A silent notification turn must not post the ``(no response)``
    placeholder either — that was itself part of the flood."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [_FakeResultMessage()]

    try:
        await worker._handle(_background_completion_event())
        assert sent == []
    finally:
        await worker._stop_stream_consumer()


# ---------------------------------------------------------------------------
# (b) notification + mid-turn user message -> NOT suppressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_turn_operator_message_defeats_suppression(tmp_path: Path) -> None:
    """THE case this change must not get wrong.

    A notification-triggered turn is running; mid-turn the harness surfaces a
    genuine operator message alongside a tool result. Everything from that
    point on is a reply the operator is owed, and it must be delivered.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        # Tool call, then a tool result that CARRIES the operator's message.
        _FakeAssistantMessage(content=[_FakeToolUseBlock()]),
        _FakeUserMessage(
            content=[
                _FakeToolResultBlock(),
                _FakeTextBlock(text="wait — what did agent 4 actually say?"),
            ],
            tool_use_result={"ok": True},
        ),
        *_reply("Agent 4 found three restock candidates."),
    ]

    try:
        await worker._handle(_background_completion_event())

        assert [m.text for m in sent] == ["Agent 4 found three restock candidates."]
        # Nothing was suppressed, so there is no suppression row at all.
        assert _suppression_rows(worker._config) == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_idle_turn_with_operator_text_not_suppressed(tmp_path: Path) -> None:
    """Same rule on the idle path: a notification AND a real person's message
    in one unsolicited turn is not a notification-only turn."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(),
            _FakeUserMessage(content="hey, what's the status?"),
            *_reply("Four of the six agents are done."),
        )
        await _spin()

        assert [m.text for m in sent] == ["Four of the six agents are done."]
        assert _suppression_rows(worker._config) == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_operator_text_arriving_after_a_drop_is_still_answered(
    tmp_path: Path,
) -> None:
    """Worst-case interleaving: narration is already dropped when the operator
    speaks. The reply after they speak must still land, and the earlier drop
    must still be recorded rather than quietly forgotten."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        # Narration flushed at a tool-use boundary -> suppressed.
        _FakeAssistantMessage(
            content=[_FakeTextBlock(text="Reading the sub-agent report"), _FakeToolUseBlock()]
        ),
        _FakeUserMessage(
            content=[_FakeToolResultBlock(), _FakeTextBlock(text="just summarize it")],
            tool_use_result={"ok": True},
        ),
        *_reply("Three candidates, one urgent."),
    ]

    try:
        await worker._handle(_background_completion_event())

        assert [m.text for m in sent] == ["Three candidates, one urgent."]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert rows[0]["char_count"] == len("Reading the sub-agent report")
    finally:
        await worker._stop_stream_consumer()


# ---------------------------------------------------------------------------
# (c) interactive DM turn -> guard unreachable by construction
# ---------------------------------------------------------------------------


def test_operator_event_produces_no_ledger(tmp_path: Path) -> None:
    """The by-construction property. A transport-originated event has no
    ``raw["background_delegation"]``, so no ledger exists to suppress against —
    the guard is not merely inactive on an interactive DM, it is absent.
    """
    worker = _make_worker(tmp_path, [])
    assert worker._notification_turn_for(_operator_event()) is None


def test_operator_typing_the_marker_produces_no_ledger(tmp_path: Path) -> None:
    """The guard keys on ``raw``, which no transport populates, NEVER on the
    text. An operator who quotes the literal notification marker — while asking
    about this very guard, say — must still get an answer.
    """
    worker = _make_worker(tmp_path, [])
    event = _operator_event(
        text="why did <task-notification> stop you replying earlier?"
    )
    assert worker._notification_turn_for(event) is None


@pytest.mark.asyncio
async def test_operator_typing_the_marker_still_gets_a_reply(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _reply("Because the turn had no user message.")

    try:
        await worker._handle(
            _operator_event(text="what does <task-notification> mean?")
        )
        assert [m.text for m in sent] == ["Because the turn had no user message."]
    finally:
        await worker._stop_stream_consumer()


def test_scheduled_turn_produces_no_ledger(tmp_path: Path) -> None:
    """A scheduler-driven turn resolves a future instead of sending and owns
    its own guards, so it is excluded even if a marker were present."""
    worker = _make_worker(tmp_path, [])
    loop = asyncio.new_event_loop()
    try:
        future: asyncio.Future[str] = loop.create_future()
        event = InboundEvent(
            transport="slack",
            conversation_key=CONV_KEY,
            sender_id="scheduler",
            sender_display="Scheduler",
            text="<task-notification>x</task-notification>",
            is_dm=False,
            is_thread=False,
            raw={"background_delegation": True},
            completion_future=future,
        )
        assert worker._notification_turn_for(event) is None
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_interactive_dm_turn_delivers_normally(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _reply("Yes — three of them shipped.")

    try:
        await worker._handle(_operator_event())

        assert [m.text for m in sent] == ["Yes — three of them shipped."]
        assert worker._notification_turn is None
        assert _suppression_rows(worker._config) == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_interactive_dm_turn_with_tool_narration_delivers_every_flush(
    tmp_path: Path,
) -> None:
    """The multi-message interactive shape (narrate, tool, narrate) is
    untouched: the guard must not eat a single boundary flush."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        _FakeAssistantMessage(
            content=[_FakeTextBlock(text="Checking now"), _FakeToolUseBlock()]
        ),
        *_reply("All clear."),
    ]

    try:
        await worker._handle(_operator_event())
        assert [m.text for m in sent] == ["Checking now", "All clear."]
    finally:
        await worker._stop_stream_consumer()


# ---------------------------------------------------------------------------
# (d) many simultaneous notifications -> one audit event per TURN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thirteen_notifications_one_turn_one_audit_row(tmp_path: Path) -> None:
    """The incident at full scale. Thirteen completions land on one idle
    worker; the accumulated narration is dropped and exactly ONE audit row is
    written — per turn, not per notification."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        for _ in range(13):
            client.push(_system_notification())
        client.push(
            *_narrated_reply(
                "agent 1 done",
                "agent 2 done",
                "agent 3 done",
                terminal="All 13 agents are in; three found nothing new.",
            )
        )
        await _spin()

        # Thirteen completions collapsing onto ONE turn still cost exactly one
        # message: the turn's single terminal report.
        assert [m.text for m in sent] == [
            "All 13 agents are in; three found nothing new."
        ]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        # Thirteen notifications collapse to one deduplicated source list.
        assert rows[0]["sources"] == ["system_task_notification"]
        assert rows[0]["send_count"] == 1
        assert rows[0]["char_count"] == len(
            "agent 1 done\nagent 2 done\nagent 3 done\n"
            "All 13 agents are in; three found nothing new."
        )
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_both_notification_kinds_recorded_on_one_row(tmp_path: Path) -> None:
    """A turn woken by both notification shapes records both sources, once."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(),
            _notification_user_message(),
            _system_notification(),
            *_narrated_reply("both kinds landed", terminal="Both are filed."),
        )
        await _spin()

        assert [m.text for m in sent] == ["Both are filed."]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert sorted(rows[0]["sources"]) == [
            "system_task_notification",
            "task_notification_user",
        ]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_consecutive_notification_turns_audit_once_each(tmp_path: Path) -> None:
    """Two separate notification turns are two turns: one row each, and the
    second turn's ledger is not polluted by the first."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(), *_narrated_reply("first", terminal="first report")
        )
        await _spin()
        client.push(
            _system_notification(), *_narrated_reply("second", terminal="second report")
        )
        await _spin()

        assert [m.text for m in sent] == ["first report", "second report"]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 2
        assert [r["char_count"] for r in rows] == [
            len("first\nfirst report"),
            len("second\nsecond report"),
        ]
        assert rows[0]["turn_id"] != rows[1]["turn_id"]
        # Each turn's terminal accumulator starts empty: turn 2 must not
        # re-deliver turn 1's report.
        assert worker._idle_terminal_chunks == []
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_unsolicited_turn_without_notification_still_delivers(
    tmp_path: Path,
) -> None:
    """Fail-open: an unsolicited turn with no notification behind it keeps
    today's delivery behavior. The guard only fires on a positive signal."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(*_reply("unprompted but not notification-driven"))
        await _spin()

        assert [m.text for m in sent] == ["unprompted but not notification-driven"]
        assert _suppression_rows(worker._config) == []
    finally:
        await worker._stop_stream_consumer()


# ---------------------------------------------------------------------------
# (e) the turn still RUNS — only the outbound post is dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppressed_turn_still_queries_and_runs_tools(tmp_path: Path) -> None:
    """Suppression is an outbound-only decision: the query is issued, tool
    calls execute, and the turn's own bookkeeping advances."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        _FakeAssistantMessage(
            content=[
                _FakeTextBlock(text="Filing the report"),
                _FakeToolUseBlock(name="Write"),
                _FakeToolUseBlock(name="Edit"),
            ]
        ),
        *_reply("done"),
    ]

    before = worker._turns_since_checkpoint
    try:
        await worker._handle(_background_completion_event())

        # The SDK really was asked to run the turn.
        assert len(client.queries) == 1
        # Turn bookkeeping advanced exactly as on any other real turn.
        assert worker._turns_since_checkpoint == before + 1
        assert worker._dirty is True
        # And only the terminal report was posted — "Filing the report" is
        # mid-turn narration ahead of two tool calls and stays dropped.
        assert [m.text for m in sent] == ["done"]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_ledger_cleared_so_next_turn_is_unaffected(tmp_path: Path) -> None:
    """A suppressed notification turn must not leak its verdict into the
    operator's next turn."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client

    try:
        client.on_query = lambda prompt: _narrated_reply(
            "suppressed narration", terminal="report"
        )
        await worker._handle(_background_completion_event())
        assert [m.text for m in sent] == ["report"]
        assert worker._notification_turn is None

        sent.clear()
        client.on_query = lambda prompt: _reply("here you go")
        await worker._handle(_operator_event())
        assert [m.text for m in sent] == ["here you go"]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_deferred_unsolicited_reply_not_judged_by_the_live_ledger(
    tmp_path: Path,
) -> None:
    """The two ledgers stay separate.

    A notification-only DISPATCHED turn is running when an unrelated
    unsolicited turn — no notification behind it — completes and is deferred to
    ``_end_turn``. The dispatched turn's verdict must not travel to it: the
    live turn's own narration is dropped, the deferred text still ships. This
    is why the live ledger is settled BEFORE ``_end_turn`` rather than after.

    Also pins the ORDERING the terminal-report delivery introduced: the live
    turn's own report is sent in ``_handle``'s finally, ahead of the deferred
    unsolicited text ``_end_turn`` flushes.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="suppress me")]),
        _FakeAssistantMessage(content=[_FakeToolUseBlock()]),
        _FakeAssistantMessage(content=[_FakeTextBlock(text="live report")]),
        _FakeResultMessage(),
        # An unsolicited turn with NO notification, landing behind the live
        # turn's Result so it is deferred rather than delivered inline.
        _FakeAssistantMessage(content=[_FakeTextBlock(text="unrelated unsolicited")]),
        _FakeResultMessage(),
    ]

    try:
        await worker._handle(_background_completion_event())
        await _spin()

        assert [m.text for m in sent] == ["live report", "unrelated unsolicited"]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert rows[0]["char_count"] == len("suppress me") + len("live report")
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_the_turn(tmp_path: Path) -> None:
    """Bookkeeping must never crash a turn: a raising ``audit_event`` is
    logged and swallowed, the narration is still suppressed, and the terminal
    report still ships (delivery must not depend on the audit succeeding)."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _narrated_reply("narration", terminal="report")

    import anna.runtime.worker as worker_mod

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise OSError("audit disk full")

    original = worker_mod.audit_event
    worker_mod.audit_event = _boom  # type: ignore[assignment]
    try:
        await worker._handle(_background_completion_event())
        assert [m.text for m in sent] == ["report"]
    finally:
        worker_mod.audit_event = original  # type: ignore[assignment]
        await worker._stop_stream_consumer()


# ---------------------------------------------------------------------------
# (f) terminal-report delivery (2026-08-30)
#
# The guard was aimed at mid-turn NARRATION; dropping the closing REPORT too
# was collateral damage, and it is the report the operator actually wanted.
# Rule: on a suppressing notification-only turn, deliver the text emitted AFTER
# the last tool call and nothing else. A turn with no tool call is terminal in
# its entirety. An empty terminal block stays silent — never a placeholder.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_b_delivers_terminal_report_only(tmp_path: Path) -> None:
    """PATH B (unsolicited / ``system_task_notification``) — 26 of 29 real
    cases, and the one that fails hardest against HEAD.

    Three narration blocks straddling two tool calls, then the report. Only the
    report ships; none of the narration does, including the block emitted
    BETWEEN the two tool calls.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(),
            _FakeAssistantMessage(content=[_FakeTextBlock(text="Reading the output")]),
            _FakeAssistantMessage(content=[_FakeToolUseBlock(name="Read")]),
            _FakeAssistantMessage(content=[_FakeTextBlock(text="Now cross-checking")]),
            _FakeAssistantMessage(content=[_FakeToolUseBlock(name="Grep")]),
            _FakeAssistantMessage(
                content=[_FakeTextBlock(text="REPORT: three of five links are dead.")]
            ),
            _FakeResultMessage(),
        )
        await _spin()

        assert [m.text for m in sent] == ["REPORT: three of five links are dead."]
        assert "Reading the output" not in "".join(m.text for m in sent)
        assert "Now cross-checking" not in "".join(m.text for m in sent)
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_path_a_delivers_terminal_report_only(tmp_path: Path) -> None:
    """PATH A (dispatched / ``background_delegation``) — same split on the
    other turn shape, including narration between two tool calls."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="Opening the report")]),
        _FakeAssistantMessage(content=[_FakeToolUseBlock(name="Read")]),
        _FakeAssistantMessage(content=[_FakeTextBlock(text="Filing it")]),
        _FakeAssistantMessage(content=[_FakeToolUseBlock(name="Write")]),
        _FakeAssistantMessage(
            content=[_FakeTextBlock(text="REPORT: the researcher found nothing new.")]
        ),
        _FakeResultMessage(),
    ]

    try:
        await worker._handle(_background_completion_event())

        assert [m.text for m in sent] == [
            "REPORT: the researcher found nothing new."
        ]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_no_tool_call_turn_delivers_everything(tmp_path: Path) -> None:
    """No tool call means no narration/report boundary exists — the whole turn
    IS the report. Suppressing it would be the black-hole behavior again."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(_system_notification(), *_reply("Agent 4 is done.", "Nothing new."))
        await _spin()

        assert [m.text for m in sent] == ["Agent 4 is done.\nNothing new."]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_empty_terminal_block_stays_silent_path_b(tmp_path: Path) -> None:
    """A turn that ends ON its tool call has narrated but never reported.
    There is nothing to deliver, so it stays as silent as it was before."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(),
            *_narrated_reply("Kicking off the follow-up", terminal=None),
        )
        await _spin()

        assert sent == []
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert rows[0]["delivery_outcome"] == "empty"
        assert rows[0]["delivered_chars"] == 0
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_whitespace_only_terminal_block_stays_silent(tmp_path: Path) -> None:
    """Whitespace is not a report."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _narrated_reply("narration", terminal="   \n  ")

    try:
        await worker._handle(_background_completion_event())
        assert sent == []
        rows = _suppression_rows(worker._config)
        assert rows[0]["delivery_outcome"] == "empty"
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_flood_cap_falls_back_to_suppression(tmp_path: Path) -> None:
    """The 2026-07-30 shape with the fix applied: 13 SEPARATE completion turns
    would be 13 messages. The per-worker count-in-window cap clips that to the
    configured maximum and audits the rest under a DISTINCT event."""
    sent: list[OutboundMessage] = []

    def _cap_at_three(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.notification_terminal_max_per_window = 3

    worker = _make_worker(tmp_path, sent, configure=_cap_at_three)
    client = _StreamClient()
    worker._client = client

    try:
        for i in range(6):
            client.on_query = lambda prompt, i=i: _narrated_reply(
                f"narration {i}", terminal=f"report {i}"
            )
            await worker._handle(_background_completion_event())

        assert [m.text for m in sent] == ["report 0", "report 1", "report 2"]
        rows = _suppression_rows(worker._config)
        assert len(rows) == 6
        assert [r["delivery_outcome"] for r in rows] == [
            "delivered",
            "delivered",
            "delivered",
            "rate_limited",
            "rate_limited",
            "rate_limited",
        ]
        limited = [
            r
            for r in _audit_records(worker._config)
            if r.get("event") == "audit.reply.notification_terminal_rate_limited"
        ]
        assert len(limited) == 3
        # The report we refused to send is recoverable from the log.
        assert limited[0]["preview"] == "report 3"
        assert limited[0]["max_per_window"] == 3
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_flood_cap_window_expiry_reopens_delivery(tmp_path: Path) -> None:
    """The cap is a count IN A WINDOW, not a lifetime budget: once the window
    has passed, delivery resumes. Exercised by ageing the recorded timestamps
    rather than sleeping, since there is no timer to wait on."""
    sent: list[OutboundMessage] = []

    def _cap_at_one(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.notification_terminal_max_per_window = 1
        cfg.runtime.visibility.notification_terminal_window_seconds = 60

    worker = _make_worker(tmp_path, sent, configure=_cap_at_one)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _narrated_reply("n", terminal="report")

    try:
        await worker._handle(_background_completion_event())
        await worker._handle(_background_completion_event())
        assert [m.text for m in sent] == ["report"]

        # Age the single recorded send past the window.
        worker._notification_terminal_sends = [
            t - 61 for t in worker._notification_terminal_sends
        ]
        await worker._handle(_background_completion_event())
        assert [m.text for m in sent] == ["report", "report"]
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_config_flag_off_restores_total_suppression_path_a(
    tmp_path: Path,
) -> None:
    """The revert switch. With the flag off, a dispatched notification turn
    behaves exactly as it did before 2026-08-30: nothing ships."""
    sent: list[OutboundMessage] = []

    def _off(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.deliver_notification_terminal_reports = False

    worker = _make_worker(tmp_path, sent, configure=_off)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: _narrated_reply("narration", terminal="report")

    try:
        await worker._handle(_background_completion_event())
        assert sent == []
        rows = _suppression_rows(worker._config)
        assert rows[0]["delivery_outcome"] == "disabled"
        assert rows[0]["delivered_chars"] == 0
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_config_flag_off_restores_total_suppression_path_b(
    tmp_path: Path,
) -> None:
    """Same switch, unsolicited path — including the no-tool-call turn that
    the new behavior would otherwise deliver whole."""
    sent: list[OutboundMessage] = []

    def _off(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.deliver_notification_terminal_reports = False

    worker = _make_worker(tmp_path, sent, configure=_off)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(_system_notification(), *_reply("no tools, all report"))
        await _spin()

        assert sent == []
        rows = _suppression_rows(worker._config)
        assert rows[0]["delivery_outcome"] == "disabled"
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_user_inbound_latch_still_forces_full_delivery(tmp_path: Path) -> None:
    """THE guarantee that must survive: once an operator is in the turn, the
    terminal-report filter must NOT apply — she gets the mid-turn narration
    too, not a report-only digest.

    The turn narrates, calls a tool, the operator speaks (latch), then it
    narrates again, calls a SECOND tool, and closes. Everything after the latch
    ships: both the narration flushed at the second tool boundary and the
    closing text. Under terminal-only filtering only the closing text would
    survive, so this fails loudly if the filter ever leaks past the latch.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    client.on_query = lambda prompt: [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="Checking on agent 4")]),
        _FakeAssistantMessage(content=[_FakeToolUseBlock()]),
        # The harness surfaces the operator alongside the tool result.
        _FakeUserMessage(
            content=[
                _FakeToolResultBlock(),
                _FakeTextBlock(text="what did it say?"),
            ],
            tool_use_result={"ok": True},
            parent_tool_use_id="toolu_1",
        ),
        _FakeAssistantMessage(content=[_FakeTextBlock(text="It found three links.")]),
        _FakeAssistantMessage(content=[_FakeToolUseBlock(name="Read")]),
        _FakeAssistantMessage(content=[_FakeTextBlock(text="Two of them are dead.")]),
        _FakeResultMessage(),
    ]

    try:
        await worker._handle(_background_completion_event())

        assert [m.text for m in sent] == [
            "It found three links.",
            "Two of them are dead.",
        ]
        # "Checking on agent 4" was flushed at the FIRST tool boundary, which
        # happened BEFORE the operator's message arrived and so before the
        # latch could be set — that drop is pre-existing guard behavior and is
        # still owed its audit row. What matters here is that the ledger ends
        # the turn NOT suppressing, so the terminal-report settle step never
        # runs and never narrows what she receives.
        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        assert rows[0]["send_count"] == 1
        assert rows[0]["delivery_outcome"] == ""
        assert rows[0]["delivered_chars"] == 0
    finally:
        await worker._stop_stream_consumer()


@pytest.mark.asyncio
async def test_audit_row_records_both_sides_of_the_split(tmp_path: Path) -> None:
    """The audit row must not structurally hide what it dropped.

    Head preview = the narration the blob starts with (unchanged field);
    tail preview = the end of what was withheld; delivered_* = what shipped.
    """
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    client = _StreamClient()
    worker._client = client
    worker._ensure_stream_consumer()
    try:
        client.push(
            _system_notification(),
            *_narrated_reply(
                "Kicking off the check", terminal="REPORT: everything is fine."
            ),
        )
        await _spin()

        rows = _suppression_rows(worker._config)
        assert len(rows) == 1
        row = rows[0]
        # Pre-existing fields keep their pre-existing meanings.
        assert row["preview"].startswith("Kicking off the check")
        assert row["char_count"] == len(
            "Kicking off the check\nREPORT: everything is fine."
        )
        assert row["send_count"] == 1
        # Added fields.
        assert row["tail_preview"].endswith("REPORT: everything is fine.")
        assert row["delivered_chars"] == len("REPORT: everything is fine.")
        assert row["delivered_preview"] == "REPORT: everything is fine."
        assert row["delivery_outcome"] == "delivered"
    finally:
        await worker._stop_stream_consumer()


def test_ledger_terminal_text_clears_at_each_tool_boundary() -> None:
    """Unit-level pin on the accumulator: only post-last-tool text survives,
    and a ledger that saw no tool at all keeps everything."""
    ledger = _NotificationOnlyTurn(turn_id="t1")
    ledger.note_terminal_text("a")
    ledger.note_terminal_text("b")
    assert ledger.terminal_text == "a\nb"
    ledger.note_tool_boundary()
    assert ledger.terminal_text == ""
    ledger.note_terminal_text("report")
    assert ledger.terminal_text == "report"
    assert ledger.tool_boundaries == 1
