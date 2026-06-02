"""Tests for the Telegram typing-action refresher (subtask 9 of the
Cadence-Visibility Hooks plan).

Four cases per the plan:

* (a) the refresher fires ``send_chat_action`` immediately on start;
* (b) the refresher loops on a ~4-second tick (we use a short
  ``telegram_typing_max_seconds`` plus a short-circuit on the second
  iteration to verify the loop body is exercised more than once);
* (c) ``clear_thinking_signal`` stops the refresher promptly (well
  under 1.5s);
* (d) hitting the ``max_seconds`` bound exits cleanly without spamming
  ``send_chat_action`` indefinitely.

The tests use a fake bot with an :class:`unittest.mock.AsyncMock` for
``send_chat_action`` so we never touch the network.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from anna.config import AnnaConfig
from anna.transports.base import InboundEvent, SignalHandle
from anna.transports.telegram import TelegramAdapter, _typing_refresher


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal stand-in for ``application.bot``.

    Only ``send_chat_action`` is required by ``_typing_refresher``;
    we wrap it in an ``AsyncMock`` so tests can assert call counts and
    arguments without instantiating python-telegram-bot.
    """

    def __init__(self) -> None:
        self.send_chat_action = AsyncMock()


class _FakeApplication:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot


def _make_adapter(*, bot: _FakeBot, max_seconds: int = 180) -> TelegramAdapter:
    cfg = AnnaConfig()
    cfg.runtime.visibility.telegram_typing_max_seconds = max_seconds
    adapter = TelegramAdapter(config=cfg)
    adapter._application = _FakeApplication(bot)  # type: ignore[attr-defined]
    return adapter


def _make_event(chat_id: int = 12345) -> InboundEvent:
    return InboundEvent(
        transport="telegram",
        conversation_key=f"telegram:dm:{chat_id}",
        sender_id=str(chat_id),
        sender_display="tester",
        text="hi",
        is_dm=True,
        is_thread=False,
        raw={
            "chat_id": chat_id,
            "chat_type": "private",
            "message_id": 1,
            "topic_id": None,
        },
    )


# ---------------------------------------------------------------------------
# (a) Refresher fires send_chat_action immediately on start
# ---------------------------------------------------------------------------


async def test_start_fires_send_chat_action_immediately() -> None:
    bot = _FakeBot()
    adapter = _make_adapter(bot=bot)
    event = _make_event(chat_id=42)

    handle = await adapter.start_thinking_signal(event)
    assert handle is not None
    assert isinstance(handle, SignalHandle)
    assert handle.telegram_task is not None
    assert handle.telegram_stopped is not None

    # Yield control so the task runs at least up to its first
    # send_chat_action call.
    for _ in range(10):
        if bot.send_chat_action.await_count >= 1:
            break
        await asyncio.sleep(0)

    assert bot.send_chat_action.await_count >= 1
    call = bot.send_chat_action.await_args
    assert call.kwargs.get("chat_id") == 42
    assert call.kwargs.get("action") == "typing"

    # Clean up so the task does not leak between tests.
    await adapter.clear_thinking_signal(handle)


# ---------------------------------------------------------------------------
# (b) Refresher loops on the 4-second tick
# ---------------------------------------------------------------------------


async def test_refresher_loops_on_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the loop body executes more than once and ``stop_event``
    drives the exit, not a hard-coded single iteration. We patch
    ``asyncio.wait_for`` inside the telegram module so the 4-second
    sleep is bypassed without affecting any other awaits.
    """

    bot = _FakeBot()
    stop_event = asyncio.Event()
    log = _SilentLogger()

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable: Any, timeout: float) -> Any:
        # Wait without honoring the long timeout: poll the stop event
        # quickly so the test runs in milliseconds, not seconds. Still
        # raise TimeoutError on the first three calls so the loop body
        # iterates, then let stop_event drive a clean exit on the
        # fourth.
        nonlocal fast_calls
        fast_calls += 1
        if fast_calls < 4:
            # Cancel the underlying awaitable (the stop_event.wait()
            # coroutine) and raise so the refresher continues looping.
            if hasattr(awaitable, "close"):
                try:
                    awaitable.close()
                except Exception:
                    pass
            raise asyncio.TimeoutError
        # On the fourth call, set the event and resolve.
        stop_event.set()
        return await real_wait_for(awaitable, timeout=0.5)

    fast_calls = 0
    monkeypatch.setattr("anna.transports.telegram.asyncio.wait_for", fast_wait_for)

    await _typing_refresher(bot, 99, stop_event, max_seconds=180, log=log)

    # The first send happens before the first wait_for; subsequent
    # iterations each send again. With four wait_for calls and an
    # immediate-send pattern we expect >= 4 sends.
    assert bot.send_chat_action.await_count >= 4
    for call in bot.send_chat_action.await_args_list:
        assert call.kwargs.get("chat_id") == 99
        assert call.kwargs.get("action") == "typing"


# ---------------------------------------------------------------------------
# (c) Clear stops the refresher promptly
# ---------------------------------------------------------------------------


async def test_clear_stops_refresher_promptly() -> None:
    bot = _FakeBot()
    adapter = _make_adapter(bot=bot)
    event = _make_event(chat_id=7)

    handle = await adapter.start_thinking_signal(event)
    assert handle is not None
    assert handle.telegram_task is not None

    # Give the task a couple of event-loop ticks to enter
    # ``stop_event.wait()``.
    for _ in range(5):
        if bot.send_chat_action.await_count >= 1:
            break
        await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await adapter.clear_thinking_signal(handle)
    elapsed = loop.time() - started

    assert elapsed < 1.5, f"clear took too long: {elapsed:.2f}s"
    assert handle.telegram_task.done()
    assert handle.telegram_stopped is not None
    assert handle.telegram_stopped.is_set()


# ---------------------------------------------------------------------------
# (d) Bound-hit exits cleanly without spamming
# ---------------------------------------------------------------------------


async def test_bound_hit_exits_without_spamming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the ``max_seconds`` bound is hit, the refresher must return
    cleanly. We simulate elapsed time by stubbing ``time.monotonic``
    inside the telegram module so the bound check trips on the second
    iteration.
    """

    bot = _FakeBot()
    log = _SilentLogger()
    stop_event = asyncio.Event()

    # First call returns t=0 (the start sample). Every subsequent call
    # returns a value past the 10-second bound so the bound-check
    # branch fires on the next loop iteration.
    samples = iter([0.0] + [100.0] * 50)

    def fake_monotonic() -> float:
        try:
            return next(samples)
        except StopIteration:
            return 100.0

    monkeypatch.setattr("anna.transports.telegram.time.monotonic", fake_monotonic)

    # Make wait_for resolve immediately with TimeoutError so the loop
    # advances to the next iteration without waiting 4 real seconds.
    async def instant_timeout(awaitable: Any, timeout: float) -> Any:
        if hasattr(awaitable, "close"):
            try:
                awaitable.close()
            except Exception:
                pass
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "anna.transports.telegram.asyncio.wait_for", instant_timeout
    )

    await _typing_refresher(bot, 1, stop_event, max_seconds=10, log=log)

    # One send before the bound check trips on the second iteration —
    # crucially, the loop does NOT continue spamming after the bound
    # hit; we expect <= 2 calls total even if monotonic is drained.
    assert bot.send_chat_action.await_count <= 2
    assert log.warnings, "expected a refresh_bound_hit warning"
    assert any(
        "refresh_bound_hit" in evt for evt, _ in log.warnings
    )


# ---------------------------------------------------------------------------
# Silent log shim
# ---------------------------------------------------------------------------


class _SilentLogger:
    """Tiny stand-in for ``structlog.BoundLogger`` — collects events
    instead of writing them so the test can assert on what was logged.
    """

    def __init__(self) -> None:
        self.debugs: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **kw: Any) -> None:
        self.debugs.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))

    def info(self, event: str, **kw: Any) -> None:  # pragma: no cover
        pass

    def error(self, event: str, **kw: Any) -> None:  # pragma: no cover
        pass
