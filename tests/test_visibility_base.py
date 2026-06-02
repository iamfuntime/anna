"""Default no-op behavior of ChannelAdapter visibility hooks.

Subtask 2 of the Cadence-Visibility Hooks plan adds
``start_thinking_signal`` and ``clear_thinking_signal`` as
default-noop methods on the base ``ChannelAdapter``. Subclasses that
do not override either method should remain instantiable and the
methods should be awaitable, return ``None``, and not raise.
"""

from __future__ import annotations

from typing import Any

from anna.runtime.visibility import NULL_VISIBILITY
from anna.transports.base import (
    ChannelAdapter,
    InboundEvent,
    OutboundMessage,
    SignalHandle,
)


class _MinimalAdapter(ChannelAdapter):
    """Smallest possible ChannelAdapter that does NOT override the
    new visibility methods. Mirrors the test-stub pattern used in
    tests/test_worker_idle_close.py and tests/test_alerter.py.
    """

    name = "minimal"

    async def start(self) -> None:  # pragma: no cover - not exercised
        return None

    async def stop(self) -> None:  # pragma: no cover - not exercised
        return None

    async def send(self, message: OutboundMessage) -> None:  # pragma: no cover
        return None

    def subscribe(self, handler) -> None:  # pragma: no cover
        return None

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    @classmethod
    def conversation_key_for(cls, event: Any) -> str:  # pragma: no cover
        return "minimal:test"


def _make_event() -> InboundEvent:
    return InboundEvent(
        transport="minimal",
        conversation_key="minimal:test",
        sender_id="u1",
        sender_display="user",
        text="hello",
        is_dm=True,
        is_thread=False,
    )


async def test_default_thinking_signal_methods_are_noop() -> None:
    """A subclass that does not override the visibility hooks should
    inherit awaitable no-ops that return None and never raise.
    """

    adapter = _MinimalAdapter()
    event = _make_event()

    start_result = await adapter.start_thinking_signal(event)
    assert start_result is None

    # Pass a synthetic handle through clear_thinking_signal to confirm
    # the default no-op tolerates any handle shape without raising.
    handle = SignalHandle(transport="minimal", conv_key="minimal:test")
    clear_result = await adapter.clear_thinking_signal(handle)
    assert clear_result is None


async def test_null_visibility_callbacks_are_awaitable_noops() -> None:
    """``NULL_VISIBILITY.start`` and ``NULL_VISIBILITY.clear`` must be
    awaitable coroutines that return ``None`` cleanly. This is the
    contract the worker relies on when it is constructed without a
    real :class:`VisibilityCallbacks` bundle (every existing unit test
    and the sub-agent path).
    """

    event = _make_event()

    start_result = await NULL_VISIBILITY.start(event)
    assert start_result is None

    handle = SignalHandle(transport="minimal", conv_key="minimal:test")
    clear_result = await NULL_VISIBILITY.clear(handle)
    assert clear_result is None

    # Clearing a ``None`` handle is also safe — the worker only calls
    # clear when start returned a handle, but the noop must tolerate
    # the broader type signature regardless.
    none_clear_result = await NULL_VISIBILITY.clear(None)
    assert none_clear_result is None

    # The remaining two fields are inert defaults on the noop bundle.
    assert NULL_VISIBILITY.lint is None
    assert NULL_VISIBILITY.cadence_reminder_loader is None
