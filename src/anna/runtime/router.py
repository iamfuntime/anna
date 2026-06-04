"""Conversation router.

Per v3 section 6. Takes a normalized :class:`InboundEvent` from any transport,
derives the conversation_key, looks up or spawns the per-conversation worker,
and hands the event to the worker's queue.

The router also writes the inbound and outbound transcript lines. It is the
only component with both sides of the conversation in scope.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from typing import Awaitable, Callable

from anna.config import AnnaConfig, IdentityAliasEntry
from anna.core.identity import CoreFile, read_core_file
from anna.log import (
    audit_event,
    get_logger,
    sweep_audit_retention,
    sweep_transcript_retention,
    sweep_voice_retention,
    transcript_event,
)
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.subagent import SubAgentRunner
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import (
    CadenceLinter,
    VisibilityCallbacks,
    _noop_clear,
    _noop_start,
)
from anna.runtime.worker import ConversationWorker
from anna.tools.google_clients import GoogleClients
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


SendCallback = Callable[[OutboundMessage], Awaitable[None]]


def _identifier_from_event(event: InboundEvent) -> str | None:
    """Extract the per-transport identifier the alias config keys on.

    Returns ``None`` for conv_key shapes that don't carry an operator
    identity (Slack channel threads, Telegram groups, CLI oneshot, future
    transports). The three shapes that *do* carry an identity are::

        slack:dm:<user_id>
        telegram:dm:<chat_id>
        cli:local:<username>

    Per Phase 2 §5 plan ("Identity aliasing"). The CLI oneshot shape
    ``cli:oneshot:<uuid>`` is deliberately excluded — one-shot turns
    must never alias to the operator's canonical conv.
    """
    parts = event.conversation_key.split(":")
    if len(parts) < 3:
        return None
    transport = event.transport
    if transport == "slack" and parts[1] == "dm":
        return parts[2]
    if transport == "telegram" and parts[1] == "dm":
        return parts[2]
    if transport == "cli" and parts[1] == "local":
        return parts[2]
    return None


def _build_identity_index(
    identities: list[IdentityAliasEntry],
) -> dict[str, tuple[IdentityAliasEntry, ...]]:
    """Group identity aliases by transport for O(small_n) dispatch lookups.

    An entry contributes to a transport's bucket iff the matching field
    is populated; an entry with only ``slack_user_id`` set never appears
    in the ``telegram`` or ``cli`` bucket, so a Telegram event sharing
    the same numeric identifier cannot accidentally alias to it.
    """
    buckets: dict[str, list[IdentityAliasEntry]] = {
        "slack": [],
        "telegram": [],
        "cli": [],
    }
    for entry in identities:
        if entry.slack_user_id is not None:
            buckets["slack"].append(entry)
        if entry.telegram_chat_id is not None:
            buckets["telegram"].append(entry)
        if entry.cli_username is not None:
            buckets["cli"].append(entry)
    return {transport: tuple(entries) for transport, entries in buckets.items()}


class ConversationRouter:
    def __init__(
        self,
        *,
        config: AnnaConfig,
        supervisor: Supervisor,
        adapters: dict[str, ChannelAdapter],
        schedule_store: ScheduleStore | None = None,
        google_clients: GoogleClients | None = None,
        subagent_runner: SubAgentRunner | None = None,
    ) -> None:
        self._config = config
        self._supervisor = supervisor
        self._adapters = adapters
        self._schedule_store = schedule_store
        self._google_clients = google_clients
        self._subagent_runner = subagent_runner
        self._log = get_logger("anna.router")
        self._workers: dict[str, ConversationWorker] = {}
        self._workers_lock = asyncio.Lock()
        # Phase 2 §5 identity aliasing: precompute a per-transport view of
        # the alias entries so dispatch stays O(small_n). See the
        # _identifier_from_event / _normalize_conv_key helpers below.
        self._identity_index: dict[str, tuple[IdentityAliasEntry, ...]] = (
            _build_identity_index(config.identities)
        )
        # Cadence-Visibility Hooks plan (Inbox/2026-06-02) subtask 7:
        # instantiate the cadence linter once at router construction and
        # share the same instance across every worker. The linter
        # compiles its patterns at __init__ time, so a malformed config
        # fails fast at boot rather than per-spawn. When
        # ``runtime.visibility.response_lint`` is false the linter is
        # ``None`` and the worker's lint pass short-circuits.
        if self._config.runtime.visibility.response_lint:
            self._cadence_linter: CadenceLinter | None = CadenceLinter(
                config=self._config,
            )
        else:
            self._cadence_linter = None

        # Background-delegation completion delivery. The runner fires a
        # detached sub-agent and, on completion, needs to route the result
        # back into the originating conversation as a NEW inbound turn so
        # ANNA is re-invoked to read and act on it. The runner cannot be
        # handed the router in its constructor (it is built first, and a
        # back-reference would be an import cycle), so we install the
        # delivery callback here once both objects exist.
        if self._subagent_runner is not None:
            self._subagent_runner.set_delivery(
                self.deliver_background_completion
            )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, event: InboundEvent) -> None:
        """Subscribe target. Called by every ChannelAdapter for inbound events."""
        # Phase 2 §5 identity aliasing: rewrite the conv_key before any
        # downstream consumer (transcript writer, worker registry) sees
        # it. Non-matched events flow through unchanged.
        new_key = self._normalize_conv_key(event)
        if new_key != event.conversation_key:
            self._log.debug(
                "router.identity_alias_applied",
                original_key=event.conversation_key,
                new_key=new_key,
                transport=event.transport,
                canonical=new_key.split(":", 1)[1] if ":" in new_key else new_key,
            )
            event = dataclasses.replace(event, conversation_key=new_key)

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
            image_count=len(event.images),
        )
        await worker.submit(event)

    async def deliver_background_completion(
        self,
        transport: str,
        conv_key: str,
        text: str,
    ) -> None:
        """Inject a finished background delegation as a new inbound turn.

        Installed on the :class:`SubAgentRunner` at construction. When a
        detached delegation completes, the runner calls this with the
        originating conversation's transport + conv_key and the formatted
        completion text (sub-agent reply + YAML trailer).

        Reuses the same vehicle the scheduler uses to inject a turn: a
        synthetic :class:`InboundEvent` dispatched through
        :meth:`dispatch`. The crucial difference from the scheduler is
        that we DO NOT attach a ``completion_future`` — so the worker runs
        the turn through its normal interactive path, ANNA reads the
        result, and her reply flushes to the originating conversation via
        the standard send callback. The conv_key is preserved verbatim so
        the completion lands in the conversation that started the
        delegation (the identity-aliasing rewrite in :meth:`dispatch` is a
        no-op for an already-canonical key).
        """
        event = InboundEvent(
            transport=transport,
            conversation_key=conv_key,
            sender_id="anna.subagent",
            sender_display="ANNA Sub-agent",
            text=text,
            is_dm=False,
            is_thread=False,
            raw={"background_delegation": True},
        )
        await self.dispatch(event)

    def _normalize_conv_key(self, event: InboundEvent) -> str:
        """Phase 2 §5 identity aliasing.

        If the event's per-transport identifier matches a configured
        ``IdentityAliasEntry``, return ``user:<canonical>`` so the worker
        registry, checkpoints, and resume context all collapse onto one
        per-operator conv across transports. Otherwise return the
        event's existing conversation_key unchanged.

        The lookup is transport-scoped: an entry that only populates
        ``slack_user_id`` never matches a Telegram event with the same
        numeric identifier.
        """
        ident = _identifier_from_event(event)
        if ident is None:
            return event.conversation_key
        for entry in self._identity_index.get(event.transport, ()):
            if event.transport == "slack" and entry.slack_user_id == ident:
                return f"user:{entry.canonical}"
            if event.transport == "telegram" and entry.telegram_chat_id == ident:
                return f"user:{entry.canonical}"
            if event.transport == "cli" and entry.cli_username == ident:
                return f"user:{entry.canonical}"
        return event.conversation_key

    async def _get_or_spawn_worker(self, event: InboundEvent) -> ConversationWorker:
        key = event.conversation_key
        async with self._workers_lock:
            worker = self._workers.get(key)
            if worker is not None:
                return worker
            # Phase 2 §5 subtask 7: propagate ``event.ephemeral`` to the
            # worker on the FIRST event for a given conv_key. Subsequent
            # events on the same conv_key reuse this worker, which
            # already carries the ephemeral flag. CLI one-shot sessions
            # (``cli:oneshot:<uuid>``) get a fresh conv_key per
            # invocation, so each spawn picks up its own flag and the
            # closeout skips the checkpoint write.
            worker = ConversationWorker(
                conversation_key=key,
                transport=event.transport,
                config=self._config,
                supervisor=self._supervisor,
                send=self._send_factory(event.transport),
                on_idle_close=self._idle_close_callback,
                adapters=self._adapters,
                schedule_store=self._schedule_store,
                google_clients=self._google_clients,
                subagent_runner=self._subagent_runner,
                ephemeral=event.ephemeral,
                visibility=self._build_visibility_callbacks(event.transport),
            )
            await worker.start()
            self._workers[key] = worker
            self._log.info(
                "conversation.start",
                channel=event.transport,
                conv_key=key,
                ephemeral=event.ephemeral,
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

    def _build_visibility_callbacks(self, transport: str) -> VisibilityCallbacks:
        """Build the per-transport :class:`VisibilityCallbacks` bundle.

        Cadence-Visibility Hooks plan (Inbox/2026-06-02) subtask 7. Each
        newly-spawned worker is handed a bundle that wires:

        * ``start`` / ``clear`` to the matching ``ChannelAdapter`` methods
          (so the worker stays decoupled from the adapter registry and
          the adapter owns its per-transport API state — Slack client,
          Telegram bot, CLI session map). When
          ``runtime.visibility.reaction_signal`` is false, both fall back
          to the module-level no-op callables so the worker's hook path
          becomes a cheap nothing.
        * ``cadence_reminder_loader`` to a zero-arg closure that reads
          ``core/CADENCE.md`` via :func:`read_core_file` on every call,
          for buffered transports only (Slack, Telegram). CLI sees deltas
          live so a reminder is not needed there; the loader is ``None``
          for any other transport. ``None`` also flows when the
          ``cadence_reminder`` config flag is disabled.
        * ``lint`` to the shared :class:`CadenceLinter` instantiated at
          router construction, or ``None`` when ``response_lint`` is
          disabled.

        The adapter lookup mirrors :meth:`_send_factory`. Missing-adapter
        cases (e.g. a transport with no registered adapter) raise the
        same ``KeyError`` ``_send_factory`` would; the router should
        never spawn a worker for an unregistered transport.
        """
        visibility_cfg = self._config.runtime.visibility

        if visibility_cfg.reaction_signal:
            adapter = self._adapters[transport]
            # Bound methods on the adapter instance — capturing them
            # here preserves ``self`` so the worker can call them
            # without re-threading the adapter reference.
            start = adapter.start_thinking_signal
            clear = adapter.clear_thinking_signal
        else:
            start = _noop_start
            clear = _noop_clear

        cadence_reminder_loader: Callable[[], str] | None
        if (
            visibility_cfg.cadence_reminder
            and transport in ("slack", "telegram")
        ):
            core_dir = self._config.core_dir

            def _load_cadence_reminder() -> str:
                return read_core_file(core_dir, CoreFile.CADENCE).strip()

            cadence_reminder_loader = _load_cadence_reminder
        else:
            cadence_reminder_loader = None

        return VisibilityCallbacks(
            start=start,
            clear=clear,
            lint=self._cadence_linter,
            cadence_reminder_loader=cadence_reminder_loader,
        )

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

        In-flight background delegations are drained first: a detached
        sub-agent that is mid-run when SIGTERM lands would otherwise be
        orphaned. :meth:`SubAgentRunner.drain_background_jobs` awaits each
        job (bounded) so it can deliver its completion turn — or be
        cancelled cleanly rather than left dangling on the torn-down loop.

        A completion that lands during the drain is best-effort and may
        not be acted on: ``worker.stop()`` cancels the run-loop without
        draining the queue, and ``_closeout`` only checkpoints + evicts —
        it does not process queued inbound events. So a delivery that
        arrives after its worker's run-loop is already cancelled will sit
        unprocessed. Nothing is lost from the record, though: the
        transcript line and the ``background_complete`` audit still fire.
        """
        if self._subagent_runner is not None:
            try:
                await self._subagent_runner.drain_background_jobs()
            except Exception as exc:
                self._log.error(
                    "router.shutdown.background_drain_failed",
                    error=str(exc),
                )

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
            voice_deleted = sweep_voice_retention(
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
                voice_files_deleted=voice_deleted,
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
