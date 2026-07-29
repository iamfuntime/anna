"""Scheduler backstop for a scheduled run that narrated instead of acting.

2026-07-29 incident: ``oem-slide-restock-watch`` fired at 12:00 ET, the model
emitted only "I'll read the skill file first" with ZERO tool calls after 2.5s,
and the scheduler posted that fragment to the operator's DM. ``deals-watch``
did the same on 2026-07-25. A healthy run of either takes ~20s and returns the
quiet sentinel.

The guard: a scheduled turn that executed no tool produced an intent, not a
result. Its text is never posted; the run is retried exactly once, and a second
tool-free turn is suppressed and recorded a FAILURE.

Covered here:
  a. zero-tool-call prose triggers exactly one retry, first text never posted
  b. a retry that DOES use tools posts normally and records a success
  c. two tool-free attempts suppress, mark failed, and audit both texts
  d. a zero-tool-call QUIET SENTINEL is still a clean quiet success, no retry
  e. interactive turns are untouched — tool-free prose is normal there
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState
from anna.runtime.scheduler import QUIET_SENTINEL, Scheduler
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import ConversationWorker
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class ScriptedRouter:
    """Router stand-in that replays a script of ``(reply, tool_call_count)``.

    Mirrors the worker's contract on the completion-future path: the reply
    lands on ``completion_future`` and the turn's tool-call count is written
    into ``event.turn_meta`` first. A ``tool_call_count`` of ``None`` models a
    worker that reported nothing at all (the fail-open case). The last script
    entry repeats if more dispatches arrive than were scripted.
    """

    def __init__(self, script: list[tuple[str, int | None]]) -> None:
        self.script = script
        self.dispatched: list[InboundEvent] = []

    async def dispatch(self, event: InboundEvent) -> None:
        self.dispatched.append(event)
        reply, tool_calls = self.script[min(len(self.dispatched) - 1, len(self.script) - 1)]
        if event.turn_meta is not None and tool_calls is not None:
            event.turn_meta["tool_call_count"] = tool_calls
        if event.completion_future is not None and not event.completion_future.done():
            event.completion_future.set_result(reply)


class FakeAdapter(ChannelAdapter):
    name = "fake-slack"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:  # pragma: no cover - not exercised
        return

    async def stop(self) -> None:  # pragma: no cover
        return

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def subscribe(self, handler) -> None:  # pragma: no cover
        return

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    @classmethod
    def conversation_key_for(cls, event):  # pragma: no cover
        return "fake"


class FakeAlerter:
    def __init__(self) -> None:
        self.warned: list[str] = []

    async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warned.append(message)
        return True

    async def critical(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warned.append(message)
        return True

    async def notify_startup(self, message: str) -> bool:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


NARRATION = "I'll read the skill file first"


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.scheduler.poll_interval_seconds = 1
    cfg.scheduler.max_concurrent = 3
    cfg.scheduler.failure_threshold = 3
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _make_schedule(*, id: str = "oem-slide-restock-watch") -> Schedule:
    return Schedule(
        id=id,
        cron="0 12 * * *",
        prompt="Run the restock watch.",
        destination=ScheduleDestination(transport="slack", channel="CWATCH"),
        timeout_seconds=300,
        enabled=True,
        created_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        state=ScheduleState(),
    )


async def _make_scheduler(
    tmp_path: Path, script: list[tuple[str, int | None]]
) -> tuple[AnnaConfig, ScheduleStore, ScriptedRouter, FakeAdapter, Scheduler]:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    router = ScriptedRouter(script)
    adapter = FakeAdapter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )
    return cfg, store, router, adapter, sched


def _read_audit_records(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not audit_dir.exists():
        return out
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _events(records: list[dict], name: str) -> list[dict]:
    return [r for r in records if r.get("event") == name]


# ---------------------------------------------------------------------------
# (a) zero-tool-call prose triggers exactly one retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_tool_call_prose_triggers_one_retry(tmp_path: Path) -> None:
    """The incident shape: prose, no tools. The text must NOT be posted and
    the schedule must be re-run exactly once."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(NARRATION, 0), (QUIET_SENTINEL, 4)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    # Exactly two dispatches: the original plus ONE retry.
    assert len(router.dispatched) == 2
    # Both were scheduled runs against the same schedule conversation.
    assert all(
        e.conversation_key.startswith("schedule:oem-slide-restock-watch:")
        for e in router.dispatched
    )
    # The narration never reached the destination.
    assert [m.text for m in adapter.sent] == []

    audits = _read_audit_records(cfg.audit_dir)
    retries = _events(audits, "audit.schedule.no_tool_call_retry")
    assert len(retries) == 1
    assert retries[0]["schedule_id"] == "oem-slide-restock-watch"
    assert retries[0]["preview"] == NARRATION
    assert retries[0]["level"] == "WARNING"
    # The retry rescued the tick — no abort, no failure.
    assert not _events(audits, "audit.schedule.no_tool_call_abort")
    assert not _events(audits, "audit.schedule.fail")


@pytest.mark.asyncio
async def test_unreported_tool_call_count_fails_open(tmp_path: Path) -> None:
    """A worker that reports no count at all reads as UNKNOWN, not zero: the
    guard stays inert and the reply posts exactly as it does today."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [("Restock alert: 3 SKUs back in stock.", None)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    assert len(router.dispatched) == 1
    assert [m.text for m in adapter.sent] == ["Restock alert: 3 SKUs back in stock."]
    assert not _events(
        _read_audit_records(cfg.audit_dir), "audit.schedule.no_tool_call_retry"
    )


# ---------------------------------------------------------------------------
# (b) a retry that uses tools proceeds through the normal logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_retry_posts_normally(tmp_path: Path) -> None:
    """Retry produced real tool calls and a real report → post it and record
    a normal success. Only the retry's text is sent; the narration is gone."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(NARRATION, 0), ("Restock alert: 3 SKUs back in stock.", 6)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    assert len(router.dispatched) == 2
    assert [m.text for m in adapter.sent] == ["Restock alert: 3 SKUs back in stock."]
    assert adapter.sent[0].conversation_key == "slack:dm:CWATCH"

    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.no_tool_call_retry")
    assert _events(audits, "audit.schedule.complete")
    assert not _events(audits, "audit.schedule.fail")

    state = store.get("oem-slide-restock-watch").state  # type: ignore[union-attr]
    assert state.last_status == "complete"
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_retry_returning_sentinel_suppresses_as_success(tmp_path: Path) -> None:
    """The realistic rescue: the retry runs its tools and finds nothing new,
    so it returns the sentinel. Quiet success, nothing posted, no failure."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(NARRATION, 0), (QUIET_SENTINEL, 5)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    assert adapter.sent == []
    audits = _read_audit_records(cfg.audit_dir)
    complete = _events(audits, "audit.schedule.complete")
    assert complete and complete[0]["suppressed"] is True
    assert not _events(audits, "audit.schedule.fail")

    state = store.get("oem-slide-restock-watch").state  # type: ignore[union-attr]
    assert state.last_status == "complete"
    assert state.consecutive_failures == 0


# ---------------------------------------------------------------------------
# (c) two tool-free attempts: suppress, mark failed, audit both texts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_no_tool_call_suppresses_and_marks_failed(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(NARRATION, 0), ("Let me start by reading the state file.", 0)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    # Exactly two attempts — never a third.
    assert len(router.dispatched) == 2
    # Neither attempt's text reached the destination.
    assert adapter.sent == []

    audits = _read_audit_records(cfg.audit_dir)
    aborts = _events(audits, "audit.schedule.no_tool_call_abort")
    assert len(aborts) == 1
    assert aborts[0]["schedule_id"] == "oem-slide-restock-watch"
    # Both attempts are recoverable in FULL from the audit log — the audit row
    # is the only surviving record of what the model said.
    assert aborts[0]["first_output"] == NARRATION
    assert aborts[0]["retry_output"] == "Let me start by reading the state file."
    assert aborts[0]["level"] == "WARNING"

    # Failure bookkeeping went through the shared path.
    fails = _events(audits, "audit.schedule.fail")
    assert len(fails) == 1
    assert fails[0]["kind"] == "no_tool_call"
    assert fails[0]["consecutive_failures"] == 1
    # A failed run is NOT a completion.
    assert not _events(audits, "audit.schedule.complete")

    state = store.get("oem-slide-restock-watch").state  # type: ignore[union-attr]
    assert state.last_status == "fail"
    assert state.consecutive_failures == 1


@pytest.mark.asyncio
async def test_repeated_no_tool_call_runs_trip_auto_disable(tmp_path: Path) -> None:
    """The failures accumulate like any other, so three bad ticks disable the
    schedule instead of narrating into the operator's DM forever."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(NARRATION, 0)]
    )

    for _ in range(cfg.scheduler.failure_threshold):
        await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    assert adapter.sent == []
    schedule = store.get("oem-slide-restock-watch")
    assert schedule is not None
    assert schedule.state.consecutive_failures == cfg.scheduler.failure_threshold
    assert schedule.enabled is False


# ---------------------------------------------------------------------------
# (d) a zero-tool-call sentinel is a clean quiet success, never a failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        QUIET_SENTINEL,
        f"  {QUIET_SENTINEL}  ",
        # A skill that early-exits on already-resolved state: one narration
        # line, the sentinel, and no tool calls at all.
        f"State says resolved: true — nothing to do.\n{QUIET_SENTINEL}",
    ],
)
@pytest.mark.asyncio
async def test_zero_tool_call_sentinel_suppresses_without_retry(
    tmp_path: Path, reply: str
) -> None:
    """The sentinel check takes precedence over the zero-tool-call check. A
    schedule that legitimately early-exits with no tools must stay a quiet
    SUCCESS — not a retry, and not a failure."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(reply, 0)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    # No retry: one dispatch only.
    assert len(router.dispatched) == 1
    assert adapter.sent == []

    audits = _read_audit_records(cfg.audit_dir)
    assert not _events(audits, "audit.schedule.no_tool_call_retry")
    assert not _events(audits, "audit.schedule.no_tool_call_abort")
    assert not _events(audits, "audit.schedule.fail")
    complete = _events(audits, "audit.schedule.complete")
    assert complete and complete[0]["suppressed"] is True

    state = store.get("oem-slide-restock-watch").state  # type: ignore[union-attr]
    assert state.last_status == "complete"
    assert state.consecutive_failures == 0


@pytest.mark.parametrize("reply", ["", "   ", "."])
@pytest.mark.asyncio
async def test_zero_tool_call_blank_output_keeps_existing_suppression(
    tmp_path: Path, reply: str
) -> None:
    """The blank-output guard also outranks the new check: a blank tick stays
    a suppressed success (2026-07-02 behavior), not a retry."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(reply, 0)]
    )

    await sched._guarded_fire(store.get("oem-slide-restock-watch"))  # type: ignore[arg-type]

    assert len(router.dispatched) == 1
    assert adapter.sent == []
    audits = _read_audit_records(cfg.audit_dir)
    assert not _events(audits, "audit.schedule.no_tool_call_retry")
    assert _events(audits, "audit.schedule.complete")


# ---------------------------------------------------------------------------
# (e) interactive turns are untouched
# ---------------------------------------------------------------------------


CONV_KEY_INTERACTIVE = "slack:dm:U123"
CONV_KEY_SCHEDULED = "schedule:oem-slide-restock-watch:2026-07-29"


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    name: str
    input: dict[str, Any]


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


class _FakeClient:
    """Fake SDK yielding a fixed block sequence then a ResultMessage."""

    def __init__(self, blocks: list[Any]) -> None:
        self._blocks = blocks
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        yield _FakeAssistantMessage(content=self._blocks)
        yield _FakeResultMessage()

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


@pytest.fixture
def _patch_sdk_types(monkeypatch):
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    yield


def _make_worker(
    tmp_path: Path, conv_key: str, sent: list[OutboundMessage]
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    return ConversationWorker(
        conversation_key=conv_key,
        transport="slack",
        config=cfg,
        supervisor=Supervisor(config=cfg),
        send=_send,
        visibility=NULL_VISIBILITY,
    )


@pytest.mark.asyncio
async def test_interactive_tool_free_reply_is_sent_unchanged(
    tmp_path: Path, _patch_sdk_types
) -> None:
    """A DM answered in prose with no tools is normal and correct. The
    interactive path has no completion future and no ``turn_meta``, so the
    backstop cannot see it, let alone suppress or retry it."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, CONV_KEY_INTERACTIVE, sent)
    worker._client = _FakeClient([_FakeTextBlock(text=NARRATION)])

    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY_INTERACTIVE,
        sender_id="U123",
        sender_display="Seth",
        text="what's your read on this?",
        is_dm=True,
        is_thread=False,
    )
    await worker._handle(event)

    assert [m.text for m in sent] == [NARRATION]
    assert event.turn_meta is None
    # Exactly one SDK turn — no retry machinery on the interactive path.
    assert len(worker._client.queries) == 1


@pytest.mark.asyncio
async def test_worker_reports_tool_call_count_to_scheduler(
    tmp_path: Path, _patch_sdk_types
) -> None:
    """Plumbing check: on the completion-future path the worker publishes the
    turn's tool-call count into the caller's ``turn_meta`` — the scheduler's
    only view of whether the turn actually did anything."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, CONV_KEY_SCHEDULED, sent)
    worker._client = _FakeClient(
        [
            _FakeTextBlock(text="checking"),
            _FakeToolUseBlock(name="Read", input={}),
            _FakeToolUseBlock(name="Grep", input={}),
            _FakeTextBlock(text="Restock alert: 3 SKUs back in stock."),
        ]
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    meta: dict[str, Any] = {}
    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY_SCHEDULED,
        sender_id="anna.scheduler",
        sender_display="ANNA Scheduler",
        text="Run the restock watch.",
        is_dm=False,
        is_thread=False,
        completion_future=future,
        turn_meta=meta,
    )
    await worker._handle(event)

    assert meta["tool_call_count"] == 2
    assert future.result() == "Restock alert: 3 SKUs back in stock."
    assert sent == []


@pytest.mark.asyncio
async def test_worker_reports_zero_for_a_narration_only_scheduled_turn(
    tmp_path: Path, _patch_sdk_types
) -> None:
    """The incident turn, at the worker layer: prose, no ToolUseBlock, count
    zero — exactly the signal the scheduler's backstop trips on."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, CONV_KEY_SCHEDULED, sent)
    worker._client = _FakeClient([_FakeTextBlock(text=NARRATION)])

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    meta: dict[str, Any] = {}
    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY_SCHEDULED,
        sender_id="anna.scheduler",
        sender_display="ANNA Scheduler",
        text="Run the restock watch.",
        is_dm=False,
        is_thread=False,
        completion_future=future,
        turn_meta=meta,
    )
    await worker._handle(event)

    assert meta["tool_call_count"] == 0
    assert future.result() == NARRATION
    assert Scheduler._is_no_tool_call_prose(future.result(), meta) is True
