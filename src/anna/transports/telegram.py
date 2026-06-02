"""Telegram adapter.

Uses python-telegram-bot v22+ async API:
:class:`telegram.ext.Application.builder().token(...).build()`, a
:class:`telegram.ext.MessageHandler` with ``filters.TEXT & ~filters.COMMAND``,
and the ``async with application: application.start()`` lifecycle pattern.

Conversation key derivation, per v3 section 2:

* Private chat: ``telegram:dm:<chat_id>``.
* Group chat: ``telegram:gr:<chat_id>``.
* Group with topics enabled: ``telegram:gr:<chat_id>:<topic_id>``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from anna.config import AnnaConfig
from anna.log import get_logger
from anna.transports.base import (
    ChannelAdapter,
    InboundEvent,
    InboundHandler,
    OutboundMessage,
    SignalHandle,
)


async def _typing_refresher(
    bot: Any,
    chat_id: int,
    stop_event: asyncio.Event,
    max_seconds: int,
    log: Any,
) -> None:
    """Refresh Telegram's ``typing`` chat-action every ~4 seconds.

    Telegram clears the typing indicator ~5 seconds after the last
    ``send_chat_action`` call, so a buffered turn needs a refresher to
    keep the indicator alive for the duration of the SDK turn. The loop
    exits when ``stop_event`` is set OR when its run time exceeds
    ``max_seconds``; per-tick failures are logged at debug and do not
    abort the loop.
    """

    start = time.monotonic()
    while not stop_event.is_set():
        if time.monotonic() - start > max_seconds:
            log.warning(
                "visibility.telegram.refresh_bound_hit",
                chat_id=chat_id,
                max_seconds=max_seconds,
            )
            return
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as exc:
            log.debug("visibility.telegram.refresh_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue
        return


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def __init__(self, *, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.transport.telegram")
        self._handlers: list[InboundHandler] = []
        self._application: Any = None
        self._updater_task: asyncio.Task[None] | None = None
        self._connect_attempt = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        try:
            from telegram.ext import ApplicationBuilder, MessageHandler, filters
        except ImportError as exc:
            self._log.error("channel.import_failed", channel="telegram", error=str(exc))
            raise

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("Telegram transport enabled but TELEGRAM_BOT_TOKEN missing")

        self._application = ApplicationBuilder().token(token).build()
        self._application.add_handler(
            MessageHandler(filters.TEXT & (~filters.COMMAND), self._on_message)
        )

        await self._application.initialize()
        await self._application.start()
        # Long polling. PTB's Updater starts a background task internally.
        self._updater_task = asyncio.create_task(
            self._application.updater.start_polling(),
            name="telegram.polling",
        )
        self._connect_attempt += 1
        bot_username = ""
        try:
            me = await self._application.bot.get_me()
            bot_username = me.username or ""
        except Exception:
            pass
        self._log.info(
            "channel.connected",
            channel="telegram",
            attempt=self._connect_attempt,
            bot_username=bot_username,
        )

    async def stop(self) -> None:
        if self._application is None:
            return
        try:
            if self._application.updater is not None:
                await self._application.updater.stop()
        except Exception as exc:
            self._log.warning("channel.updater_stop_failed", channel="telegram", error=str(exc))
        try:
            await self._application.stop()
            await self._application.shutdown()
        except Exception as exc:
            self._log.warning("channel.close_failed", channel="telegram", error=str(exc))
        if self._updater_task is not None:
            self._updater_task.cancel()
            try:
                await self._updater_task
            except (asyncio.CancelledError, Exception):
                pass
            self._updater_task = None
        self._log.info("channel.disconnected", channel="telegram", reason="clean")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, message: OutboundMessage) -> None:
        if self._application is None:
            raise RuntimeError("Telegram adapter not started")
        chat_id, topic_id = self._chat_and_topic_for(message.conversation_key)
        try:
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": message.text}
            if topic_id is not None:
                kwargs["message_thread_id"] = topic_id
            if message.reply_to:
                kwargs["reply_to_message_id"] = int(message.reply_to)
            await self._application.bot.send_message(**kwargs)
            self._log.debug(
                "channel.message.sent",
                channel="telegram",
                conv_key=message.conversation_key,
                text_length=len(message.text),
            )
        except Exception as exc:
            self._log.error(
                "channel.send_failed",
                channel="telegram",
                conv_key=message.conversation_key,
                error=str(exc),
            )
            raise

    def _chat_and_topic_for(self, conv_key: str) -> tuple[int, int | None]:
        parts = conv_key.split(":")
        # telegram:dm:<chat_id> or telegram:gr:<chat_id> or telegram:gr:<chat_id>:<topic_id>
        if parts[0] != "telegram" or len(parts) < 3:
            raise ValueError(f"unrecognized telegram conv_key: {conv_key}")
        chat_id = int(parts[2])
        topic_id = int(parts[3]) if len(parts) >= 4 else None
        return (chat_id, topic_id)

    # ------------------------------------------------------------------
    # Subscribe and listeners
    # ------------------------------------------------------------------

    def subscribe(self, handler: InboundHandler) -> None:
        self._handlers.append(handler)

    async def _on_message(self, update: Any, context: Any) -> None:
        try:
            inbound = self._to_inbound_event(update)
        except Exception as exc:
            self._log.warning("channel.normalize_failed", channel="telegram", error=str(exc))
            return
        self._log.debug(
            "channel.message.received",
            channel="telegram",
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

    def _to_inbound_event(self, update: Any) -> InboundEvent:
        message = update.message
        if message is None:
            raise ValueError("telegram update has no message")
        chat = message.chat
        user = message.from_user
        is_dm = chat.type == "private"
        text = message.text or ""
        sender_id = str(user.id) if user else ""
        sender_display = (user.full_name if user else "") or sender_id

        conv_key = self._derive_key(
            chat_id=chat.id,
            chat_type=chat.type,
            topic_id=getattr(message, "message_thread_id", None),
        )

        return InboundEvent(
            transport="telegram",
            conversation_key=conv_key,
            sender_id=sender_id,
            sender_display=sender_display,
            text=text,
            is_dm=is_dm,
            is_thread=False,
            raw={
                "chat_id": chat.id,
                "chat_type": chat.type,
                "message_id": message.message_id,
                "topic_id": getattr(message, "message_thread_id", None),
            },
        )

    @staticmethod
    def _derive_key(*, chat_id: int, chat_type: str, topic_id: int | None) -> str:
        if chat_type == "private":
            return f"telegram:dm:{chat_id}"
        if topic_id is not None:
            return f"telegram:gr:{chat_id}:{topic_id}"
        return f"telegram:gr:{chat_id}"

    @classmethod
    def conversation_key_for(cls, event: Any) -> str:
        """Map a raw telegram Update (or a dict shaped like one) to a conv_key.

        Accepts either a real ``telegram.Update`` object or a dict with the
        keys the test suite uses (``chat_id``, ``chat_type``, ``topic_id``).
        """
        if isinstance(event, dict):
            return cls._derive_key(
                chat_id=int(event["chat_id"]),
                chat_type=event.get("chat_type", "private"),
                topic_id=event.get("topic_id"),
            )
        # Real Update object.
        message = event.message
        chat = message.chat
        return cls._derive_key(
            chat_id=chat.id,
            chat_type=chat.type,
            topic_id=getattr(message, "message_thread_id", None),
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._application is None:
            return False
        try:
            await self._application.bot.get_me()
            return True
        except Exception as exc:
            self._log.warning("channel.health_check_failed", channel="telegram", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Thinking-signal overrides (subtask 9 of cadence-visibility plan)
    # ------------------------------------------------------------------

    async def start_thinking_signal(
        self, event: InboundEvent
    ) -> SignalHandle | None:
        """Spawn a typing-action refresher for the duration of the turn.

        Reads ``chat_id`` from ``event.raw`` (populated by
        :meth:`_to_inbound_event`). Exception-isolated — any failure
        logs ``visibility.telegram.start_failed`` and returns ``None``
        so the worker skips the corresponding clear path.
        """

        try:
            if self._application is None:
                return None
            chat_id = event.raw.get("chat_id") if event.raw else None
            if chat_id is None:
                return None
            stop_event = asyncio.Event()
            max_seconds = (
                self._config.runtime.visibility.telegram_typing_max_seconds
            )
            task = asyncio.create_task(
                _typing_refresher(
                    self._application.bot,
                    int(chat_id),
                    stop_event,
                    max_seconds,
                    self._log,
                ),
                name=f"telegram.typing_refresher:{event.conversation_key}",
            )
            return SignalHandle(
                transport="telegram",
                conv_key=event.conversation_key,
                telegram_task=task,
                telegram_stopped=stop_event,
            )
        except Exception as exc:
            self._log.warning(
                "visibility.telegram.start_failed",
                conv_key=event.conversation_key,
                error=str(exc),
            )
            return None

    async def clear_thinking_signal(self, handle: SignalHandle) -> None:
        """Stop the refresher and await its exit.

        Sets the stop event, waits up to one second for clean exit, and
        falls back to ``task.cancel()`` if the task is still alive.
        Exception-isolated so a misbehaving cleanup never propagates
        into the worker's ``finally`` block.
        """

        if handle.telegram_stopped is None or handle.telegram_task is None:
            return
        try:
            handle.telegram_stopped.set()
            try:
                await asyncio.wait_for(handle.telegram_task, timeout=1.0)
            except asyncio.TimeoutError:
                handle.telegram_task.cancel()
                try:
                    await handle.telegram_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._log.debug(
                "visibility.telegram.cleared",
                conv_key=handle.conv_key,
            )
        except Exception as exc:
            self._log.debug(
                "visibility.telegram.clear_failed",
                conv_key=handle.conv_key,
                error=str(exc),
            )
