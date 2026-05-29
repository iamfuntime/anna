"""Per-conversation worker.

Per v3 section 6. One async worker per active conversation_key, owning one
:class:`claude_agent_sdk.ClaudeSDKClient`. The worker reads events from an
``asyncio.Queue``, dispatches them through the SDK, and writes a vault
checkpoint when it idles out.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from anna.config import AnnaConfig
from anna.log import get_logger
from anna.transports.base import InboundEvent, OutboundMessage

if TYPE_CHECKING:
    from anna.runtime.supervisor import Supervisor


SendCallback = Callable[[OutboundMessage], Awaitable[None]]


class ConversationWorker:
    """An async worker that owns one Claude SDK session for one conversation."""

    def __init__(
        self,
        *,
        conversation_key: str,
        transport: str,
        config: AnnaConfig,
        supervisor: "Supervisor",
        send: SendCallback,
    ) -> None:
        self.conversation_key = conversation_key
        self.transport = transport
        self._config = config
        self._supervisor = supervisor
        self._send = send
        self._log = get_logger("anna.worker").bind(conv_key=conversation_key, channel=transport)

        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue(maxsize=128)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._client: object | None = None

        now = datetime.now(timezone.utc)
        self.last_active: datetime = now
        self.last_event_received_at: datetime | None = None
        self.last_event_processed_at: datetime | None = None
        self.is_dm: bool = conversation_key.split(":")[1].startswith("dm") if ":" in conversation_key else False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"worker.{self.conversation_key}")
        self._log.info("worker.spawn")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._close_client()
        self._log.info("worker.complete")

    async def restart(self) -> None:
        await self.stop()
        self._stopping = False
        await self.start()

    async def submit(self, event: InboundEvent) -> None:
        self.last_event_received_at = datetime.now(timezone.utc)
        await self._queue.put(event)

    # ------------------------------------------------------------------
    # Inner loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            await self._ensure_client()
            while not self._stopping:
                event = await self._queue.get()
                try:
                    await self._handle(event)
                finally:
                    self.last_event_processed_at = datetime.now(timezone.utc)
                    self.last_active = self.last_event_processed_at
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error("worker.crashed", error=str(exc))
            raise

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:
            self._log.critical("worker.sdk_import_failed", error=str(exc))
            raise

        options = ClaudeAgentOptions(
            # Per v3, ANNA's per-conversation system prompt embeds the IDENTITY.md
            # frame plus the core CLAUDE.md operating instructions. The actual
            # prompt assembly is the responsibility of the prompt builder in a
            # later module; we wire a minimal default here.
            system_prompt=f"You are ANNA. Active conversation key: {self.conversation_key}.",
        )
        # ClaudeSDKClient is an async context manager. We hold it open for the
        # life of the worker and close it in stop().
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        self._client = client

    async def _close_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.__aexit__(None, None, None)  # type: ignore[attr-defined]
        except Exception as exc:
            self._log.warning("worker.client_close_failed", error=str(exc))
        finally:
            self._client = None

    async def _handle(self, event: InboundEvent) -> None:
        if self._client is None:
            return

        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        except ImportError:
            AssistantMessage = ResultMessage = TextBlock = None  # type: ignore[assignment,misc]

        # Send the user message into the SDK.
        try:
            await self._client.query(event.text)  # type: ignore[attr-defined]
        except Exception as exc:
            self._log.error("worker.sdk_query_failed", error=str(exc))
            await self._send(OutboundMessage(
                conversation_key=event.conversation_key,
                text=f"I hit an error talking to the model: {exc}",
            ))
            return

        # Collect text blocks until ResultMessage.
        reply_chunks: list[str] = []
        try:
            async for msg in self._client.receive_response():  # type: ignore[attr-defined]
                if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if TextBlock is not None and isinstance(block, TextBlock):
                            reply_chunks.append(block.text)
                if ResultMessage is not None and isinstance(msg, ResultMessage):
                    break
        except Exception as exc:
            self._log.error("worker.sdk_receive_failed", error=str(exc))
            await self._send(OutboundMessage(
                conversation_key=event.conversation_key,
                text=f"I hit an error reading the model response: {exc}",
            ))
            return

        reply_text = "\n".join(c for c in reply_chunks if c).strip()
        if not reply_text:
            reply_text = "(no response)"

        await self._send(OutboundMessage(
            conversation_key=event.conversation_key,
            text=reply_text,
        ))
