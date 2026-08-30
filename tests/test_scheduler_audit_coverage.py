"""Verify every audit event the scheduled/background paths must emit lands.

Walks each event in the audit vocabulary, exercises the code path that emits
it, and checks the JSONL audit file for the corresponding record. This is the
§12 audit-event-coverage gate.

The vocabulary must grow whenever a guard on these paths gains an audit row,
or the gate silently stops covering it. Two omissions were closed on
2026-07-30: the tool-free-scheduled-turn guard's ``no_tool_call_retry`` /
``no_tool_call_abort`` rows (added 2026-07-29) and the notification-only reply
guard's ``notification_only_suppressed`` row.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import ConversationWorker
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


EXPECTED_EVENTS = (
    "audit.scheduler.start",
    "audit.scheduler.loop_error",
    "audit.schedule.created",
    "audit.schedule.updated",
    "audit.schedule.deleted",
    "audit.schedule.disabled",
    "audit.schedule.fire",
    "audit.schedule.complete",
    "audit.schedule.fail",
    # Tool-free-scheduled-turn guard (2026-07-29 oem-slide-restock-watch).
    "audit.schedule.no_tool_call_retry",
    "audit.schedule.no_tool_call_abort",
    # Notification-only reply guard (2026-07-30 sub-agent flood).
    "audit.reply.notification_only_suppressed",
)


class _FakeAdapter(ChannelAdapter):
    name = "slack"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:  # pragma: no cover
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


class _FakeRouter:
    def __init__(self) -> None:
        self.raise_on_dispatch: Exception | None = None
        # When set, every dispatch publishes this tool-call count into
        # ``turn_meta`` exactly as the worker does on the completion-future
        # path. Left ``None`` for the happy-path sections so the scheduler's
        # no-tool-call backstop reads UNKNOWN and fails open (today's
        # behavior), keeping schedule.complete coverage intact.
        self.tool_call_count: int | None = None
        self.reply = "ok"

    async def dispatch(self, event: InboundEvent) -> None:
        if self.raise_on_dispatch is not None:
            if event.completion_future is not None and not event.completion_future.done():
                event.completion_future.set_exception(self.raise_on_dispatch)
            return
        if event.turn_meta is not None and self.tool_call_count is not None:
            event.turn_meta["tool_call_count"] = self.tool_call_count
        if event.completion_future is not None and not event.completion_future.done():
            event.completion_future.set_result(self.reply)


class _StubClient:
    """Minimal SDK client stand-in for the worker-side coverage section.

    Yields REAL ``claude_agent_sdk`` messages so the worker's ``isinstance``
    checks match without patching the SDK module.
    """

    def __init__(self, text: str, *, end_on_tool_use: bool = False) -> None:
        self._text = text
        self._end_on_tool_use = end_on_tool_use
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )

        yield AssistantMessage(
            content=[TextBlock(text=self._text)], model="claude-opus-5"
        )
        if self._end_on_tool_use:
            # Turn ends ON a tool call: everything above it is mid-turn
            # narration and there is NO terminal report, so the
            # notification-only guard drops the whole turn (2026-08-30
            # terminal-report delivery has nothing to hand back).
            yield AssistantMessage(
                content=[ToolUseBlock(id="toolu_a", name="Write", input={})],
                model="claude-opus-5",
            )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="audit-coverage",
        )


class _FakeAlerter:
    async def warn(self, *_a, **_kw) -> bool:
        return True

    async def critical(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True

    async def notify_startup(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True


async def _never_send(message: OutboundMessage) -> None:
    """Send sink for the worker-side section: the notification-only guard must
    drop the text before it ever gets here."""
    raise AssertionError(f"notification-only turn posted text: {message.text!r}")


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


def _seen_event_names(audit_dir: Path) -> set[str]:
    return {r.get("event") for r in _read_audit_records(audit_dir) if r.get("event")}


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.scheduler.poll_interval_seconds = 0
    cfg.scheduler.failure_threshold = 3
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _make_schedule(
    *,
    id: str = "audit-test",
    enabled: bool = True,
    state: ScheduleState | None = None,
) -> Schedule:
    return Schedule(
        id=id,
        cron="0 6 * * *",
        prompt="x",
        destination=ScheduleDestination(transport="slack", channel="CTEST"),
        timeout_seconds=300,
        enabled=enabled,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        state=state or ScheduleState(),
    )


@pytest.mark.asyncio
async def test_every_expected_event_emits(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    router = _FakeRouter()
    adapter = _FakeAdapter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=_FakeAlerter(),  # type: ignore[arg-type]
    )

    # 1-3: created / fire / complete via happy-path guarded fire.
    await store.create(_make_schedule())
    await sched._guarded_fire(store.get("audit-test"))  # type: ignore[arg-type]

    # 4: updated
    await store.update("audit-test", timeout_seconds=600)

    # 5: deleted (re-create another schedule for fail/disabled coverage)
    await store.delete("audit-test")

    # 6-7: fail x3 -> disabled (drives schedule.fail and schedule.disabled)
    await store.create(_make_schedule(id="will-fail"))
    router.raise_on_dispatch = RuntimeError("synthetic failure")
    for _ in range(3):
        await sched._guarded_fire(store.get("will-fail"))  # type: ignore[arg-type]

    # 7b: the tool-free-scheduled-turn guard (2026-07-29). Postable prose with
    # a REPORTED tool-call count of zero on both attempts drives
    # no_tool_call_retry (first detection) and then no_tool_call_abort.
    router.raise_on_dispatch = None
    router.tool_call_count = 0
    router.reply = "I'll read the skill file first"
    await store.create(_make_schedule(id="narrates-only"))
    await sched._guarded_fire(store.get("narrates-only"))  # type: ignore[arg-type]
    # Restore the fail-open default so nothing below inherits the zero count.
    router.tool_call_count = None
    router.reply = "ok"

    # 7c: the notification-only reply guard (2026-07-30). A turn triggered by
    # NOTHING but a finished background delegation runs to completion and has
    # its user-facing text dropped at the send boundary, which is the row.
    # Shaped to end ON a tool call so there is no terminal report to deliver
    # (2026-08-30), keeping ``_never_send`` a valid assertion.
    worker = ConversationWorker(
        conversation_key="slack:dm:UAUDIT",
        transport="slack",
        config=cfg,
        supervisor=Supervisor(config=cfg),
        send=_never_send,
        visibility=NULL_VISIBILITY,
    )
    worker._client = _StubClient(
        "Sub-agent finished; here is the report...", end_on_tool_use=True
    )
    try:
        await worker._handle(
            InboundEvent(
                transport="slack",
                conversation_key="slack:dm:UAUDIT",
                sender_id="anna.subagent",
                sender_display="ANNA Sub-agent",
                text="Background delegation abc123 (researcher) finished.",
                is_dm=False,
                is_thread=False,
                raw={"background_delegation": True},
            )
        )
    finally:
        await worker._stop_stream_consumer()

    # 8: scheduler.start - exercise via run() with a single-cycle short-circuit
    cfg.scheduler.poll_interval_seconds = 100  # avoid polling during the brief window
    run_task = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass

    # 9: scheduler.loop_error - drive by patching _poll_once to raise once.
    sched2 = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=_FakeAlerter(),  # type: ignore[arg-type]
    )

    raised = {"n": 0}

    async def _bad_poll():
        if raised["n"] == 0:
            raised["n"] += 1
            raise RuntimeError("boom")

    sched2._poll_once = _bad_poll  # type: ignore[assignment]
    cfg.scheduler.poll_interval_seconds = 0  # immediate cycle
    run2 = asyncio.create_task(sched2.run())
    await asyncio.sleep(0.05)
    run2.cancel()
    try:
        await run2
    except asyncio.CancelledError:
        pass

    seen = _seen_event_names(cfg.audit_dir)
    missing = set(EXPECTED_EVENTS) - seen
    assert not missing, f"missing audit events: {missing}; seen: {sorted(seen)}"
