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

# Telegram rejects ``sendMessage`` payloads whose text exceeds 4096
# characters. Slack's ``chat.postMessage`` tolerates much more but
# truncates/degrades very long single messages, so we apply a
# conservative practical cap there too. Both are enforced at the
# transport send boundary via :func:`split_message_text` (per the
# Inbox/2026-06-04 periodic-flush plan, decision C).
TELEGRAM_MAX_CHARS = 4096
SLACK_MAX_CHARS = 3900


def split_message_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Used by the Slack and Telegram send paths so an oversized message
    (a drip, the final flush, or a pre-existing long single reply) is
    delivered as a sequence of messages instead of being rejected or
    truncated by the upstream API.

    Boundary preference, in order:

    1. The whole string when it already fits within ``limit``.
    2. The last newline at/under ``limit`` (keeps paragraphs intact).
    3. The last whitespace at/under ``limit`` (avoids cutting a word).
    4. A hard cut at exactly ``limit`` (last resort — a single token
       longer than ``limit``, e.g. an URL or hash).

    Chunk order is preserved. An empty / whitespace-only input returns a
    single-element list containing the original string so callers keep
    their existing "send exactly one message" behavior unchanged.
    """
    if limit <= 0:
        raise ValueError("split_message_text limit must be > 0")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # If the character just past the window is a boundary, the full
        # ``limit``-char window fits cleanly and that boundary is consumed.
        if remaining[limit] in (" ", "\n"):
            cut = limit
        else:
            window = remaining[:limit]
            # Prefer the last newline, then the last whitespace, then a hard
            # cut (a single token longer than ``limit``: URL, hash, …).
            cut = window.rfind("\n")
            if cut <= 0:
                cut = window.rfind(" ")
            if cut <= 0:
                cut = limit
        chunks.append(remaining[:cut])
        # Drop a single boundary whitespace char so it is not re-emitted as
        # leading whitespace on the next chunk; a hard cut keeps every char.
        if cut < len(remaining) and remaining[cut] in (" ", "\n"):
            remaining = remaining[cut + 1 :]
        else:
            remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


@dataclass(frozen=True)
class ImageAttachment:
    """A single inbound image carried on an :class:`InboundEvent`.

    ``media_type`` is the Anthropic-viewable MIME type (one of
    image/jpeg, image/png, image/gif, image/webp) and ``data`` is the
    raw image bytes as downloaded from the transport. The worker
    base64-encodes ``data`` into an SDK image content block at query
    time. Kept deliberately tiny — transports build these only after the
    subtype allow-set and size caps have passed.
    """

    media_type: str
    data: bytes


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

    ``images`` carries inbound image attachments (Slack drag-and-drop)
    as :class:`ImageAttachment` records the worker base64-encodes into
    SDK image content blocks. Empty for every text and voice turn; voice
    inbound is exclusive and never co-carries images. Like the callable
    fields it is excluded from compare / hash / repr so the dataclass
    stays hashable and the raw bytes never bloat logs.

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
    images: list[ImageAttachment] = field(
        default_factory=list, compare=False, hash=False, repr=False
    )


@dataclass(frozen=True)
class OutboundMessage:
    """Normalized outbound message from the worker."""
    conversation_key: str
    text: str
    structured: dict[str, Any] | None = None
    files: list[str] | None = None
    reply_to: str | None = None


@dataclass
class SignalHandle:
    """Per-transport cleanup state for a thinking-signal.

    Fields are deliberately union-typed because each transport carries
    different cleanup state. None of these fields enter compare/hash —
    handles are passed by reference. ``telegram_stopped`` is the
    Telegram refresher's stop-flag; Slack and CLI ignore it. The
    dataclass is intentionally not frozen so the Telegram transport can
    swap in a freshly-created ``asyncio.Event`` reference when it
    spawns the refresher task.
    """

    transport: str
    conv_key: str
    # Slack: channel + ts + emoji name. Telegram: typing task + stopped
    # event. CLI: ref to the session. All optional so unused fields
    # default to None for transports that ignore them.
    slack_channel: str | None = None
    slack_ts: str | None = None
    slack_emoji: str | None = None
    telegram_task: asyncio.Task[None] | None = None
    telegram_stopped: asyncio.Event | None = None
    cli_session_key: str | None = None


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

    async def start_thinking_signal(
        self, event: InboundEvent
    ) -> SignalHandle | None:
        """Post a transport-specific 'working' signal.

        Default no-op. Subclasses override only if they support a
        visible thinking-signal (Slack reactions, Telegram typing
        action, CLI socket frame). Implementations MUST be
        exception-safe — a failure here must not abort the SDK call.
        Returns ``None`` when the signal cannot be posted (e.g. missing
        metadata in ``event.raw``) so the worker can skip the
        corresponding clear path.
        """

        return None

    async def clear_thinking_signal(self, handle: SignalHandle) -> None:
        """Remove the thinking signal.

        Default no-op. Subclasses override to undo whatever
        ``start_thinking_signal`` posted. Implementations MUST be
        exception-safe — a failure here must not propagate into the
        worker's ``finally`` block.
        """

        return None

    @classmethod
    @abstractmethod
    def conversation_key_for(cls, event: Any) -> str: ...
