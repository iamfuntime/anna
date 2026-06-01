"""Conversation router.

Per v3 section 6. Takes a normalized :class:`InboundEvent` from any transport,
derives the conversation_key, looks up or spawns the per-conversation worker,
and hands the event to the worker's queue.

The router also writes the inbound and outbound transcript lines. It is the
only component with both sides of the conversation in scope.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger, sweep_audit_retention, sweep_transcript_retention, transcript_event
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker
from anna.tools.google_clients import GoogleClients
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


SendCallback = Callable[[OutboundMessage], Awaitable[None]]


class ConversationRouter:
    def __init__(
        self,
        *,
        config: AnnaConfig,
        supervisor: Supervisor,
        adapters: dict[str, ChannelAdapter],
        schedule_store: ScheduleStore | None = None,
        google_clients: GoogleClients | None = None,
    ) -> None:
        self._config = config
        self._supervisor = supervisor
        self._adapters = adapters
        self._schedule_store = schedule_store
        self._google_clients = google_clients
        self._log = get_logger("anna.router")
        self._workers: dict[str, ConversationWorker] = {}
        self._workers_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, event: InboundEvent) -> None:
        """Subscribe target. Called by every ChannelAdapter for inbound events."""
        # Transcript: inbound line.
        transcript_event(
            channel=event.transport,
            conv_key=event.conversation_key,
            transcripts_dir=self._config.transcripts_dir,
            direction="inbound",
            text=event.text,
            sender_id=event.sender_id,
            sender_display=event.sender_display,
            is_dm=event.is_dm,
            is_thread=event.is_thread,
        )

        worker = await self._get_or_spawn_worker(event)
        self._log.debug(
            "router.dispatch",
            conv_key=event.conversation_key,
            transport=event.transport,
        )
        await worker.submit(event)

    async def _get_or_spawn_worker(self, event: InboundEvent) -> ConversationWorker:
        key = event.conversation_key
        async with self._workers_lock:
            worker = self._workers.get(key)
            if worker is not None:
                return worker
            worker = ConversationWorker(
                conversation_key=key,
                transport=event.transport,
                config=self._config,
                supervisor=self._supervisor,
                send=self._send_factory(event.transport),
                on_idle_close=self._idle_close_callback,
                schedule_store=self._schedule_store,
                google_clients=self._google_clients,
            )
            await worker.start()
            self._workers[key] = worker
            self._log.info(
                "conversation.start",
                channel=event.transport,
                conv_key=key,
            )
        return worker

    async def _idle_close_callback(self, key: str) -> None:
        """Called by a worker's idle watcher when it has been silent too long.

        We pop the worker from the registry first (so a fresh inbound message
        spawns a brand new worker rather than reviving the closing one), then
        run the full close path so the checkpoint+eviction routine fires.
        """
        async with self._workers_lock:
            worker = self._workers.pop(key, None)
        if worker is None:
            return
        try:
            await worker.stop()
        except Exception as exc:
            self._log.error("conversation.idle_close.stop_failed", conv_key=key, error=str(exc))
        self._log.info(
            "conversation.end",
            channel=worker.transport,
            conv_key=key,
            reason="idle",
        )

    def _send_factory(self, transport: str) -> SendCallback:
        """Build a per-transport send callback the worker can invoke."""
        adapter = self._adapters[transport]

        async def _send(message: OutboundMessage) -> None:
            await adapter.send(message)
            transcript_event(
                channel=transport,
                conv_key=message.conversation_key,
                transcripts_dir=self._config.transcripts_dir,
                direction="outbound",
                text=message.text,
            )

        return _send

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def list_workers(self) -> list[ConversationWorker]:
        return list(self._workers.values())

    async def close_worker(self, key: str) -> None:
        async with self._workers_lock:
            worker = self._workers.pop(key, None)
        if worker is None:
            return
        await worker.stop()
        self._log.info(
            "conversation.end",
            channel=worker.transport,
            conv_key=key,
        )

    async def shutdown(self) -> None:
        """Stop every active worker so each gets a chance to run closeout.

        Called from the process-level shutdown path in ``__main__.py`` before
        adapters are torn down. Drains the worker registry first so that any
        late dispatch attempt cannot revive a worker we are tearing down.
        Errors in individual ``worker.stop()`` calls are logged but do not
        prevent the remaining workers from being closed.
        """
        async with self._workers_lock:
            workers = list(self._workers.values())
            self._workers.clear()

        if not workers:
            self._log.info("router.shutdown.no_workers")
            return

        self._log.info("router.shutdown.start", workers=len(workers))
        results = await asyncio.gather(
            *(worker.stop() for worker in workers),
            return_exceptions=True,
        )
        failures = 0
        for worker, result in zip(workers, results):
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                failures += 1
                self._log.error(
                    "router.shutdown.worker_failed",
                    conv_key=worker.conversation_key,
                    error=str(result),
                )
            else:
                self._log.info(
                    "conversation.end",
                    channel=worker.transport,
                    conv_key=worker.conversation_key,
                    reason="shutdown",
                )
        self._log.info(
            "router.shutdown.complete",
            workers=len(workers),
            failures=failures,
        )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    async def run_housekeeping(self) -> None:
        """Daily sweep: audit retention, transcript gzip and delete, idle workers.

        Runs once on startup, then waits for the configured ``daily_sweep_time``
        each subsequent day.
        """
        try:
            while True:
                await self._run_sweep_once()
                await self._sleep_until_next_sweep()
        except asyncio.CancelledError:
            self._log.info("housekeeping.stopped")
            raise

    async def _run_sweep_once(self) -> None:
        self._log.info("housekeeping.sweep.start")
        # Idle-worker closeout is no longer part of the daily sweep — each
        # worker now runs its own continuous idle watcher (see
        # ConversationWorker._idle_watch). The sweep only handles audit
        # retention and transcript gzip/delete.
        try:
            deleted_audit = sweep_audit_retention(
                self._config.audit_dir,
                self._config.logging.audit.retention_days,
            )
            gzipped, deleted_tr = sweep_transcript_retention(
                self._config.transcripts_dir,
                self._config.logging.transcripts.retention_days,
            )
            audit_event(
                "audit.housekeeping.swept",
                audit_dir=self._config.audit_dir,
                actor="anna",
                fsync_on_write=self._config.logging.audit.fsync_on_write,
                audit_files_deleted=deleted_audit,
                transcripts_gzipped=gzipped,
                transcripts_deleted=deleted_tr,
            )
        except Exception as exc:
            self._log.error("housekeeping.sweep.failed", error=str(exc))

    async def _sleep_until_next_sweep(self) -> None:
        hhmm = self._config.housekeeping.daily_sweep_time
        hour, minute = (int(x) for x in hhmm.split(":"))
        now = datetime.now(timezone.utc).astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=now.day) if False else target
            # Move to tomorrow if today's slot already passed.
            target = target.replace(year=now.year, month=now.month, day=now.day)
            if target <= now:
                # safe addition using timedelta
                from datetime import timedelta
                target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        await asyncio.sleep(max(delay, 60))
