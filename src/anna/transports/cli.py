"""CLI adapter (daemon-side).

Phase 2 §5 subtask 5. Wraps :class:`anna.transports.cli_server.UnixSocketServer`
into a :class:`ChannelAdapter` matching the Slack/Telegram surface so the
router can drive it identically. The socket-level plumbing (NDJSON framing,
per-connection task, hello/bye handshake) lives in ``cli_server.py``; this
module is responsible for:

* Conversation-key derivation from ``mode`` + ``username``.
* Per-session state tracking so outbound :class:`OutboundMessage` writes can
  reach the right client socket.
* Identity-alias reverse mapping so the worker's outbound key
  (``user:<canonical>`` after :class:`ConversationRouter` rewrites the
  inbound) still finds the session that was opened under the per-transport
  shape (``cli:local:<username>``).
* Building :class:`InboundEvent` per ``user_message`` frame with a
  ``stream_subscriber`` that forwards :class:`TextBlock` chunks back to the
  client as ``delta`` frames.

The CLI conv_key shapes:

* Interactive (``anna chat``):  ``cli:local:<username>``
* One-shot   (``anna ask``):   ``cli:oneshot:<uuid4>``

The interactive form is the *only* one identity-aliasing may rewrite. The
one-shot form is always fresh — a per-invocation UUID — so it never
matches an alias entry and never resumes prior context.

See ``Inbox/2026-06-01-ANNA-Phase-2-CLI-Transport-Plan.md`` for the full
design.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from anna.config import AnnaConfig
from anna.log import get_logger
from anna.transports.base import (
    ChannelAdapter,
    InboundEvent,
    InboundHandler,
    OutboundMessage,
)
from anna.transports.cli_server import ClientSession, UnixSocketServer


class CLIAdapter(ChannelAdapter):
    """Daemon-side CLI transport.

    Owns the Unix-socket :class:`UnixSocketServer` and a session table
    keyed by conv_key. The session table is the only piece of state that
    matters for outbound routing: when :meth:`send` is called with an
    :class:`OutboundMessage`, the adapter looks up the recipient session
    by ``message.conversation_key`` and writes a final-text plus
    ``end_of_turn`` frame over its writer.

    Identity-alias reverse mapping is computed once at construction from
    ``config.identities``: any entry with ``cli_username`` set produces
    a second index entry under ``user:<canonical>`` so the post-alias
    outbound key still resolves to the live session. The forward
    (per-transport) key is also registered so a session that was *not*
    aliased (e.g. a different OS user) still resolves.
    """

    name = "cli"

    def __init__(self, *, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.transport.cli")
        self._handlers: list[InboundHandler] = []
        self._sessions: dict[str, ClientSession] = {}
        self._server: UnixSocketServer | None = None

        # Reverse mapping: cli_username -> canonical. Built once at
        # construction so on_session can register the session under both
        # the per-transport key (``cli:local:<username>``) and the post-
        # alias key (``user:<canonical>``). When the router rewrites the
        # inbound conv_key on its way to the worker, the worker's
        # outbound message carries the rewritten key; without this
        # reverse lookup ``send()`` could not find the session.
        self._username_to_canonical: dict[str, str] = {}
        for entry in config.identities:
            if entry.cli_username:
                self._username_to_canonical[entry.cli_username] = entry.canonical

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        socket_path = self._config.transports.cli.resolved_socket_path
        self._server = UnixSocketServer(
            socket_path=socket_path,
            on_session=self._on_session,
            on_inbound=self._on_inbound,
        )
        await self._server.start()
        self._log.info(
            "channel.connected",
            channel="cli",
            socket_path=str(socket_path),
        )

    async def stop(self) -> None:
        if self._server is not None:
            try:
                await self._server.stop()
            except Exception as exc:
                self._log.warning("channel.close_failed", channel="cli", error=str(exc))
            self._server = None
        self._sessions.clear()
        self._log.info("channel.disconnected", channel="cli", reason="clean")

    # ------------------------------------------------------------------
    # Session lifecycle (server callbacks)
    # ------------------------------------------------------------------

    async def _on_session(self, session: ClientSession) -> None:
        """Finalize a new session: derive the real conv_key, register, ack.

        For ``interactive`` mode, the conv_key is ``cli:local:<username>``
        (matches the cli_server placeholder). Identity-alias rewriting
        happens at router ingress, not here.

        For ``oneshot`` mode, overwrite the placeholder (which used the
        username) with ``cli:oneshot:<uuid4>`` so each invocation gets a
        unique key. This means oneshot conv_keys never collide and never
        match an identity alias entry.
        """
        if session.mode == "oneshot":
            session.conv_key = f"cli:oneshot:{uuid.uuid4()}"
        else:
            # Interactive: the placeholder ``cli:local:<username>`` is
            # already correct. No rewrite here.
            session.conv_key = f"cli:local:{session.username}"

        # Register under the per-transport key. Always.
        self._sessions[session.conv_key] = session

        # Also register under the post-alias key if the username maps to
        # a canonical. Lets ``send()`` find this session when the
        # worker's outbound carries ``user:<canonical>`` after the router
        # rewrites the inbound conv_key.
        canonical = self._username_to_canonical.get(session.username)
        if canonical is not None and session.mode == "interactive":
            alias_key = f"user:{canonical}"
            self._sessions[alias_key] = session
            self._log.debug(
                "cli.session.alias_registered",
                conv_key=session.conv_key,
                alias_key=alias_key,
                username=session.username,
            )

        # Ack back. ``protocol_version: 1`` mirrors the hello-frame
        # version the cli_server validates against in subtask 4.
        await UnixSocketServer.send_frame(
            session.writer,
            session.write_lock,
            {
                "type": "ack",
                "conv_key": session.conv_key,
                "protocol_version": 1,
            },
        )

        self._log.info(
            "cli.session.opened",
            conv_key=session.conv_key,
            username=session.username,
            mode=session.mode,
        )

    async def _on_inbound(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        """Dispatch a parsed inbound frame for an established session.

        Three frame types reach this callback (cli_server filters the
        rest):

        * ``user_message``: build an :class:`InboundEvent` and fan it
          out to subscribed handlers (the router).
        * ``cancel``: log only. Per operator decision #1, v1 does not
          cancel the SDK call or the worker task — the turn runs to
          completion and audit records it as such. A future iteration
          can wire actual cancellation.
        * ``bye``: remove the session from the routing table. The
          cli_server's read loop terminates naturally on the next
          iteration (it also breaks out of the loop on ``bye``).
        """
        frame_type = frame.get("type")
        if frame_type == "user_message":
            await self._dispatch_user_message(session, frame)
        elif frame_type == "cancel":
            self._log.info(
                "cli.cancel_requested",
                conv_key=session.conv_key,
                note="v1 does not cancel the SDK call; turn runs to completion",
            )
        elif frame_type == "bye":
            self._log.info("cli.session.closed", conv_key=session.conv_key, reason="bye")
            self._unregister(session)
        else:
            # Defensive — cli_server already filters unknown types.
            self._log.warning(
                "cli.unhandled_frame",
                conv_key=session.conv_key,
                frame_type=frame_type,
            )

    async def _dispatch_user_message(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        text = frame.get("text", "")
        if not isinstance(text, str):
            self._log.warning(
                "cli.user_message_invalid",
                conv_key=session.conv_key,
                text_type=type(text).__name__,
            )
            return

        # Per-session stream subscriber: forwards each TextBlock chunk
        # back over the socket as a ``delta`` frame. Captured by closure
        # over ``session`` so concurrent sessions can each receive their
        # own deltas without cross-talk.
        async def _stream(chunk: str, _session: ClientSession = session) -> None:
            await UnixSocketServer.send_frame(
                _session.writer,
                _session.write_lock,
                {"type": "delta", "text": chunk},
            )

        inbound = InboundEvent(
            transport="cli",
            conversation_key=session.conv_key,
            sender_id=session.username,
            sender_display=session.username,
            text=text,
            is_dm=True,
            is_thread=False,
            raw={"mode": session.mode, "username": session.username},
            stream_subscriber=_stream,
        )

        self._log.debug(
            "channel.message.received",
            channel="cli",
            conv_key=inbound.conversation_key,
            sender_id=inbound.sender_id,
            text_length=len(inbound.text),
            is_dm=inbound.is_dm,
        )

        for handler in self._handlers:
            try:
                await handler(inbound)
            except Exception as exc:
                self._log.error("router.handler_failed", error=str(exc))

    def _unregister(self, session: ClientSession) -> None:
        """Drop a session from both per-transport and post-alias indices."""
        for key, val in list(self._sessions.items()):
            if val is session:
                self._sessions.pop(key, None)

    # ------------------------------------------------------------------
    # Send (outbound)
    # ------------------------------------------------------------------

    async def send(self, message: OutboundMessage) -> None:
        """Deliver a buffered final-text turn to the right client.

        The streaming deltas already reached the client via
        ``stream_subscriber``. ``send`` is invoked once per turn for the
        buffered finalize that Slack/Telegram also receive (the
        transcript writer downstream of the router needs it). For CLI
        the body of the message is also written as a frame for
        consistency: the TUI ignores it, but ``socat`` operator-spelunk
        sees it.

        For one-shot sessions we additionally write a ``bye`` frame and
        close the writer so the client process exits cleanly.
        """
        session = self._sessions.get(message.conversation_key)
        if session is None:
            self._log.warning(
                "cli.send_no_session",
                conv_key=message.conversation_key,
                text_length=len(message.text),
                note="session disconnected mid-turn; dropping outbound",
            )
            return

        if session.closed.is_set():
            self._log.warning(
                "cli.send_session_closed",
                conv_key=message.conversation_key,
                text_length=len(message.text),
            )
            self._unregister(session)
            return

        try:
            # Final-text frame, then end_of_turn. The TUI ignores the
            # final text (already shown via deltas) but the audit-trail
            # parity with Slack/Telegram is preserved.
            await UnixSocketServer.send_frame(
                session.writer,
                session.write_lock,
                {"type": "final_text", "text": message.text},
            )
            await UnixSocketServer.send_frame(
                session.writer,
                session.write_lock,
                {"type": "end_of_turn"},
            )
            self._log.debug(
                "channel.message.sent",
                channel="cli",
                conv_key=message.conversation_key,
                text_length=len(message.text),
            )
        except Exception as exc:
            self._log.error(
                "channel.send_failed",
                channel="cli",
                conv_key=message.conversation_key,
                text_length=len(message.text),
                error=str(exc),
            )
            # Drop the session — its writer is likely broken.
            self._unregister(session)
            return

        # One-shot sessions terminate after their single turn. Send a
        # ``bye`` frame and close the writer so the client process
        # exits and the cli_server's read loop sees EOF.
        if session.mode == "oneshot":
            try:
                await UnixSocketServer.send_frame(
                    session.writer,
                    session.write_lock,
                    {"type": "bye"},
                )
            except Exception as exc:
                self._log.warning(
                    "cli.oneshot_bye_failed",
                    conv_key=message.conversation_key,
                    error=str(exc),
                )
            try:
                session.writer.close()
            except Exception:
                pass
            self._unregister(session)

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    def subscribe(self, handler: InboundHandler) -> None:
        self._handlers.append(handler)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        return self._server is not None and self._server.is_serving

    # ------------------------------------------------------------------
    # Conv-key derivation
    # ------------------------------------------------------------------

    @classmethod
    def conversation_key_for(cls, event: Any) -> str:
        """Return the conv_key for a CLI event.

        Unlike Slack/Telegram, the CLI conv_key is set at session-open
        time (not per-event) because the OS username and mode arrive in
        the ``hello`` frame, not per ``user_message``. For an
        :class:`InboundEvent` already constructed by this adapter, the
        ``conversation_key`` attribute carries the right value and we
        return it verbatim.

        Accepts dicts shaped like ``{"mode": ..., "username": ...,
        "conv_key": ...}`` for symmetric use in tests.
        """
        if isinstance(event, InboundEvent):
            return event.conversation_key
        if isinstance(event, dict):
            if "conv_key" in event:
                return str(event["conv_key"])
            mode = event.get("mode")
            if mode == "oneshot":
                return f"cli:oneshot:{uuid.uuid4()}"
            username = event.get("username")
            if not username:
                raise ValueError("CLI event dict missing 'username' for interactive mode")
            return f"cli:local:{username}"
        # Fallback: callers that pass a ClientSession directly.
        conv_key = getattr(event, "conversation_key", None) or getattr(event, "conv_key", None)
        if conv_key is None:
            raise TypeError(f"cannot derive conv_key from {type(event).__name__}")
        return str(conv_key)
