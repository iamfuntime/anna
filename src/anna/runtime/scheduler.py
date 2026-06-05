"""Phase 2 scheduler.

Polls :class:`ScheduleStore` for due schedules at every
``poll_interval_seconds`` and dispatches each as a synthetic
:class:`InboundEvent` through :class:`ConversationRouter`. The event
carries a ``completion_future`` so the worker resolves it with the
final assistant message instead of calling its normal outbound send
path. The scheduler then routes the message to the schedule's declared
destination via the appropriate :class:`ChannelAdapter` directly.

Failure handling is three-strikes-then-disable per
``config.scheduler.failure_threshold``. Each fire is timeboxed by
``schedule.timeout_seconds``. Timeouts and exceptions both count as
failures, are persisted via ``ScheduleStore.mark_failed``, and trip the
auto-disable + :meth:`anna.runtime.alerter.AdminAlerter.warn` path at
threshold.

See ``Inbox/2026-06-01-ANNA-Phase-2-Scheduler-Buildout-Plan.md`` for
the full design.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage

if TYPE_CHECKING:
    from anna.runtime.alerter import AdminAlerter
    from anna.runtime.router import ConversationRouter


# Sentinel a scheduled skill can return verbatim as its final reply to opt out
# of posting. When ``fire()`` produces exactly this string (after stripping),
# the scheduler records the run as a normal success (updates last_fired_at,
# resets consecutive_failures, marks complete) but sends NOTHING to the
# destination transport. This enables a "quiet by default" heartbeat schedule.
# The worker coerces empty/whitespace replies to "(no response)" before
# resolving the completion future, so the sentinel must be a non-empty literal
# the skill returns exactly.
QUIET_SENTINEL = "[[ANNA_NO_OUTPUT]]"

__all__ = ["QUIET_SENTINEL", "Scheduler"]


class Scheduler:
    """Owns the scheduled-fire loop."""

    def __init__(
        self,
        *,
        config: AnnaConfig,
        store: ScheduleStore,
        router: "ConversationRouter",
        adapters: dict[str, ChannelAdapter],
        alerter: "AdminAlerter",
    ) -> None:
        self._config = config
        self._store = store
        self._router = router
        self._adapters = adapters
        self._alerter = alerter
        self._log = get_logger("anna.scheduler")
        self._audit_dir: Path = config.audit_dir
        self._fsync: bool = config.logging.audit.fsync_on_write
        self._semaphore = asyncio.Semaphore(config.scheduler.max_concurrent)
        self._inflight: set[asyncio.Task[None]] = set()
        self._shutdown_grace_seconds: float = 5.0

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop. Polls the store and dispatches due schedules.

        Exits cleanly on cancellation. Loop-level exceptions are caught,
        audited, and logged so one bad cycle does not crash the
        scheduler.
        """
        audit_event(
            "audit.scheduler.start",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            poll_interval_seconds=self._config.scheduler.poll_interval_seconds,
            max_concurrent=self._config.scheduler.max_concurrent,
        )
        self._log.info(
            "scheduler.start",
            poll_interval_seconds=self._config.scheduler.poll_interval_seconds,
        )
        try:
            while True:
                try:
                    await asyncio.sleep(self._config.scheduler.poll_interval_seconds)
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log.error("scheduler.loop_error", error=str(exc))
                    audit_event(
                        "audit.scheduler.loop_error",
                        audit_dir=self._audit_dir,
                        actor="anna",
                        fsync_on_write=self._fsync,
                        level="WARNING",
                        error=str(exc),
                    )
        except asyncio.CancelledError:
            self._log.info("scheduler.cancelled")
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        """Wait briefly for in-flight fires to land, then drop them."""
        if not self._inflight:
            return
        self._log.info("scheduler.shutdown.waiting", inflight=len(self._inflight))
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._inflight, return_exceptions=True),
                timeout=self._shutdown_grace_seconds,
            )
        except asyncio.TimeoutError:
            self._log.warning(
                "scheduler.shutdown.timeout",
                inflight=len(self._inflight),
            )
            for task in list(self._inflight):
                task.cancel()
        self._inflight.clear()

    # ------------------------------------------------------------------
    # Poll + dispatch
    # ------------------------------------------------------------------

    async def _poll_once(self) -> None:
        now = datetime.now(timezone.utc)
        due = self._store.due_schedules(now)
        for schedule in due:
            # Optimistically mark the schedule as dispatched BEFORE creating
            # the fire task. due_schedules() computes the next cron tick from
            # state.last_fired_at; without this, a long-running fire (>poll
            # interval) leaves last_fired_at null and every poll re-dispatches
            # the same schedule. See the 2026-06-01 morning-brief-test
            # incident: cron `2 15 * * *` produced 4 concurrent fires because
            # the 153-243s run kept the schedule "due" across 5 polls.
            await self._store.mark_dispatched(schedule.id, when=now)
            task = asyncio.create_task(
                self._guarded_fire(schedule),
                name=f"scheduler.fire.{schedule.id}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def fire(self, schedule_id: str) -> str:
        """Fire one schedule by id. Returns the reply text the worker produced.

        Caller is responsible for routing the returned text to the
        schedule's destination. :meth:`_guarded_fire` is the normal
        entrypoint; ``fire`` is exposed for tests and for a future
        ``fire_now`` operator command.
        """
        schedule = self._store.get(schedule_id)
        if schedule is None:
            raise ValueError(f"schedule '{schedule_id}' does not exist")
        conv_key = f"schedule:{schedule.id}:{date.today().isoformat()}"
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[str] = loop.create_future()

        event = InboundEvent(
            transport=schedule.destination.transport,
            conversation_key=conv_key,
            sender_id="anna.scheduler",
            sender_display="ANNA Scheduler",
            text=schedule.prompt,
            is_dm=False,
            is_thread=False,
            raw={"schedule_id": schedule.id},
            completion_future=completion,
            ephemeral=schedule.ephemeral,
        )

        await self._router.dispatch(event)
        return await completion

    # ------------------------------------------------------------------
    # Guarded fire with timeout + failure handling
    # ------------------------------------------------------------------

    async def _guarded_fire(self, schedule: Schedule) -> None:
        async with self._semaphore:
            started_at = datetime.now(timezone.utc)
            audit_event(
                "audit.schedule.fire",
                audit_dir=self._audit_dir,
                actor="anna",
                fsync_on_write=self._fsync,
                schedule_id=schedule.id,
                fire_at=started_at.isoformat(),
            )
            self._log.info("scheduler.fire", schedule_id=schedule.id)

            try:
                reply = await asyncio.wait_for(
                    self.fire(schedule.id),
                    timeout=schedule.timeout_seconds,
                )
            except asyncio.TimeoutError:
                await self._handle_failure(
                    schedule, reason=f"timeout after {schedule.timeout_seconds}s", kind="timeout"
                )
                return
            except Exception as exc:
                await self._handle_failure(schedule, reason=f"error: {exc}", kind="fail")
                return

            # Quiet sentinel: the skill opted out of posting. Record the run as
            # a normal success (identical bookkeeping minus the actual send) so
            # last_fired_at advances and consecutive_failures resets. Do NOT
            # count this as a failure.
            if reply is not None and reply.strip() == QUIET_SENTINEL:
                self._log.info(
                    "scheduler.quiet_suppress",
                    schedule_id=schedule.id,
                )
                self._log.info(
                    f"schedule {schedule.id} returned quiet sentinel — "
                    f"suppressing post"
                )
                await self._record_success(
                    schedule, reply, started_at=started_at, suppressed=True
                )
                return

            # Route the reply to the destination.
            try:
                await self._send_to_destination(schedule, reply)
            except Exception as exc:
                await self._handle_failure(
                    schedule, reason=f"send failed: {exc}", kind="fail"
                )
                return

            await self._record_success(schedule, reply, started_at=started_at)

    async def _record_success(
        self,
        schedule: Schedule,
        reply: str,
        *,
        started_at: datetime,
        suppressed: bool = False,
    ) -> None:
        """Record a successful fire: mark_fired (resets failures, sets
        last_fired_at + last_status="complete"), audit, and log.

        Shared by the normal-send path and the quiet-sentinel path so both
        use identical bookkeeping. ``suppressed`` only affects telemetry; the
        persisted success state is the same in both cases.
        """
        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()
        await self._store.mark_fired(
            schedule.id, status="complete", when=completed_at
        )
        audit_event(
            "audit.schedule.complete",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule.id,
            duration_seconds=duration,
            output_length=len(reply),
            suppressed=suppressed,
        )
        self._log.info(
            "scheduler.complete",
            schedule_id=schedule.id,
            duration_seconds=duration,
            output_length=len(reply),
            suppressed=suppressed,
        )

    async def _send_to_destination(self, schedule: Schedule, text: str) -> None:
        """Push the worker's reply to the schedule's declared destination."""
        adapter = self._adapters.get(schedule.destination.transport)
        if adapter is None:
            raise RuntimeError(
                f"no adapter registered for transport '{schedule.destination.transport}'"
            )
        dest_conv_key = self._dest_conv_key(
            transport=schedule.destination.transport,
            channel=schedule.destination.channel,
        )
        await adapter.send(OutboundMessage(conversation_key=dest_conv_key, text=text))

    @staticmethod
    def _dest_conv_key(*, transport: str, channel: str) -> str:
        """Synthesize a conv_key the adapter's send path will decode.

        Mirrors the AdminAlerter convention: both Slack channel posts
        and Telegram chat sends use the ``<transport>:dm:<id>`` form,
        which both adapters understand as a one-shot post to the
        channel or chat.
        """
        return f"{transport}:dm:{channel}"

    async def _handle_failure(
        self,
        schedule: Schedule,
        *,
        reason: str,
        kind: str,
    ) -> None:
        when = datetime.now(timezone.utc)
        new_count = await self._store.mark_failed(
            schedule.id, reason=reason, when=when, kind=kind
        )
        audit_event(
            "audit.schedule.fail",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            level="WARNING",
            schedule_id=schedule.id,
            reason=reason,
            kind=kind,
            consecutive_failures=new_count,
        )
        self._log.warning(
            "scheduler.fire_failed",
            schedule_id=schedule.id,
            reason=reason,
            kind=kind,
            consecutive_failures=new_count,
        )
        if new_count >= self._config.scheduler.failure_threshold:
            await self._store.mark_disabled(schedule.id, reason=reason)
            # AdminAlerter.warn is best-effort; do not block the loop on it.
            asyncio.create_task(
                self._alerter.warn(
                    f"Schedule '{schedule.id}' disabled after {new_count} "
                    f"consecutive failures. Last reason: {reason}"
                ),
                name=f"scheduler.alert.{schedule.id}",
            )
