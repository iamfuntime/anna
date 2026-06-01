"""ChannelAdapter abstract base class.

Per v3 section 2. Every transport implements the same contract:

* :meth:`start`: open the connection and begin delivering events.
* :meth:`stop`: close the connection cleanly.
* :meth:`send`: deliver an outbound message.
* :meth:`subscribe`: register the router's handler for inbound events.
* :meth:`health_check`: ping the upstream API for the watchdog.
* :meth:`restart`: stop and start, used by the watchdog after failed pings.

A transport-specific event is normalized into :class:`InboundEvent` before
being handed to the subscribed handler. The classmethod
:meth:`conversation_key_for` is the canonical mapping from raw event to the
key the router uses to multiplex workers.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundEvent:
    """Normalized event from any transport.

    ``completion_future`` is an optional asyncio.Future the caller can
    attach to receive the worker's final assistant message in-process
    rather than via the normal outbound send path. Used by the Phase 2
    scheduler so a scheduled fire can route its output to a destination
    that differs from the worker's natural send target. When set, the
    worker resolves the future with the reply text and skips the
    outbound send; when unset (the transport-originated default), the
    worker sends to ``conversation_key`` via the router's send callback
    as it has always done.

    ``stream_subscriber`` is an optional per-event callback the worker
    awaits once per ``TextBlock`` as ``AssistantMessage`` events arrive
    from ``client.receive_response()``. Set by the Phase 2 §5 CLI
    transport so the operator's TUI can render streaming text deltas
    live; Slack and Telegram leave it ``None`` and keep the existing
    buffered final-text send path unchanged. The worker is responsible
    for exception isolation so a misbehaving subscriber cannot drop the
    buffered finalize.

    ``ephemeral`` marks a session whose worker must NOT write a
    checkpoint or run resume-context cleanup at closeout. Set true by
    the Phase 2 §5 CLI adapter for ``anna ask`` (one-shot) sessions so
    each ad-hoc invocation does not pollute
    ``vault/Conversations/cli-oneshot-<uuid>/`` with a per-query
    checkpoint. The flag is read by ``ConversationRouter`` on the first
    event for a given conv_key and propagated to the worker via its
    constructor; subsequent events on the same conv_key reuse the
    already-flagged worker.

    Both callable / future fields are excluded from compare / hash so
    the dataclass remains hashable and equality compares on the
    semantic payload only. ``ephemeral`` is similarly excluded so a
    one-shot event still compares equal to its non-ephemeral peer with
    the same semantic payload.
    """

    transport: str
    conversation_key: str
    sender_id: str
    sender_display: str
    text: str
    is_dm: bool
    is_thread: bool
    raw: dict[str, Any] = field(default_factory=dict)
    completion_future: asyncio.Future[str] | None = field(
        default=None, compare=False, hash=False, repr=False
    )
    stream_subscriber: Callable[[str], Awaitable[None]] | None = field(
        default=None, compare=False, hash=False, repr=False
    )
    ephemeral: bool = field(
        default=False, compare=False, hash=False, repr=False
    )


@dataclass(frozen=True)
class OutboundMessage:
    """Normalized outbound message from the worker."""
    conversation_key: str
    text: str
    structured: dict[str, Any] | None = None
    files: list[str] | None = None
    reply_to: str | None = None


InboundHandler = Callable[[InboundEvent], Awaitable[None]]


class ChannelAdapter(ABC):
    """Abstract base for any ANNA transport.

    Subclasses must set the class attribute ``name`` to a short string
    identifier (e.g., ``"slack"`` or ``"telegram"``).
    """

    name: str = "base"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None: ...

    @abstractmethod
    def subscribe(self, handler: InboundHandler) -> None: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    async def restart(self) -> None:
        """Default restart: stop then start. Subclasses may override."""
        await self.stop()
        await self.start()

    @classmethod
    @abstractmethod
    def conversation_key_for(cls, event: Any) -> str: ...
