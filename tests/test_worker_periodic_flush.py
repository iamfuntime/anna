"""Validate the worker's time-based "drip" flush (Inbox/2026-06-04 plan).

During a long single-turn run (sub-agent chains, multi-tool sequences) the
worker flushes its pending narration buffer to buffered transports on a
wall-clock cadence via a separate background timer task that guards the
shared buffer with an ``asyncio.Lock``. This is layered on top of the
existing tool-use-boundary flush (regression-covered by
``tests/test_worker_flush.py``, which must keep passing unchanged).

The consumer loop stays a plain ``async for`` — the SDK generator is NEVER
pumped manually (a spike proved ``wait_for(__anext__())`` finalizes the
SDK's native async-generator stack and drops the rest of the turn).

These tests drive the timer with a controllable clock and a patched
``asyncio.sleep`` (no real waits): the fake client yields blocks across
real ``await`` points so the timer task can interleave, while the clock is
advanced explicitly to make the ``>= interval`` comparison deterministic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import (
    NULL_VISIBILITY,
    VisibilityCallbacks,
    _noop_clear,
    _noop_start,
)
from anna.runtime.worker import ConversationWorker
from anna.transports.base import InboundEvent, OutboundMessage


CONV_KEY = "slack:channel:dm:U123"


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


class _FakeClock:
    """A controllable monotonic clock the worker's ``loop.time()`` reads."""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class _GappedClient:
    """Fake SDK that yields blocks, pausing on a per-step gate.

    Each yielded item is either a block or a ``("gate", key)`` sentinel.
    On a sentinel the generator awaits ``gates[key]`` (an ``asyncio.Event``)
    so the test can hold the turn open while the background timer fires,
    then advance the clock and release the gate. Blocks are wrapped one per
    ``AssistantMessage`` so each ``async for`` iteration is a real await
    point the timer task can interleave with.
    """

    def __init__(self, steps: list[Any], gates: dict[str, asyncio.Event]) -> None:
        self._steps = steps
        self._gates = gates
        self.queries: list[str] = []

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        for step in self._steps:
            if isinstance(step, tuple) and step and step[0] == "gate":
                await self._gates[step[1]].wait()
                continue
            yield _FakeAssistantMessage(content=[step])
        yield _FakeResultMessage()

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


class _ImmediateClient:
    """Fake SDK that yields every block in one message, no gating."""

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
    """Swap the SDK block/message classes for our fakes so the worker's
    ``isinstance`` checks match the scripted content."""
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    yield


@pytest.fixture
async def clock(monkeypatch) -> _FakeClock:
    """Install a controllable clock as the running loop's ``time()`` and make
    the worker's ``asyncio.sleep`` instantaneous (the test owns clock
    advancement so it decides when a drip is "due"). Async so it binds to the
    same running loop the test body uses."""
    import anna.runtime.worker as worker_mod

    fake = _FakeClock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", fake, raising=False)

    real_sleep = asyncio.sleep

    async def _fast_sleep(delay: float, *a, **k):
        # Yield control so the consumer loop / gates can make progress, but
        # do NOT advance the clock here — the test owns clock advancement so
        # it can decide whether a drip is "due".
        await real_sleep(0)

    monkeypatch.setattr(worker_mod.asyncio, "sleep", _fast_sleep, raising=False)
    return fake


async def _spin(n: int = 20) -> None:
    """Yield control ``n`` times so background tasks can interleave."""
    for _ in range(n):
        await asyncio.sleep(0)


async def _wait_for(pred, *, tries: int = 200) -> None:
    """Spin until ``pred()`` is true (bounded) so a drip can be observed
    deterministically without a real wall-clock wait."""
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)


def _make_worker(
    tmp_path: Path,
    send_target: list[OutboundMessage],
    *,
    transport: str = "slack",
    flush_seconds: int = 30,
    voice_only: bool = False,
    voice_enabled: bool = True,
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    cfg.runtime.visibility.periodic_flush_seconds = flush_seconds
    cfg.voice.outbound.enabled = voice_enabled
    cfg.voice.outbound.voice_only = voice_only
    supervisor = Supervisor(config=cfg)

    async def _send(msg: OutboundMessage) -> None:
        send_target.append(msg)

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport=transport,
        config=cfg,
        supervisor=supervisor,
        send=_send,
        visibility=NULL_VISIBILITY,
    )


def _make_event(*, future: asyncio.Future[str] | None = None) -> InboundEvent:
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


# ---------------------------------------------------------------------------
# Timer fires when due
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timer_fires_when_interval_elapsed(tmp_path: Path, clock: _FakeClock) -> None:
    """A silent gap longer than the interval drips the buffered narration
    as its own message, and the trailing text lands at turn end."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="partial narration"),
            ("gate", "hold"),
            _FakeTextBlock(text="final answer"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        # Let the consumer append "partial narration" and block on the gate.
        await _spin()
        # Advance past the interval and wait for the timer to drip.
        clock.advance(31)
        await _wait_for(lambda: len(sent) >= 1)
        # Release the rest of the turn.
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert [m.text for m in sent] == ["partial narration", "final answer"]


@pytest.mark.asyncio
async def test_lint_sees_full_text_across_drip(tmp_path: Path, clock: _FakeClock) -> None:
    """The cadence linter receives the WHOLE reply regardless of drips —
    ``reply_chunks`` is never cleared by a flush, only ``pending`` is."""
    linted: list[str] = []

    class _CapturingLint:
        def lint(self, text: str, *, transport: str, conv_key: str) -> None:
            linted.append(text)

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)
    # Swap in a lint-capturing visibility (NULL_VISIBILITY.lint is None).
    # Build a fresh frozen bundle rather than mutating the shared singleton.
    worker._visibility = VisibilityCallbacks(
        start=_noop_start,
        clear=_noop_clear,
        lint=_CapturingLint(),  # type: ignore[arg-type]
        cadence_reminder_loader=None,
    )

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="alpha"),
            ("gate", "hold"),
            _FakeTextBlock(text="omega"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(31)
        await _wait_for(lambda: len(sent) >= 1)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert linted == ["alpha\nomega"]
    assert [m.text for m in sent] == ["alpha", "omega"]


# ---------------------------------------------------------------------------
# Timer skips when it must
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timer_skips_under_interval(tmp_path: Path, clock: _FakeClock) -> None:
    """A gap SHORTER than the interval produces no drip; the whole reply
    lands as one consolidated message at turn end."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="quick"),
            ("gate", "hold"),
            _FakeTextBlock(text="reply"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        # Only 5s elapse — well under the 30s interval; give the timer ample
        # scheduling room to confirm it does NOT drip.
        clock.advance(5)
        await _spin(60)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert [m.text for m in sent] == ["quick\nreply"]


@pytest.mark.asyncio
async def test_timer_skips_empty_buffer(tmp_path: Path, clock: _FakeClock) -> None:
    """An empty pending buffer never drips even after the interval — the
    tool-only turn falls through to the ``(no response)`` fallback."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeToolUseBlock(),
            ("gate", "hold"),
            _FakeToolUseBlock(),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(60)
        await _spin(60)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert [m.text for m in sent] == ["(no response)"]


@pytest.mark.asyncio
async def test_scheduler_path_never_drips(tmp_path: Path, clock: _FakeClock) -> None:
    """When ``completion_future`` is set the timer is never started: no
    drips, and the future resolves with the full consolidated reply."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="a"),
            ("gate", "hold"),
            _FakeTextBlock(text="b"),
        ],
        gates=gates,
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    async def _driver() -> None:
        await _spin()
        clock.advance(120)
        await _spin(60)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event(future=future)), _driver())

    assert sent == []
    assert future.done()
    assert future.result() == "a\nb"


@pytest.mark.asyncio
async def test_voice_only_never_drips(tmp_path: Path, clock: _FakeClock) -> None:
    """Voice-only outbound stays consolidated: the timer is not started so
    even a long gap produces a single end-of-turn message."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(
        tmp_path, sent, flush_seconds=30, voice_only=True, voice_enabled=True
    )

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="spoken"),
            ("gate", "hold"),
            _FakeTextBlock(text="reply"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(120)
        await _spin(60)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert [m.text for m in sent] == ["spoken\nreply"]


@pytest.mark.asyncio
async def test_disabled_interval_never_drips(tmp_path: Path, clock: _FakeClock) -> None:
    """``periodic_flush_seconds == 0`` reproduces today's behavior: no timer,
    so a long gap still consolidates to one message at turn end."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=0)

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="one"),
            ("gate", "hold"),
            _FakeTextBlock(text="two"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(120)
        await _spin(60)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert [m.text for m in sent] == ["one\ntwo"]


# ---------------------------------------------------------------------------
# No double-send and interaction with the tool-use flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_double_send_drip_then_tool_use(tmp_path: Path, clock: _FakeClock) -> None:
    """A drip empties ``pending``; a following tool-use boundary must not
    resend the already-dripped text. The partition is exact."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="dripped"),
            ("gate", "hold"),
            _FakeToolUseBlock(),
            _FakeTextBlock(text="after tool"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(31)
        await _wait_for(lambda: len(sent) >= 1)
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    # "dripped" went out via the timer; the tool-use boundary has an empty
    # buffer (nothing to flush); "after tool" lands at turn end.
    assert [m.text for m in sent] == ["dripped", "after tool"]


@pytest.mark.asyncio
async def test_final_flush_still_lands_after_drip(tmp_path: Path, clock: _FakeClock) -> None:
    """A drip mid-turn does not swallow the residual buffer: text appended
    AFTER the drip is sent at turn end."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    gates = {"g1": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="first"),
            ("gate", "g1"),
            _FakeTextBlock(text="second"),
            _FakeTextBlock(text="third"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(31)
        await _wait_for(lambda: len(sent) >= 1)
        gates["g1"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    assert [m.text for m in sent] == ["first", "second\nthird"]


@pytest.mark.asyncio
async def test_no_gap_consolidates_like_today(tmp_path: Path, clock: _FakeClock) -> None:
    """With the timer active but no silent gap, a single-message reply still
    sends exactly once (regression parity with the no-tool-use case)."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)
    worker._client = _ImmediateClient(blocks=[_FakeTextBlock(text="hello world")])

    await worker._handle(_make_event())

    assert [m.text for m in sent] == ["hello world"]


# ---------------------------------------------------------------------------
# Loss-safety: a drip cancelled mid-send must not drop its text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drip_cancelled_mid_send_not_lost(tmp_path: Path, clock: _FakeClock) -> None:
    """FIX 1: if the turn ends (ResultMessage) while a drip is suspended
    inside ``_send``, the teardown cancels the timer — the in-flight text
    must NOT be lost. Because the drip sends BEFORE clearing ``pending``,
    the cancelled send leaves the text in the buffer and the final turn-end
    send re-emits it."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    # The first (drip) send blocks forever on an event we never set, so the
    # drip is suspended inside ``_send`` when teardown cancels the timer.
    # The final turn-end send goes through normally.
    block_first = asyncio.Event()
    first_send_started = asyncio.Event()
    call_count = {"n": 0}

    async def _blocking_send(msg: OutboundMessage) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            first_send_started.set()
            await block_first.wait()  # never released — cancel lands here
        sent.append(msg)

    worker._send = _blocking_send  # type: ignore[method-assign]

    gates = {"hold": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="dripme"),
            ("gate", "hold"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        clock.advance(31)
        # Wait until the drip is actually suspended inside ``_send``.
        await _wait_for(first_send_started.is_set)
        # End the turn: the consumer reaches ResultMessage and the finally
        # cancels the (suspended) drip task while it sits inside _send.
        gates["hold"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    # The drip's send was cancelled mid-flight, so "dripme" was never
    # delivered by the timer — but it must survive and be re-emitted by the
    # final turn-end send. Exactly one message, carrying the text.
    assert [m.text for m in sent] == ["dripme"]


# ---------------------------------------------------------------------------
# A failed drip send is logged and the timer keeps running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drip_send_failure_logged_timer_survives(
    tmp_path: Path, clock: _FakeClock
) -> None:
    """FIX 2: a non-Cancelled exception from a drip ``_send`` is logged and
    the timer keeps ticking — a later drip still fires (the buffer is left
    intact so the text retries)."""
    sent: list[OutboundMessage] = []
    warnings: list[tuple[str, dict[str, Any]]] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    # Capture structured warnings so we can assert the failure is observable.
    def _capture_warning(event: str, **kw: Any) -> None:
        warnings.append((event, kw))

    worker._log.warning = _capture_warning  # type: ignore[method-assign]

    fail_next = {"on": True}

    async def _flaky_send(msg: OutboundMessage) -> None:
        if fail_next["on"]:
            fail_next["on"] = False
            raise RuntimeError("transport boom")
        sent.append(msg)

    worker._send = _flaky_send  # type: ignore[method-assign]

    gates = {"g1": asyncio.Event(), "g2": asyncio.Event()}
    worker._client = _GappedClient(
        steps=[
            _FakeTextBlock(text="retryme"),
            ("gate", "g1"),
            ("gate", "g2"),
        ],
        gates=gates,
    )

    async def _driver() -> None:
        await _spin()
        # First drip is due: it raises, is logged, buffer untouched.
        clock.advance(31)
        await _wait_for(lambda: any(w[0] == "worker.periodic_flush.send_failed" for w in warnings))
        gates["g1"].set()
        # Advance again so a SECOND drip fires — proves the timer survived.
        clock.advance(31)
        await _wait_for(lambda: len(sent) >= 1)
        gates["g2"].set()

    await asyncio.gather(worker._handle(_make_event()), _driver())

    # The failure was logged once with the conv/transport context.
    failed = [w for w in warnings if w[0] == "worker.periodic_flush.send_failed"]
    assert len(failed) == 1
    assert failed[0][1]["conv_key"] == CONV_KEY
    assert failed[0][1]["transport"] == "slack"
    # The later drip delivered the (retained) text — timer kept running.
    assert [m.text for m in sent] == ["retryme"]


# ---------------------------------------------------------------------------
# Direct ``_periodic_flush_loop`` coverage of the 1s poll-floor change:
# the loop must wake frequently (poll == min(interval, 1.0)) yet still gate
# the actual SEND on the full ``interval`` via the elapsed-since-last_flush
# guard. These drive the loop task directly with a controllable clock.
# ---------------------------------------------------------------------------


def _make_buffer(clock: _FakeClock, *, pending: list[str] | None = None):
    """Build a ``_FlushBuffer`` seeded with the current clock as ``last_flush``
    (mirroring the turn-start seeding in ``_handle``)."""
    from anna.runtime.worker import _FlushBuffer

    return _FlushBuffer(last_flush=clock(), pending=list(pending or []))


@pytest.mark.asyncio
async def test_loop_flushes_when_interval_elapsed_send_before_clear(
    tmp_path: Path, clock: _FakeClock
) -> None:
    """(a) With pending text and >= interval elapsed, the loop flushes:
    it sends BEFORE clearing ``pending`` (the lock is held across the send,
    pending still carries the text at send time) and stamps ``last_flush``."""
    sent: list[OutboundMessage] = []
    observed_pending_at_send: list[list[str]] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    async def _recording_send(msg: OutboundMessage) -> None:
        # Capture pending AS IT IS at send time to prove send-before-clear.
        observed_pending_at_send.append(list(buffer.pending))
        sent.append(msg)

    worker._send = _recording_send  # type: ignore[method-assign]

    buffer = _make_buffer(clock, pending=["partial narration"])
    event = _make_event()

    task = asyncio.create_task(worker._periodic_flush_loop(event, buffer))
    try:
        # The interval has elapsed since last_flush → the loop must drip.
        clock.advance(31)
        await _wait_for(lambda: len(sent) >= 1)
    finally:
        # Cancel-and-await teardown (mirrors the turn's finally).
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert [m.text for m in sent] == ["partial narration"]
    # Send happened while the text was still in ``pending`` (send-before-clear).
    assert observed_pending_at_send == [["partial narration"]]
    # The buffer was cleared and ``last_flush`` re-stamped to "now".
    assert buffer.pending == []
    assert buffer.last_flush == clock()


@pytest.mark.asyncio
async def test_loop_does_not_flush_before_interval_despite_fast_poll(
    tmp_path: Path, clock: _FakeClock
) -> None:
    """(b) The 1s poll floor wakes the loop many times, but with < interval
    elapsed since ``last_flush`` it must NOT send — no premature / double
    drip. Pending and ``last_flush`` are left untouched."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent, flush_seconds=30)

    buffer = _make_buffer(clock, pending=["not yet"])
    seeded_last_flush = buffer.last_flush
    event = _make_event()

    task = asyncio.create_task(worker._periodic_flush_loop(event, buffer))
    try:
        # Only 5s elapse — well under the 30s interval. Spin generously so the
        # fast (1s-floor) poll loop iterates many times with the clock fake's
        # instant sleep, confirming it never sends.
        clock.advance(5)
        await _spin(60)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert sent == []
    # Nothing was consumed or re-stamped.
    assert buffer.pending == ["not yet"]
    assert buffer.last_flush == seeded_last_flush
