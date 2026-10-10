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
import os
import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.runtime.heartbeat import Classifier, HeartbeatJob
from anna.runtime.ledger import OutboxItem, Recovery, RuntimeLedger, now_utc
from anna.runtime.schedule_jobs import JobResult, Outcome
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule
from anna.runtime.worker import (
    _contains_unparsed_toolcall_markup,
    _matched_markers,
)
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


def _stub_schedule(schedule_id: str) -> Schedule:
    """Placeholder for a schedule that no longer exists (failure bookkeeping is then a no-op)."""
    from types import SimpleNamespace
    return SimpleNamespace(id=schedule_id)  # type: ignore[return-value]


class NoAdapterError(RuntimeError):
    """The destination transport is not connected; nothing was sent."""

    retry_safe = True


class _TurnTimeout(Exception):
    """The worker turn exceeded its timeout; carries the cancellation verdict."""

    def __init__(self, status: str, side_effects: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.side_effects, self.detail = status, side_effects, detail


def classify_send_error(exc: BaseException) -> bool:
    """True only when the failure proves the message was NOT delivered (safe to resend)."""
    if getattr(exc, "retry_safe", False) is True:
        return True
    if isinstance(exc, ConnectionError):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429


def _backup_yaml_before_first_ledger(config: AnnaConfig) -> None:
    """One-time copy of schedules.yaml the first time the ledger is created."""
    yaml_path = config.scheduler.resolved_state_path
    ledger_path = config.state_dir / "runtime.sqlite3"
    if ledger_path.exists() or not yaml_path.exists():
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copy2(yaml_path, yaml_path.with_name(f"{yaml_path.name}.bak-pre-ledger-{stamp}"))


class Scheduler:
    """Owns the scheduled-fire loop."""

    def __init__(
        self,
        *,
        config: AnnaConfig,
        store: ScheduleStore,
        router: ConversationRouter,
        adapters: dict[str, ChannelAdapter],
        alerter: AdminAlerter,
        ledger: RuntimeLedger | None = None,
        google_clients: Any | None = None,
        heartbeat_classifier: Classifier | None = None,
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
        # Parallel index of _inflight keyed by schedule id so deletion can
        # cancel exactly the tasks belonging to one schedule. Entries are
        # removed by the task done-callback; a key vanishes when its set
        # empties.
        self._tasks_by_schedule: dict[str, set[asyncio.Task[None]]] = {}
        if ledger is None:
            _backup_yaml_before_first_ledger(config)
            ledger = RuntimeLedger(config.state_dir / "runtime.sqlite3")
        self._ledger = ledger
        self._google_clients = google_clients
        self._heartbeat_classifier = heartbeat_classifier
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._spawned: set[int] = set()
        self._shutdown_grace_seconds: float = 5.0
        self._cancel_grace_seconds: float = config.scheduler.cancel_grace_seconds + 1.0

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
        except TimeoutError:
            self._log.warning(
                "scheduler.shutdown.timeout",
                inflight=len(self._inflight),
            )
            for task in list(self._inflight):
                task.cancel()
        self._inflight.clear()
        self._tasks_by_schedule.clear()

    async def cancel_schedule_tasks(self, schedule_id: str) -> int:
        """Cancel every queued or in-flight fire task for ``schedule_id``.

        Invoked when a schedule is deleted (wired via
        :meth:`ScheduleStore.set_cancel_callback`) so tasks parked on the
        ``max_concurrent`` semaphore do not later acquire a slot, call
        :meth:`fire` on a schedule that no longer exists, and emit a noisy
        ``audit.schedule.fail`` with "does not exist" — the 2026-06-01
        morning-brief-test incident tail.

        DECISION: in-flight runs are cancelled too, not only queued ones.
        The operator chose to remove the schedule; output from a run they
        no longer want is at best noise at the destination and at worst an
        action they intended to stop. A run worth keeping can always be
        re-issued manually. Cancellation lands inside
        ``_guarded_fire``'s ``await`` points as ``CancelledError``, which
        is deliberately NOT caught by its failure handling, so a cancelled
        fire records neither a completion nor a failure.

        Waits up to ``_cancel_grace_seconds`` for the cancelled tasks to
        land; stragglers are logged and left to their done-callbacks for
        index cleanup. Returns the number of tasks cancelled.
        """
        # Durable state first: unrun occurrences become cancelled and unsent
        # output is dropped, so nothing queued can start or deliver afterwards.
        self._ledger.cancel_schedule(schedule_id, detail="schedule deleted")
        tasks = [
            task
            for task in self._tasks_by_schedule.get(schedule_id, ())
            if not task.done()
        ]
        if not tasks:
            return 0
        for task in tasks:
            task.cancel()
        _done, pending = await asyncio.wait(
            tasks, timeout=self._cancel_grace_seconds
        )
        if pending:
            self._log.warning(
                "scheduler.cancel.timeout",
                schedule_id=schedule_id,
                still_pending=len(pending),
            )
        audit_event(
            "audit.schedule.cancelled",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule_id,
            cancelled=len(tasks),
            still_pending=len(pending),
        )
        self._log.info(
            "scheduler.tasks_cancelled",
            schedule_id=schedule_id,
            cancelled=len(tasks),
        )
        return len(tasks)

    # ------------------------------------------------------------------
    # Poll + dispatch
    # ------------------------------------------------------------------

    async def _poll_once(self) -> None:
        """One scheduler cycle: recover, reconcile, materialize, dispatch, deliver.

        Nothing is marked "fired" optimistically.  A tick becomes a unique
        ``(schedule_id, scheduled_for)`` occurrence row in the ledger in one
        transaction with the cursor advance, so a crash can neither lose the
        tick nor produce it twice.
        """
        now = datetime.now(UTC)
        cfg = self._config.scheduler
        try:
            await self._store.load()  # pick up dashboard edits and other writers
        except Exception as exc:
            self._log.warning("scheduler.reload_failed", error=str(exc))
        for done in self._ledger.apply_repairs():
            audit_event("audit.schedule.repair_applied", audit_dir=self._audit_dir, actor="operator",
                        fsync_on_write=self._fsync,
                        level="INFO" if done["outcome"] == "applied" else "WARNING", repair_id=done["id"],
                        kind=done["kind"], target_id=done["target_id"], action=done["action"],
                        outcome=done["outcome"], detail=done["detail"], requested_by=done["requested_by"])
        for rec in self._ledger.recover_expired(now=now, max_run_attempts=cfg.max_run_attempts):
            await self._on_recovery(rec)
        self._ledger.recover_outbox(now=now)
        for sid in self._ledger.active_schedule_ids():
            if self._store.get(sid) is None:  # deleted by another process (dashboard)
                await self.cancel_schedule_tasks(sid)
        for schedule in self._store.list():
            if schedule.enabled:
                self._materialize(schedule, now)
        for occ in self._ledger.pending():
            if occ.id in self._spawned:
                continue
            found = self._store.get(occ.schedule_id)
            if found is None or not found.enabled:
                self._ledger.end_unrun(occ.id, status="cancelled", detail="schedule missing or disabled")
                continue
            self._spawn(found, occ.id)
        await self._deliver_due()

    def _materialize(self, schedule: Schedule, now: datetime) -> None:
        cfg = self._config.scheduler
        try:
            tz = ZoneInfo(schedule.timezone)
            baseline = schedule.state.last_fired_at or schedule.created_at
            if baseline.tzinfo is None:
                baseline = baseline.replace(tzinfo=UTC)
            created = self._ledger.materialize(
                schedule_id=schedule.id, cron=schedule.cron, tz=tz, baseline=baseline, now=now,
                job_kind=schedule.job_kind, run_safety=schedule.run_safety,
                overlap=schedule.overlap_policy or cfg.default_overlap_policy,
                misfire=schedule.misfire_policy or cfg.default_misfire_policy,
                grace_seconds=schedule.misfire_grace_seconds or cfg.misfire_grace_seconds,
            )
        except Exception as exc:
            self._log.warning("scheduler.materialize_failed", schedule_id=schedule.id, error=str(exc))
            return
        for occ_id in created:
            audit_event("audit.schedule.occurrence", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, schedule_id=schedule.id, occurrence_id=occ_id)

    def _spawn(self, schedule: Schedule, occurrence_id: int) -> asyncio.Task[None]:
        self._spawned.add(occurrence_id)
        task = asyncio.create_task(
            self._guarded_fire(schedule, occurrence_id=occurrence_id),
            name=f"scheduler.fire.{schedule.id}",
        )
        self._inflight.add(task)
        self._tasks_by_schedule.setdefault(schedule.id, set()).add(task)
        sid = schedule.id

        def _done(t: asyncio.Task[None]) -> None:
            self._task_done(sid, occurrence_id, t)

        task.add_done_callback(_done)
        return task

    def _task_done(self, schedule_id: str, occurrence_id: int, task: asyncio.Task[None]) -> None:
        self._spawned.discard(occurrence_id)
        self._discard_task(schedule_id, task)

    async def _on_recovery(self, rec: Recovery) -> None:
        level = "WARNING" if rec.action in ("unknown", "failed") else "INFO"
        audit_event("audit.schedule.recovered", audit_dir=self._audit_dir, actor="anna",
                    fsync_on_write=self._fsync, level=level, schedule_id=rec.schedule_id,
                    occurrence_id=rec.occurrence_id, action=rec.action, detail=rec.detail)
        self._log.warning("scheduler.recovered", schedule_id=rec.schedule_id,
                          occurrence_id=rec.occurrence_id, action=rec.action)
        if rec.action in ("unknown", "failed"):
            await self._handle_failure(
                self._store.get(rec.schedule_id) or _stub_schedule(rec.schedule_id),
                reason=rec.detail, kind="unknown" if rec.action == "unknown" else "fail")

    def _discard_task(self, schedule_id: str, task: asyncio.Task[None]) -> None:
        """Done-callback: drop a finished fire task from both indexes."""
        self._inflight.discard(task)
        tasks = self._tasks_by_schedule.get(schedule_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                del self._tasks_by_schedule[schedule_id]

    async def fire(
        self,
        schedule_id: str,
        *,
        turn_meta: dict[str, object] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> str:
        """Fire one schedule by id. Returns the reply text the worker produced.

        Caller is responsible for routing the returned text to the
        schedule's destination. :meth:`_guarded_fire` is the normal
        entrypoint; ``fire`` is exposed for tests and for a future
        ``fire_now`` operator command.

        ``turn_meta``, when passed, is handed to the worker on the event and
        comes back populated with per-turn telemetry (``tool_call_count``).
        :meth:`_guarded_fire` uses it for the no-tool-call backstop; callers
        that do not care leave it ``None``.
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
            turn_meta=turn_meta,
            cancel=cancel,
        )

        await self._router.dispatch(event)
        return await completion

    # ------------------------------------------------------------------
    # Guarded fire with timeout + failure handling
    # ------------------------------------------------------------------

    async def _guarded_fire(self, schedule: Schedule, *, occurrence_id: int | None = None) -> None:
        """Claim, run and settle one occurrence.

        Execution is recorded in the ledger (claim -> run -> typed result) and
        delivery is a separate outbox step.  ``occurrence_id=None`` creates an
        ad-hoc occurrence (tests / a future ``fire_now``).
        """
        if occurrence_id is None:
            occurrence_id = self._ledger.create_manual(
                schedule_id=schedule.id, job_kind=schedule.job_kind, run_safety=schedule.run_safety)
        async with self._semaphore:
            lease = 2 * schedule.timeout_seconds + self._config.scheduler.lease_grace_seconds
            if not self._ledger.claim(occurrence_id, owner=self._owner, lease_seconds=lease):
                return
            started_at = datetime.now(UTC)
            try:
                run_id = self._ledger.start_run(occurrence_id, owner=self._owner)
            except PermissionError:
                return
            audit_event(
                "audit.schedule.fire",
                audit_dir=self._audit_dir,
                actor="anna",
                fsync_on_write=self._fsync,
                schedule_id=schedule.id,
                occurrence_id=occurrence_id,
                fire_at=started_at.isoformat(),
            )
            self._log.info("scheduler.fire", schedule_id=schedule.id, occurrence_id=occurrence_id)

            if schedule.job_kind == "google_heartbeat":
                await self._run_heartbeat(schedule, occurrence_id, run_id)
                await self._deliver_due()
                return
            meta: dict[str, object] = {}
            try:
                result = await self._run_job(schedule, meta)
            except asyncio.CancelledError:
                verdict = meta.get("_cancel_verdict")
                status, effects, detail = verdict if isinstance(verdict, tuple) else (
                    "unknown", "unknown", "scheduler task cancelled; outcome unverified")
                self._ledger.finish_run(occurrence_id, run_id, status=status, side_effects=effects,
                                        error=detail, detail=detail)
                raise
            except _TurnTimeout as exc:
                self._ledger.finish_run(occurrence_id, run_id, status=exc.status, side_effects=exc.side_effects,
                                        error=exc.detail, detail=exc.detail)
                await self._handle_failure(
                    schedule, reason=f"timeout after {schedule.timeout_seconds}s ({exc.status}: {exc.detail})",
                    kind="timeout")
                return
            except Exception as exc:
                self._ledger.finish_run(occurrence_id, run_id, status="failed", side_effects="unknown",
                                        error=f"{type(exc).__name__}: {exc}"[:300])
                await self._handle_failure(schedule, reason=f"error: {exc}", kind="fail")
                return

            if result.outcome == "failed":
                self._ledger.finish_run(occurrence_id, run_id, status="failed", side_effects=result.side_effects,
                                        result=result.to_json(), error=result.error, detail=result.error)
                await self._handle_failure(schedule, reason=result.error or "job failed",
                                           kind=str(result.data.get("kind", "fail")))
                return

            if result.outcome == "quiet":
                self._ledger.finish_run(occurrence_id, run_id, status="succeeded", side_effects=result.side_effects,
                                        result=result.to_json(), delivery_status="suppressed",
                                        detail=result.quiet_reason)
                await self._record_success(schedule, result.output, started_at=started_at, suppressed=True)
                return

            dest = schedule.destination
            written = self._ledger.finish_run(
                occurrence_id, run_id, status="succeeded", side_effects=result.side_effects,
                result=result.to_json(), outbox=[(dest.transport, dest.channel, result.output)],
                chunk_chars=self._config.scheduler.delivery_chunk_chars)
            if not written:
                return  # recovered/cancelled meanwhile: never enqueue a duplicate delivery
            await self._record_success(schedule, result.output, started_at=started_at)
        await self._deliver_due()

    # ------------------------------------------------------------------
    # Built-in read-only Gmail + Calendar heartbeat (no worker turn, no LLM)
    # ------------------------------------------------------------------

    async def _run_heartbeat(self, schedule: Schedule, occurrence_id: int, run_id: int) -> None:
        cfg = self._config
        finish = self._ledger.finish_run

        async def fail(status: str, error: str, kind: str = "fail") -> None:
            finish(occurrence_id, run_id, status=status, side_effects="none", error=error, detail=error)
            await self._handle_failure(schedule, reason=error, kind=kind)

        if not cfg.heartbeat.enabled:
            await fail("failed", "heartbeat is disabled (set heartbeat.enabled: true)")
            return
        dest = f"{schedule.destination.transport}:{schedule.destination.channel}"
        if dest not in cfg.heartbeat.allowed_destinations:
            await fail("failed", f"heartbeat destination {dest!r} is not in heartbeat.allowed_destinations")
            return
        occ = self._ledger.get(occurrence_id)
        job = HeartbeatJob(config=cfg, clients=self._google_clients, ledger=self._ledger,
                           classifier=self._heartbeat_classifier)
        try:
            outcome = await asyncio.wait_for(job.run(schedule, scheduled_for=occ.scheduled_for),
                                             timeout=schedule.timeout_seconds)
        except asyncio.CancelledError:
            finish(occurrence_id, run_id, status="cancelled", side_effects="none",
                   error="heartbeat cancelled (read-only; safe to repeat)", detail="cancelled")
            raise
        except TimeoutError:
            await fail("timeout", f"heartbeat timed out after {schedule.timeout_seconds}s", "timeout")
            return
        except Exception as exc:  # noqa: BLE001
            await fail("failed", f"heartbeat error: {type(exc).__name__}: {exc}"[:300])
            return
        now = datetime.now(UTC)
        specs = job.outbox_specs(schedule, outcome, now, ZoneInfo(schedule.timezone))
        exec_status = {"success": "succeeded"}.get(outcome.status, outcome.status)
        written = finish(
            occurrence_id, run_id, status=exec_status, side_effects="none", result=outcome.summary,
            error=None if outcome.status == "success" else outcome.status,
            outbox=specs or None, chunk_chars=cfg.scheduler.delivery_chunk_chars,
            detail=outcome.status, heartbeat=outcome.commit)
        if not written:
            return  # recovered / cancelled meanwhile: never double-apply
        audit_event(
            "audit.schedule.heartbeat", audit_dir=self._audit_dir, actor="anna", fsync_on_write=self._fsync,
            level="INFO" if outcome.status == "success" else "WARNING", schedule_id=schedule.id,
            occurrence_id=occurrence_id, status=outcome.status,
            checks=[{"account": c.account, "service": c.service, "status": c.status} for c in outcome.checks],
            notifications=len(specs))
        if outcome.status == "success":
            await self._record_success(schedule, "", started_at=now)
        else:
            # Recorded for the dashboard, but a heartbeat is never auto-disabled.
            await self._store.mark_failed(schedule.id, reason=outcome.status, when=now, kind="fail")

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def _run_job(self, schedule: Schedule, meta: dict[str, object]) -> JobResult:
        """Dispatch on ``job_kind``.  Deterministic typed jobs (heartbeat) plug in here."""
        if schedule.job_kind == "agent_prompt":
            return await self._run_agent_prompt(schedule, meta)
        raise RuntimeError(f"unknown job_kind {schedule.job_kind!r}")

    async def _turn(self, schedule: Schedule, meta: dict[str, object]) -> str:
        """Run one worker turn with timeout; on timeout/cancel, stop the worker turn.

        The cancel event travels on the inbound event: a queued turn is dropped
        without running, a running turn is interrupted.  The verdict says whether
        side effects can be ruled out (``timeout``), cannot (``unknown``), or the
        worker never acknowledged the stop (``blocked``).
        """
        cancel = asyncio.Event()
        task = asyncio.ensure_future(self.fire(schedule.id, turn_meta=meta, cancel=cancel))
        try:
            done, _ = await asyncio.wait({task}, timeout=schedule.timeout_seconds)
            if done:
                return task.result()
        except asyncio.CancelledError:
            meta["_cancel_verdict"] = await self._stop_turn(task, cancel, meta, cancelled_status="cancelled")
            raise
        verdict = await self._stop_turn(task, cancel, meta, cancelled_status="timeout")
        raise _TurnTimeout(*verdict)

    async def _stop_turn(self, task: asyncio.Future[str], cancel: asyncio.Event, meta: dict[str, object],
                         *, cancelled_status: str) -> tuple[str, str, str]:
        cancel.set()
        grace = self._config.scheduler.cancel_grace_seconds
        try:
            await asyncio.wait({task}, timeout=grace)
        except asyncio.CancelledError:
            pass
        if not task.done():
            task.cancel()
            return ("blocked", "unknown", f"worker turn did not acknowledge cancel within {grace}s")
        if task.cancelled():
            return ("blocked", "unknown", "turn future cancelled before the worker confirmed it stopped")
        state = meta.get("turn_state")
        tools = meta.get("tool_call_count")
        if state == "dropped":
            return (cancelled_status, "none", "queued turn dropped before it started")
        if state == "finished" and tools == 0:
            return (cancelled_status, "none", "turn interrupted; no tool calls had run")
        task.exception()  # consume
        return ("unknown", "possible", f"turn interrupted after {tools if tools is not None else 'unknown'} "
                "tool call(s); side effects cannot be verified")

    async def _run_agent_prompt(self, schedule: Schedule, meta: dict[str, object]) -> JobResult:
        """Legacy prompt job with its quiet / no-tool-call / markup heuristics."""
        reply = await self._turn(schedule, meta)
        # No-tool-call backstop (2026-07-29 incident: oem-slide-restock-watch
        # resolved in 2.5s with zero tool calls and the bare text "I'll read the
        # skill file first", which the scheduler posted to the operator's DM). A
        # scheduled run exists to DO something - a turn that executed no tool
        # produced an intent, not a result. Retry ONCE into the same session:
        # zero tools ran, so the aborted turn has no side effects and the re-run
        # carries no double-execution risk. AT MOST one retry.
        if self._is_no_tool_call_prose(reply, meta):
            first_reply = reply
            audit_event(
                "audit.schedule.no_tool_call_retry",
                audit_dir=self._audit_dir,
                actor="anna",
                fsync_on_write=self._fsync,
                level="WARNING",
                schedule_id=schedule.id,
                output_length=len(first_reply),
                preview=first_reply[:280],
            )
            self._log.warning("scheduler.no_tool_call_retry", schedule_id=schedule.id,
                              output_length=len(first_reply))
            meta.clear()
            reply = await self._turn(schedule, meta)
            if self._is_no_tool_call_prose(reply, meta):
                return self._no_tool_call_abort(schedule, first_reply=first_reply, retry_reply=reply)
        count = meta.get("tool_call_count")
        effects = "none" if count == 0 else ("possible" if isinstance(count, int) else "unknown")
        tools = count if isinstance(count, int) else None

        def result(outcome: Outcome, reason: str | None = None) -> JobResult:
            return JobResult(outcome=outcome, output=reply, quiet_reason=reason, tool_call_count=tools,
                             side_effects=effects)  # type: ignore[arg-type]

        # Quiet sentinel, matched per-LINE: a stray narration line before it must
        # not leak the sentinel or the narration to the operator.
        if self._is_quiet_sentinel(reply):
            self._log.info("scheduler.quiet_suppress", schedule_id=schedule.id)
            return result("quiet", "quiet_sentinel")
        # Blank-output guard (2026-07-02 incident: a single stray character flushed
        # to the operator's DM as a blank message).
        if self._is_blank_output(reply):
            self._log.warning("scheduler.blank_output_suppress", schedule_id=schedule.id, reply_len=len(reply))
            return result("quiet", "blank_output")
        # Defense-in-depth tool-call-markup guard.
        if _contains_unparsed_toolcall_markup(reply):
            self._log.warning("scheduler.toolcall_markup_suppress", schedule_id=schedule.id,
                              markers=_matched_markers(reply))
            return result("quiet", "toolcall_markup")
        return result("ok")

    def _no_tool_call_abort(self, schedule: Schedule, *, first_reply: str, retry_reply: str) -> JobResult:
        """Both attempts narrated instead of acting: suppress and fail.

        This audit row is the only record of what the model said - it carries
        both replies in full so the run stays reconstructable.
        """
        audit_event(
            "audit.schedule.no_tool_call_abort",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            level="WARNING",
            schedule_id=schedule.id,
            first_output=first_reply,
            retry_output=retry_reply,
        )
        self._log.warning("scheduler.no_tool_call_abort", schedule_id=schedule.id,
                          first_output_length=len(first_reply), retry_output_length=len(retry_reply))
        return JobResult(outcome="failed", error="no tool call on either attempt (output suppressed)",
                         side_effects="none", tool_call_count=0, data={"kind": "no_tool_call"})

    # ------------------------------------------------------------------
    # Delivery (outbox) - retries here never re-run execution
    # ------------------------------------------------------------------

    async def _deliver_due(self) -> None:
        cfg = self._config.scheduler
        attempted: set[int] = set()  # one attempt per row per pass; retries wait for the next poll
        for _ in range(50):  # chunk i+1 becomes claimable only after chunk i was sent
            items = self._ledger.claim_outbox(now=now_utc(), lease_seconds=cfg.send_timeout_seconds + 30,
                                              exclude=frozenset(attempted))
            if not items:
                return
            for item in items:
                attempted.add(item.id)
                await self._send_item(item)

    async def _send_item(self, item: OutboxItem) -> None:
        cfg = self._config.scheduler
        try:
            adapter = self._adapters.get(item.transport)
            if adapter is None:
                raise NoAdapterError(f"no adapter registered for transport '{item.transport}'")
            await asyncio.wait_for(
                adapter.send(OutboundMessage(
                    conversation_key=self._dest_conv_key(transport=item.transport, channel=item.channel),
                    text=item.text,
                    plain=self._ledger.get(item.occurrence_id).job_kind != "agent_prompt")),
                timeout=cfg.send_timeout_seconds)
        except asyncio.CancelledError:
            # Interrupted mid-send: delivery unknown. Never resend automatically.
            self._ledger.outbox_failed(item, error="cancelled mid-send", retryable=False, max_attempts=1,
                                       backoff_base=0, backoff_max=0)
            raise
        except Exception as exc:
            retryable = classify_send_error(exc) or (
                cfg.retry_unknown_send_errors and not isinstance(exc, asyncio.TimeoutError))
            outcome = self._ledger.outbox_failed(
                item, error=f"{type(exc).__name__}: {exc}", retryable=retryable,
                max_attempts=cfg.delivery_max_attempts, backoff_base=cfg.delivery_backoff_base_seconds,
                backoff_max=cfg.delivery_backoff_max_seconds)
            audit_event("audit.schedule.delivery_attempt_failed", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, level="WARNING", schedule_id=item.schedule_id,
                        occurrence_id=item.occurrence_id, chunk=item.chunk_index, attempts=item.attempts,
                        outcome=outcome, error=f"{type(exc).__name__}: {exc}"[:200])
            if outcome in ("failed", "uncertain"):
                await self._handle_failure(
                    self._store.get(item.schedule_id) or _stub_schedule(item.schedule_id),
                    reason=f"send failed ({outcome}): {exc}", kind="delivery")
            return
        self._ledger.outbox_sent(item)

    @staticmethod
    def _is_quiet_sentinel(reply: str) -> bool:
        """True when ANY line of ``reply`` is exactly the quiet sentinel."""
        return any(line.strip() == QUIET_SENTINEL for line in reply.splitlines())

    @staticmethod
    def _is_blank_output(reply: str) -> bool:
        """True when ``reply`` carries no alphanumeric character at all."""
        return not any(ch.isalnum() for ch in reply)

    @classmethod
    def _is_no_tool_call_prose(
        cls, reply: str | None, meta: dict[str, object]
    ) -> bool:
        """True when a scheduled turn produced postable prose without running
        a single tool — the shape the backstop retries and, on a repeat,
        fails.

        Gated on ``tool_call_count`` ALONE, never on duration: a legitimately
        fast run is fine, and slow narration is still narration. A count the
        worker did not report (key absent — e.g. an event that never reached
        the completion-future path) reads as UNKNOWN and fails OPEN, so a
        plumbing regression can only restore today's behavior rather than
        start failing every tick.

        The three suppression guards in :meth:`_guarded_fire` take
        precedence. A run that returns the quiet sentinel, blank output, or
        leaked markup already has a correct home — it posts nothing and is
        recorded a success — and must not be re-run or marked failed. The
        sentinel case is the load-bearing one: a skill that early-exits on
        already-resolved state legitimately returns the sentinel with zero
        tool calls, and that is a healthy quiet tick, not a failure.
        """
        if reply is None:
            return False
        count = meta.get("tool_call_count")
        if not isinstance(count, int) or count > 0:
            return False
        if cls._is_quiet_sentinel(reply) or cls._is_blank_output(reply):
            return False
        return not _contains_unparsed_toolcall_markup(reply)

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
        completed_at = datetime.now(UTC)
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
        when = datetime.now(UTC)
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
        if getattr(schedule, "job_kind", "agent_prompt") == "google_heartbeat":
            return  # transient provider trouble must never disable the heartbeat; alerts are rate-limited instead
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
