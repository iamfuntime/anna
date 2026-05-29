"""Slack adapter.

Uses :class:`slack_bolt.async_app.AsyncApp` over Socket Mode via
:class:`slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler`.

Conversation key derivation, per v3 section 2:

* DM (``channel_type == "im"``): ``slack:dm:<user_id>``.
* Channel thread reply: ``slack:ch:<channel_id>:<thread_ts>``.
* ``app_mention`` not in a thread: ``slack:ch:<channel_id>:<event_ts>:oneshot``.
* ``app_mention`` in a thread: same as channel thread reply.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from anna.config import AnnaConfig
from anna.log import get_logger
from anna.transports.base import ChannelAdapter, InboundEvent, InboundHandler, OutboundMessage


class SlackAdapter(ChannelAdapter):
    name = "slack"

    def __init__(self, *, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.transport.slack")
        self._handlers: list[InboundHandler] = []
        self._app: Any = None
        self._handler: Any = None
        self._handler_task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._connect_attempt = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError as exc:
            self._log.error("channel.import_failed", channel="slack", error=str(exc))
            raise

        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        app_token = os.environ.get("SLACK_APP_TOKEN")
        if not bot_token or not app_token:
            raise RuntimeError(
                "Slack transport enabled but SLACK_BOT_TOKEN or SLACK_APP_TOKEN missing"
            )

        self._app = AsyncApp(token=bot_token)
        self._client = self._app.client
        self._register_listeners()

        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._connect_attempt += 1
        # start_async blocks. Run it in a task so start() returns control.
        self._handler_task = asyncio.create_task(
            self._handler.start_async(),
            name="slack.socket_mode",
        )
        self._log.info(
            "channel.connected",
            channel="slack",
            attempt=self._connect_attempt,
        )

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception as exc:
                self._log.warning("channel.close_failed", channel="slack", error=str(exc))
        if self._handler_task is not None:
            self._handler_task.cancel()
            try:
                await self._handler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._handler_task = None
        self._log.info("channel.disconnected", channel="slack", reason="clean")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, message: OutboundMessage) -> None:
        if self._client is None:
            raise RuntimeError("Slack adapter not started")
        channel, thread_ts = self._channel_and_thread_for(message.conversation_key)
        try:
            kwargs: dict[str, Any] = {"channel": channel, "text": message.text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            if message.structured and "blocks" in message.structured:
                kwargs["blocks"] = message.structured["blocks"]
            response = await self._client.chat_postMessage(**kwargs)
            self._log.debug(
                "channel.message.sent",
                channel="slack",
                conv_key=message.conversation_key,
                text_length=len(message.text),
                ts=response.get("ts"),
            )
        except Exception as exc:
            self._log.error(
                "channel.send_failed",
                channel="slack",
                conv_key=message.conversation_key,
                text_length=len(message.text),
                error=str(exc),
            )
            raise

    def _channel_and_thread_for(self, conv_key: str) -> tuple[str, str | None]:
        """Recover the Slack channel and thread_ts from a conversation_key.

        Mirror of :meth:`conversation_key_for`. Knows about three shapes:
        slack:dm:<user>, slack:ch:<channel>:<ts>, slack:ch:<channel>:<ts>:oneshot.
        """
        parts = conv_key.split(":")
        if len(parts) >= 3 and parts[0] == "slack" and parts[1] == "dm":
            # DMs do not use thread_ts. The Web API accepts the user ID as
            # ``channel`` and opens the DM if necessary.
            return (parts[2], None)
        if parts[0] == "slack" and parts[1] == "ch":
            channel = parts[2]
            thread_ts = parts[3]
            return (channel, thread_ts)
        raise ValueError(f"unrecognized slack conv_key: {conv_key}")

    # ------------------------------------------------------------------
    # Subscribe and listeners
    # ------------------------------------------------------------------

    def subscribe(self, handler: InboundHandler) -> None:
        self._handlers.append(handler)

    def _register_listeners(self) -> None:
        @self._app.event("app_mention")
        async def _on_mention(event: dict[str, Any], body: dict[str, Any]) -> None:
            await self._dispatch_event(event, body)

        @self._app.event("message")
        async def _on_message(event: dict[str, Any], body: dict[str, Any]) -> None:
            # Ignore bot echoes and message-edit subtypes.
            if event.get("bot_id") or event.get("subtype"):
                return
            await self._dispatch_event(event, body)

    async def _dispatch_event(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        try:
            inbound = self._to_inbound_event(event)
        except Exception as exc:
            self._log.warning("channel.normalize_failed", channel="slack", error=str(exc))
            return
        self._log.debug(
            "channel.message.received",
            channel="slack",
            conv_key=inbound.conversation_key,
            sender_id=inbound.sender_id,
            text_length=len(inbound.text),
            is_dm=inbound.is_dm,
            is_thread=inbound.is_thread,
        )
        for handler in self._handlers:
            try:
                await handler(inbound)
            except Exception as exc:
                self._log.error("router.handler_failed", error=str(exc))

    def _to_inbound_event(self, event: dict[str, Any]) -> InboundEvent:
        channel_type = event.get("channel_type", "")
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts")
        event_ts = event.get("ts", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        is_dm = channel_type == "im"
        is_thread = bool(thread_ts)

        conv_key = self.conversation_key_for(event)

        return InboundEvent(
            transport="slack",
            conversation_key=conv_key,
            sender_id=user_id,
            sender_display=user_id,  # Display-name lookup is a Phase 2 enrichment.
            text=text,
            is_dm=is_dm,
            is_thread=is_thread,
            raw=event,
        )

    @classmethod
    def conversation_key_for(cls, event: Any) -> str:
        """Map a raw Slack event dict to the canonical conversation_key."""
        if not isinstance(event, dict):
            raise TypeError("Slack event must be a dict")

        channel_type = event.get("channel_type", "")
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts")
        event_ts = event.get("ts", "")
        user_id = event.get("user", "")
        event_type = event.get("type", "")

        if channel_type == "im":
            return f"slack:dm:{user_id}"
        if thread_ts:
            return f"slack:ch:{channel_id}:{thread_ts}"
        if event_type == "app_mention":
            return f"slack:ch:{channel_id}:{event_ts}:oneshot"
        # Fallback: treat top-level channel messages as one-shot.
        return f"slack:ch:{channel_id}:{event_ts}:oneshot"

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            response = await self._client.auth_test()
            return bool(response.get("ok", False))
        except Exception as exc:
            self._log.warning("channel.health_check_failed", channel="slack", error=str(exc))
            return False
