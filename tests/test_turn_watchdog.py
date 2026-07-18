"""Interactive-turn watchdog: threshold logic, telemetry, breach recording.

The core :class:`TurnWatchdog` is a pure state machine driven by an
INJECTABLE clock, so every threshold/telemetry assertion here is
deterministic — no real wall-clock, no ``asyncio.sleep``. The four
scenarios the guard exists to distinguish are covered head-on: a turn that
never breaches, a soft-only breach, a hard (escalated) breach, and a turn
that backgrounded its work early and must therefore stay silent.

The worker-side tests then confirm the mechanical side effects wired around
the state machine: the hard-breach admin alert fires exactly once, per-turn
telemetry lands in the audit log, and the forcing reminder stashed by a
breach is prepended to ANNA's next turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig, TurnWatchdogConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.turn_watchdog import (
    TurnWatchdog,
    WatchdogAction,
    hard_reminder,
    is_background_spawn,
    soft_reminder,
)
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import ConversationWorker, _FlushBuffer
from anna.transports.base import InboundEvent, OutboundMessage

# Captured BEFORE any test monkeypatches ``asyncio.sleep`` so the loop-driving
# fakes below can yield to the event loop without recursing into their own
# patched sleep.
_REAL_SLEEP = asyncio.sleep


# ---------------------------------------------------------------------------
# Injectable clock
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock. ``advance`` moves it forward."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _watchdog(clock: FakeClock, *, soft: float = 60.0, hard: float = 120.0) -> TurnWatchdog:
    return TurnWatchdog(
        soft_seconds=soft,
        hard_seconds=hard,
        clock=clock,
        start_time=clock(),
    )


# ---------------------------------------------------------------------------
# State machine — the four required scenarios
# ---------------------------------------------------------------------------


def test_no_breach_turn_stays_silent() -> None:
    """A turn that never crosses the soft threshold never fires and records
    no breach."""
    clock = FakeClock()
    wd = _watchdog(clock)

    # A couple of quick tool calls, well under the soft line.
    wd.note_tool_call("Read")
    clock.advance(10.0)
    assert wd.poll() is WatchdogAction.NONE
    clock.advance(49.0)  # 59s total — still under 60s soft
    assert wd.poll() is WatchdogAction.NONE

    tel = wd.telemetry()
    assert tel["soft_breached"] is False
    assert tel["hard_breached"] is False
    assert tel["backgrounded"] is False
    assert tel["tool_call_count"] == 1


def test_soft_only_breach_fires_once() -> None:
    """Crossing soft (but not hard) fires SOFT exactly once, then goes quiet;
    only the soft flag is set."""
    clock = FakeClock()
    wd = _watchdog(clock)

    clock.advance(60.0)  # exactly the soft threshold
    assert wd.poll() is WatchdogAction.SOFT
    # Idempotent: a second poll still under hard does not re-fire.
    clock.advance(30.0)  # 90s total, under 120s hard
    assert wd.poll() is WatchdogAction.NONE

    tel = wd.telemetry()
    assert tel["soft_breached"] is True
    assert tel["hard_breached"] is False


def test_hard_breach_after_soft() -> None:
    """Soft fires, then hard fires once when the hard line is crossed; a
    further poll is silent and both flags are set."""
    clock = FakeClock()
    wd = _watchdog(clock)

    clock.advance(60.0)
    assert wd.poll() is WatchdogAction.SOFT
    clock.advance(60.0)  # 120s total — the hard threshold
    assert wd.poll() is WatchdogAction.HARD
    clock.advance(60.0)
    assert wd.poll() is WatchdogAction.NONE

    tel = wd.telemetry()
    assert tel["soft_breached"] is True
    assert tel["hard_breached"] is True


def test_backgrounded_early_never_fires() -> None:
    """A turn that delegates before the soft line stays silent through hard —
    the guard catches UN-yielded turns, not ones that handed work off."""
    clock = FakeClock()
    wd = _watchdog(clock)

    clock.advance(5.0)
    wd.note_tool_call("mcp__anna_delegate__delegate")  # backgrounded
    assert wd.backgrounded is True

    # Well past BOTH thresholds — still silent because work is backgrounded.
    clock.advance(200.0)
    assert wd.poll() is WatchdogAction.NONE
    assert wd.poll() is WatchdogAction.NONE

    tel = wd.telemetry()
    assert tel["backgrounded"] is True
    assert tel["soft_breached"] is False
    assert tel["hard_breached"] is False


# ---------------------------------------------------------------------------
# State machine — edges
# ---------------------------------------------------------------------------


def test_hard_marks_soft_when_both_crossed_in_one_poll() -> None:
    """A slow/stalled tick that lands past hard before soft ever polled fires
    HARD and records the soft breach too (never a hard without its soft)."""
    clock = FakeClock()
    wd = _watchdog(clock)

    clock.advance(130.0)  # first poll of the turn, already past hard
    assert wd.poll() is WatchdogAction.HARD
    tel = wd.telemetry()
    assert tel["soft_breached"] is True
    assert tel["hard_breached"] is True


def test_background_after_soft_suppresses_hard() -> None:
    """Backgrounding AFTER a soft breach keeps the recorded soft breach but
    suppresses any hard escalation (turn is winding down)."""
    clock = FakeClock()
    wd = _watchdog(clock)

    clock.advance(60.0)
    assert wd.poll() is WatchdogAction.SOFT
    wd.note_tool_call("Bash", {"run_in_background": True})  # backgrounded now
    clock.advance(120.0)  # would be well past hard
    assert wd.poll() is WatchdogAction.NONE

    tel = wd.telemetry()
    assert tel["soft_breached"] is True
    assert tel["hard_breached"] is False
    assert tel["backgrounded"] is True


def test_mark_ended_freezes_duration() -> None:
    """Telemetry duration is frozen at ``mark_ended``; later clock motion does
    not change it, and a second mark_ended is a no-op."""
    clock = FakeClock()
    wd = _watchdog(clock)

    clock.advance(42.0)
    wd.mark_ended()
    clock.advance(1000.0)
    wd.mark_ended()  # idempotent — must not rewind to 1042
    assert wd.telemetry()["duration_seconds"] == pytest.approx(42.0)


def test_tool_count_accumulates() -> None:
    clock = FakeClock()
    wd = _watchdog(clock)
    for name in ("Read", "Grep", "Edit"):
        wd.note_tool_call(name)
    assert wd.telemetry()["tool_call_count"] == 3
    assert wd.backgrounded is False


# ---------------------------------------------------------------------------
# is_background_spawn classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tool_input", "expected"),
    [
        ("mcp__anna_delegate__delegate", None, True),
        ("mcp__anna_delegate__delegate", {"agent": "x"}, True),
        ("Bash", {"command": "sleep 1000", "run_in_background": True}, True),
        ("Bash", {"command": "ls", "run_in_background": False}, False),
        ("Bash", {"command": "ls"}, False),
        ("Read", {"file_path": "/x"}, False),
        ("mcp__anna_web__web_fetch", {"url": "http://x"}, False),
    ],
)
def test_is_background_spawn(name: str, tool_input: Any, expected: bool) -> None:
    assert is_background_spawn(name, tool_input) is expected


# ---------------------------------------------------------------------------
# Reminder text
# ---------------------------------------------------------------------------


def test_reminders_carry_thresholds_and_are_system_reminders() -> None:
    soft = soft_reminder(60)
    hard = hard_reminder(120)
    assert soft.startswith("<system-reminder>")
    assert soft.rstrip().endswith("</system-reminder>")
    assert "60s" in soft
    assert "120s" in hard
    # The hard reminder is the stronger of the two.
    assert "STOP" in hard


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_defaults_are_on_and_conservative() -> None:
    cfg = TurnWatchdogConfig()
    assert cfg.enabled is True
    assert cfg.soft_threshold_seconds == 60
    assert cfg.hard_threshold_seconds == 120


def test_config_rejects_hard_not_above_soft() -> None:
    with pytest.raises(ValueError):
        TurnWatchdogConfig(soft_threshold_seconds=120, hard_threshold_seconds=120)
    with pytest.raises(ValueError):
        TurnWatchdogConfig(soft_threshold_seconds=120, hard_threshold_seconds=60)


def test_config_rejects_non_positive_thresholds() -> None:
    with pytest.raises(ValueError):
        TurnWatchdogConfig(soft_threshold_seconds=0)
    with pytest.raises(ValueError):
        TurnWatchdogConfig(hard_threshold_seconds=-1)


# ---------------------------------------------------------------------------
# Worker-side: breach recording, telemetry, reminder prepend
# ---------------------------------------------------------------------------


CONV_KEY = "slack:channel:dm:U123"


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    name: str = "fake_tool"
    input: dict[str, Any] | None = None


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


class _FakeBlocksClient:
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


class _RecordingAlerter:
    """Minimal AdminAlerter stand-in capturing warn() calls."""

    def __init__(self) -> None:
        self.warns: list[str] = []

    async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warns.append(message)
        return True


@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
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
    alerter: _RecordingAlerter | None = None,
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
        alerter=alerter,
    )


def _make_event() -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U123",
        sender_display="Seth",
        text="Do the thing.",
        is_dm=True,
        is_thread=False,
        completion_future=None,
    )


@pytest.mark.asyncio
async def test_watchdog_active_only_for_buffered_interactive(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path, [])
    # Buffered interactive turn: active.
    assert worker._turn_watchdog_active(_make_event()) is True
    # Scheduler-driven turn (completion_future set): exempt.
    ev = _make_event()
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    object.__setattr__(ev, "completion_future", fut)
    assert worker._turn_watchdog_active(ev) is False
    fut.cancel()
    # Disabled by config: inactive even for a buffered interactive turn.
    worker._turn_watchdog_cfg = TurnWatchdogConfig(enabled=False)
    assert worker._turn_watchdog_active(_make_event()) is False


@pytest.mark.asyncio
async def test_hard_breach_fires_single_admin_alert_and_audits(
    tmp_path: Path, monkeypatch
) -> None:
    """``_record_watchdog_hard_breach`` writes a breach audit row and fires
    exactly one admin alert summarizing the turn."""
    audited: list[tuple[str, dict[str, Any]]] = []

    def _capture(name: str, **fields: Any) -> None:
        audited.append((name, fields))

    monkeypatch.setattr("anna.runtime.worker.audit_event", _capture)

    alerter = _RecordingAlerter()
    worker = _make_worker(tmp_path, [], alerter=alerter)

    clock = FakeClock()
    wd = _watchdog(clock)
    wd.note_tool_call("Read")
    clock.advance(130.0)
    assert wd.poll() is WatchdogAction.HARD

    await worker._record_watchdog_hard_breach(_make_event(), wd)

    assert len(alerter.warns) == 1
    assert "held the operator's channel" in alerter.warns[0]
    breach_rows = [f for (n, f) in audited if n == "audit.turn.watchdog_breach"]
    assert len(breach_rows) == 1
    assert breach_rows[0]["tool_call_count"] == 1
    assert breach_rows[0]["backgrounded"] is False


@pytest.mark.asyncio
async def test_hard_breach_no_alert_without_alerter(
    tmp_path: Path, monkeypatch
) -> None:
    """No wired alerter → still audits, just no alert (and no crash)."""
    monkeypatch.setattr("anna.runtime.worker.audit_event", lambda *a, **k: None)
    worker = _make_worker(tmp_path, [], alerter=None)

    clock = FakeClock()
    wd = _watchdog(clock)
    clock.advance(130.0)
    wd.poll()
    # Must not raise.
    await worker._record_watchdog_hard_breach(_make_event(), wd)


@pytest.mark.asyncio
async def test_turn_telemetry_written_at_turn_end(tmp_path: Path, monkeypatch) -> None:
    """A completed interactive turn emits one ``audit.turn.telemetry`` row."""
    audited: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "anna.runtime.worker.audit_event",
        lambda name, **fields: audited.append((name, fields)),
    )

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(blocks=[_FakeTextBlock(text="quick reply")])

    await worker._handle(_make_event())

    tel_rows = [f for (n, f) in audited if n == "audit.turn.telemetry"]
    assert len(tel_rows) == 1
    assert tel_rows[0]["soft_breached"] is False
    assert tel_rows[0]["hard_breached"] is False
    assert tel_rows[0]["tool_call_count"] == 0
    # A fast, non-breaching turn logs at INFO.
    assert tel_rows[0]["level"] == "INFO"


@pytest.mark.asyncio
async def test_pending_reminder_prepended_to_next_turn(tmp_path: Path) -> None:
    """A reminder stashed by a prior breach is prepended to the next turn's
    query text and then cleared (fires exactly once)."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(blocks=[_FakeTextBlock(text="ok")])
    worker._pending_watchdog_reminder = soft_reminder(60)

    await worker._handle(_make_event())

    client: _FakeBlocksClient = worker._client  # type: ignore[assignment]
    assert client.queries, "expected a query to have been issued"
    assert client.queries[0].startswith("<system-reminder>")
    assert "Do the thing." in client.queries[0]
    # Consumed — a subsequent turn does not re-prepend it.
    assert worker._pending_watchdog_reminder is None


@pytest.mark.asyncio
async def test_pending_reminder_not_consumed_by_scheduled_turn(tmp_path: Path) -> None:
    """A stashed forcing reminder is INHERITED only by an INTERACTIVE turn: a
    scheduled/heartbeat turn (``completion_future`` set) sharing the same worker
    must not prepend it, and must leave it stashed for the next interactive
    turn (SAFETY: it says 'you are holding the operator's channel' — false on a
    scheduled turn)."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeBlocksClient(blocks=[_FakeTextBlock(text="ok")])
    reminder = hard_reminder(120)
    worker._pending_watchdog_reminder = reminder

    # Scheduled turn: completion_future set.
    ev = _make_event()
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    object.__setattr__(ev, "completion_future", fut)

    await worker._handle(ev)

    client: _FakeBlocksClient = worker._client  # type: ignore[assignment]
    assert client.queries, "expected a query to have been issued"
    # The scheduled turn's query does NOT carry the forcing reminder...
    assert not client.queries[0].startswith("<system-reminder>")
    # ...and the reminder is preserved for the next interactive turn.
    assert worker._pending_watchdog_reminder == reminder


# ---------------------------------------------------------------------------
# Worker-side: PRODUCE path — the async ticker (_turn_watchdog_loop) and the
# flush (_flush_buffer_now). These drive the loop to a real soft/hard breach
# with an injected clock and a fake ``asyncio.sleep`` that advances that clock.
# ---------------------------------------------------------------------------


class _SleepDriver:
    """Fake ``asyncio.sleep`` that advances an injected ``FakeClock`` per tick.

    Substituted for ``asyncio.sleep`` while a ``_turn_watchdog_loop`` runs so
    each ~1s tick deterministically moves the watchdog's clock forward without
    any real wall-clock wait. After ``max_ticks`` advances it raises
    ``CancelledError`` — the same signal the turn's ``finally`` uses to stop the
    ticker — so the loop terminates cleanly at a known clock reading.
    """

    def __init__(self, clock: FakeClock, *, step: float, max_ticks: int) -> None:
        self._clock = clock
        self._step = step
        self._max_ticks = max_ticks
        self.ticks = 0

    async def __call__(self, _delay: float) -> None:
        if self.ticks >= self._max_ticks:
            raise asyncio.CancelledError
        self.ticks += 1
        self._clock.advance(self._step)
        # Yield to the loop WITHOUT recursing into the patched sleep.
        await _REAL_SLEEP(0)


async def _run_watchdog_loop(
    worker: ConversationWorker,
    event: InboundEvent,
    buffer: _FlushBuffer,
    watchdog: TurnWatchdog,
) -> None:
    """Drive the ticker to completion, swallowing the driver's cancel."""
    try:
        await worker._turn_watchdog_loop(event, buffer, watchdog)
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_watchdog_loop_soft_breach_flushes_and_stashes_reminder(
    tmp_path: Path, monkeypatch
) -> None:
    """Driving the ticker to a soft (only) breach flushes pending narration to
    the operator and stashes the soft forcing reminder for the next turn."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    clock = FakeClock()
    wd = _watchdog(clock)  # soft 60, hard 120
    wd.note_tool_call("Read")
    buffer = _FlushBuffer(last_flush=0.0)
    buffer.pending.append("Still working on it...")

    # One 60s tick lands exactly on soft, below hard; the loop then cancels.
    driver = _SleepDriver(clock, step=60.0, max_ticks=1)
    monkeypatch.setattr("anna.runtime.worker.asyncio.sleep", driver)

    await _run_watchdog_loop(worker, _make_event(), buffer, wd)

    # PRODUCE side: the buffered narration was flushed to the operator.
    assert [m.text for m in sent] == ["Still working on it..."]
    assert buffer.pending == []
    # Soft reminder stashed for ANNA's next turn (fires there exactly once).
    assert worker._pending_watchdog_reminder is not None
    assert worker._pending_watchdog_reminder.startswith("<system-reminder>")
    assert "60s" in worker._pending_watchdog_reminder
    assert wd.soft_breached is True
    assert wd.hard_breached is False


@pytest.mark.asyncio
async def test_watchdog_loop_hard_breach_records_and_alerts_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Driving the ticker past the hard line flushes narration, stashes the hard
    reminder, records exactly one breach audit row, and fires exactly one admin
    alert."""
    audited: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "anna.runtime.worker.audit_event",
        lambda name, **fields: audited.append((name, fields)),
    )
    alerter = _RecordingAlerter()
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, alerter=alerter)

    clock = FakeClock()
    wd = _watchdog(clock)  # soft 60, hard 120
    wd.note_tool_call("Read")
    wd.note_tool_call("Edit")
    buffer = _FlushBuffer(last_flush=0.0)
    buffer.pending.append("Grinding away inline...")

    # A single 130s tick lands past hard (poll fires HARD, marking soft too);
    # the loop then cancels, so poll runs exactly once and the alert cannot
    # double-fire.
    driver = _SleepDriver(clock, step=130.0, max_ticks=1)
    monkeypatch.setattr("anna.runtime.worker.asyncio.sleep", driver)

    await _run_watchdog_loop(worker, _make_event(), buffer, wd)

    # Narration flushed.
    assert [m.text for m in sent] == ["Grinding away inline..."]
    assert buffer.pending == []
    # Hard reminder stashed.
    assert worker._pending_watchdog_reminder is not None
    assert "STOP" in worker._pending_watchdog_reminder
    assert "120s" in worker._pending_watchdog_reminder
    # Breach recorded exactly once.
    breach_rows = [f for (n, f) in audited if n == "audit.turn.watchdog_breach"]
    assert len(breach_rows) == 1
    assert breach_rows[0]["tool_call_count"] == 2
    assert breach_rows[0]["backgrounded"] is False
    # Admin alert fired exactly once.
    assert len(alerter.warns) == 1
    assert "held the operator's channel" in alerter.warns[0]


@pytest.mark.asyncio
async def test_hard_breach_audit_failure_still_alerts_and_isolates(
    tmp_path: Path, monkeypatch
) -> None:
    """A raising ``audit_event`` (disk/IO error) must NOT crash the breach
    recorder nor suppress the admin alert — "Never raises" holds and the alert
    still fires (SHOULD-FIX: exception-isolate the breach write)."""
    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("audit disk full")

    monkeypatch.setattr("anna.runtime.worker.audit_event", _boom)
    alerter = _RecordingAlerter()
    worker = _make_worker(tmp_path, [], alerter=alerter)

    clock = FakeClock()
    wd = _watchdog(clock)
    clock.advance(130.0)
    assert wd.poll() is WatchdogAction.HARD

    # Must not raise despite the audit write blowing up.
    await worker._record_watchdog_hard_breach(_make_event(), wd)

    # The admin alert still fired even though the breach row could not be written.
    assert len(alerter.warns) == 1


@pytest.mark.asyncio
async def test_flush_buffer_now_sends_and_restamps(tmp_path: Path) -> None:
    """``_flush_buffer_now`` sends the pending narration, clears the buffer, and
    restamps ``last_flush``; a second call over an empty buffer is a no-op."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    buffer = _FlushBuffer(last_flush=0.0)
    buffer.pending.extend(["line one", "line two"])

    await worker._flush_buffer_now(_make_event(), buffer)

    assert len(sent) == 1
    assert sent[0].text == "line one\nline two"
    assert buffer.pending == []
    assert buffer.last_flush > 0.0

    # Empty buffer: no second send.
    await worker._flush_buffer_now(_make_event(), buffer)
    assert len(sent) == 1
