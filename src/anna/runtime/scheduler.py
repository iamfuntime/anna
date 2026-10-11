"""Phase 2 scheduler.

Polls :class:`ScheduleStore` for due schedules at every
``poll_interval_seconds`` and dispatches each as a synthetic
:class:`InboundEvent` through :class:`ConversationRouter`. The event
carries a ``completion_future`` so the worker resolves it with the
final assistant message instead of calling its normal outbound send
path. The scheduler then routes the message to the schedule's declared
destination via the appropriate :class:`ChannelAdapter` directly, and
records the delivered post in the destination conversation's transcript
(:mod:`anna.runtime.delivered_posts`) so a reply there has a referent.

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
import json
import shutil
import subprocess
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.runtime.delivered_posts import (
    conv_key_for_destination,
    record_delivered_post,
    transcript_has_delivery_key,
)
from anna.runtime.heartbeat import Classifier, HeartbeatJob, heartbeat_min_timeout
from anna.runtime.ledger import (
    OutboxItem,
    Recovery,
    RuntimeLedger,
    new_owner_id,
    now_utc,
    owner_alive,
)
from anna.runtime.schedule_jobs import JobResult, Outcome
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule
from anna.runtime.worker import (
    TURN_COST_FIELD,
    TURN_SPEND_FIELDS,
    TURN_USAGE_FIELDS,
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

# Wall-clock cap on a schedule's optional ``gate_command`` pre-flight. A gate
# exists to be cheaper than the fire it guards, so it gets a hard, short
# budget; overrunning it is a fail-open (the fire proceeds), never a skip.
GATE_TIMEOUT_SECONDS = 15.0

# The ONLY exit code that suppresses a fire. Derived from
# ``scripts/heartbeat_gate.py``: 0 = "nothing present, safe to skip", 10 =
# "something present or a check was inconclusive, run", 1 = "the gate itself
# malfunctioned, run". Its docstring states the caller contract directly —
# "The caller must treat ANY non-zero exit as 'run'. Only 0 is a skip."
GATE_SKIP_EXIT_CODE = 0

# Bound on the gate stdout we copy into the audit row and on the reason we
# derive from it. Mirrors the 280-char previews the no-tool-call guard emits.
GATE_REASON_MAX_CHARS = 280
GATE_OUTPUT_MAX_CHARS = 2000

__all__ = ["GATE_TIMEOUT_SECONDS", "QUIET_SENTINEL", "Scheduler"]


class GateOutcome(NamedTuple):
    """What a ``gate_command`` pre-flight decided, plus telemetry.

    ``skip`` is the only field that changes behavior and it is true ONLY on
    an unambiguous skip signal (a clean exit with
    :data:`GATE_SKIP_EXIT_CODE`). Every other field is audit payload.
    """

    skip: bool
    exit_code: int | None
    reason: str
    output: str
    duration_seconds: float

    @classmethod
    def fire(
        cls,
        *,
        exit_code: int | None = None,
        reason: str = "",
        output: str = "",
        duration_seconds: float = 0.0,
    ) -> "GateOutcome":
        """A fail-open outcome: whatever happened, the schedule fires."""
        return cls(
            skip=False,
            exit_code=exit_code,
            reason=reason,
            output=output,
            duration_seconds=duration_seconds,
        )


def _gate_reason(stdout: str) -> str:
    """Best-effort human reason for a suppression, read from gate stdout.

    Purely cosmetic: this NEVER influences the decision. A gate that prints
    nothing, prose, or malformed JSON still suppresses on its exit code
    alone, so no parsing failure here can turn a skip into a fire or the
    reverse. JSON objects are preferred because ``heartbeat_gate.py --json``
    emits one; a plain-text gate falls back to its last line.
    """
    text = (stdout or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text.splitlines()[-1].strip()[:GATE_REASON_MAX_CHARS]
    if isinstance(payload, dict):
        for key in ("reason", "detail", "summary", "decision"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:GATE_REASON_MAX_CHARS]
    return text[:GATE_REASON_MAX_CHARS]


def _accumulate_turn_usage(
    total: dict[str, object], meta: dict[str, object] | None
) -> None:
    """Fold one fire's ``turn_meta`` token counts and cost into a per-run tally.

    A scheduled run is not always one turn: the no-tool-call backstop fires a
    second time into a fresh ``meta``, and the worker's markup-regeneration
    path adds a third drain. Spend-per-job means the SUM of every drain the run
    paid for, so each attempt accumulates here rather than overwriting.
    Non-numeric / missing values are skipped (fail open — telemetry must never
    break a scheduled run), and ``model`` takes the most recent non-empty id.
    ``total_cost_usd`` is a float and sums the same way the counters do.
    """
    if not meta:
        return
    for key in TURN_SPEND_FIELDS:
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        previous = total.get(key)
        base = (
            previous
            if isinstance(previous, (int, float)) and not isinstance(previous, bool)
            else 0
        )
        summed = base + value
        total[key] = round(summed, 6) if isinstance(summed, float) else summed
    model = meta.get("model")
    if isinstance(model, str) and model:
        total["model"] = model


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


def _exception_chain(exc: BaseException, limit: int = 6) -> list[BaseException]:
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in chain and len(chain) < limit:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def _connect_phase_types() -> tuple[type[BaseException], ...]:
    """Exception classes that prove the request never reached the server.

    Only errors raised while *establishing* the connection qualify (refused, DNS, TLS to the proxy,
    connect timeout, no pooled connection). Resets, broken pipes, read timeouts and "server
    disconnected" can happen after bytes were sent, so they are deliberately NOT here.
    """
    import socket
    found: list[type[BaseException]] = [ConnectionRefusedError, socket.gaierror]
    try:
        import aiohttp
        found += [aiohttp.ClientConnectorError, aiohttp.ClientProxyConnectionError]
    except ImportError:
        pass
    try:
        import httpx
        found += [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
    except ImportError:
        pass
    return tuple(found)


def classify_send_error(exc: BaseException) -> bool:
    """True only when the failure PROVES the message was not delivered (safe to resend).

    Walks the ``__cause__``/``__context__`` chain so wrappers (python-telegram-bot's
    ``NetworkError``/``TimedOut`` around httpx, Slack SDK around aiohttp) are judged by the
    underlying error.  Ambiguous failures stay "uncertain" and are never resent automatically.
    """
    connect_types = _connect_phase_types()
    for err in _exception_chain(exc):
        if getattr(err, "retry_safe", False) is True or isinstance(err, connect_types):
            return True
        if type(err).__name__ == "RetryAfter":  # Telegram flood control: rejected, not delivered
            return True
        status = getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)
        if status == 429:
            return True
    return False


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
        self._owner = new_owner_id()
        self._absent: dict[str, int] = {}
        self._deleted: set[str] = set()
        self._shutting_down = False
        self._last_prune = 0.0
        self._spawned: set[int] = set()
        self._active_fires: set[int] = set()  # occurrences a task of THIS process is executing right now
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
        """Wait briefly for in-flight fires to land, then interrupt them as a SHUTDOWN.

        Shutdown is not deletion: read-only runs go back to the queue for the next start and
        side-effecting runs are reported honestly as unknown (see ``_guarded_fire``).
        """
        self._shutting_down = True
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
        self._deleted.add(schedule_id)
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
        load_ok = True
        try:
            await self._store.load()  # pick up dashboard edits and other writers
        except Exception as exc:
            load_ok = False
            self._log.warning("scheduler.reload_failed", error=str(exc))
        for done in self._ledger.apply_repairs():
            audit_event("audit.schedule.repair_applied", audit_dir=self._audit_dir, actor="operator",
                        fsync_on_write=self._fsync,
                        level="INFO" if done["outcome"] == "applied" else "WARNING", repair_id=done["id"],
                        kind=done["kind"], target_id=done["target_id"], action=done["action"],
                        outcome=done["outcome"], detail=done["detail"], requested_by=done["requested_by"])
        await self._apply_controls()
        if load_ok:
            for known in self._store.list():
                self._adopt_external(known.id)  # before recovery / completion stamps overwrite it
            self._emit_ledger_notes()
        for rec in self._ledger.recover_expired(now=now, max_run_attempts=cfg.max_run_attempts,
                                                owner_alive=owner_alive, own_owner=self._owner,
                                                own_active=frozenset(self._active_fires)):
            await self._on_recovery(rec)
        for info in self._ledger.recover_outbox_rows(now=now):
            await self._report_interrupted_send(info, "send interrupted (daemon stopped mid-send)")
        if load_ok:
            await self._reconcile_deleted()
        self._maybe_prune(now)
        for schedule in self._store.list():
            if schedule.enabled:
                self._materialize(schedule, now)
        for occ in self._ledger.pending():
            if occ.id in self._spawned:
                continue
            found = self._store.get(occ.schedule_id)
            if found is None:
                continue  # absence is not yet confirmed (see _reconcile_deleted); never cancel on one bad read
            if not found.enabled:
                self._ledger.end_unrun(occ.id, status="cancelled", detail="schedule disabled")
                continue
            self._spawn(found, occ.id)
        await self._deliver_due()
        self._record_settled_posts()  # also back-fills messages settled while a crash hid the bookkeeping

    async def _apply_controls(self) -> None:
        """Daemon-owned Google connector controls queued by the dashboard (see runtime/controls.py)."""
        import json

        from anna.tools.google_verify import verify_account
        for req in self._ledger.pending_controls():
            kind, slug = req["kind"], req["subject"]
            clients = self._google_clients
            try:
                if clients is None:
                    self._ledger.finish_control(req["id"], status="rejected",
                                                result="this daemon has no Google client (google.enabled is false?)")
                elif kind == "google_invalidate":
                    clients.invalidate(slug)
                    self._ledger.finish_control(req["id"], status="applied", result="cached Google services dropped")
                elif kind == "google_verify":
                    res = await verify_account(clients, self._config, slug)
                    self._ledger.finish_control(req["id"], status="applied", result=json.dumps(res))
                else:
                    self._ledger.finish_control(req["id"], status="rejected", result="unknown control kind")
            except Exception as exc:  # noqa: BLE001
                self._ledger.finish_control(req["id"], status="rejected", result=type(exc).__name__)
            audit_event("audit.google.control", audit_dir=self._audit_dir, actor="operator",
                        fsync_on_write=self._fsync, kind=kind, slug=slug, requested_by=req["requested_by"])

    async def _reconcile_deleted(self) -> None:
        """Cancel work of schedules another process deleted - only once the deletion is CONFIRMED.

        A hand edit or in-place save can leave ``schedules.yaml`` empty, truncated or missing for a
        moment.  A schedule must therefore be absent on two consecutive successful loads before its
        queued/running work is cancelled (cancellation is irreversible).  In-process deletes cancel
        immediately through the store callback and do not depend on this path.
        """
        present = {s.id for s in self._store.list()}
        active = self._ledger.active_schedule_ids()
        for sid in list(self._absent):
            if sid in present or sid not in active:
                self._absent.pop(sid, None)
        for sid in active - present:
            self._absent[sid] = self._absent.get(sid, 0) + 1
            if self._absent[sid] >= 2:
                self._absent.pop(sid, None)
                await self.cancel_schedule_tasks(sid)

    def _maybe_prune(self, now: datetime) -> None:
        cfg = self._config.scheduler
        mono = asyncio.get_running_loop().time()
        if self._last_prune and mono - self._last_prune < cfg.prune_interval_hours * 3600:
            return
        self._last_prune = mono
        try:
            counts = self._ledger.prune(now=now, retention_days=cfg.retention_days,
                                        text_retention_days=cfg.text_retention_days)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("scheduler.prune_failed", error=str(exc)[:200])
            return
        if any(counts.values()):
            audit_event("audit.schedule.pruned", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, occurrences=counts["occurrences"],
                        outbox_text_purged=counts["outbox_text_purged"], results_redacted=counts["results_redacted"],
                        repairs=counts["repairs"])

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
                last_fired_at=schedule.state.last_fired_at,
            )
        except Exception as exc:
            self._log.warning("scheduler.materialize_failed", schedule_id=schedule.id, error=str(exc))
            return
        self._emit_ledger_notes()
        for occ_id in created:
            audit_event("audit.schedule.occurrence", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, schedule_id=schedule.id, occurrence_id=occ_id)

    # The ledger records every ``last_fired_at`` this build writes (BEFORE the write), so
    # ``materialize`` can tell its own completion-time stamps from ones an older build wrote.

    def _adopt_external(self, schedule_id: str) -> None:
        """Adopt an older build's ``last_fired_at`` BEFORE this build overwrites it with its own stamp."""
        found = self._store.get(schedule_id)
        if found is not None:
            self._ledger.adopt_external_stamp(schedule_id, found.state.last_fired_at, now=now_utc(),
                                              job_kind=getattr(found, "job_kind", "agent_prompt"))

    async def _mark_fired(self, schedule_id: str, *, when: datetime) -> None:
        self._adopt_external(schedule_id)
        self._ledger.note_stamp(schedule_id, when)
        await self._store.mark_fired(schedule_id, status="complete", when=when)

    async def _mark_dispatched(self, schedule_id: str, *, when: datetime) -> None:
        self._adopt_external(schedule_id)
        self._ledger.note_stamp(schedule_id, when)
        await self._store.mark_dispatched(schedule_id, when=when)

    async def _mark_failed(self, schedule_id: str, *, reason: str, when: datetime, kind: str) -> int:
        self._adopt_external(schedule_id)
        self._ledger.note_stamp(schedule_id, when)
        return await self._store.mark_failed(schedule_id, reason=reason, when=when, kind=kind)

    def _emit_ledger_notes(self) -> None:
        for note in self._ledger.drain_notes():
            kind = note.pop("kind")
            audit_event(f"audit.schedule.cursor_{kind}", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, level="WARNING", **note)
            self._log.warning(f"scheduler.cursor_{kind}", schedule_id=note["schedule_id"])

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
        if rec.action == "skipped":  # another LIVE owner: never taken over, only reported
            audit_event("audit.schedule.recovery_skipped", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, level="WARNING", schedule_id=rec.schedule_id,
                        occurrence_id=rec.occurrence_id, detail=rec.detail)
            return
        level = "WARNING" if rec.action in ("unknown", "failed") else "INFO"
        audit_event("audit.schedule.recovered", audit_dir=self._audit_dir, actor="anna",
                    fsync_on_write=self._fsync, level=level, schedule_id=rec.schedule_id,
                    occurrence_id=rec.occurrence_id, action=rec.action, detail=rec.detail)
        self._log.warning("scheduler.recovered", schedule_id=rec.schedule_id,
                          occurrence_id=rec.occurrence_id, action=rec.action)
        if rec.action in ("unknown", "failed"):
            await self._handle_failure(
                self._store.get(rec.schedule_id) or _stub_schedule(rec.schedule_id),
                reason=rec.detail, kind="interrupted" if rec.action == "unknown" else "fail")

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
            model=schedule.model,
        )

        await self._router.dispatch(event)
        return await completion

    # ------------------------------------------------------------------
    # Guarded fire with timeout + failure handling
    # ------------------------------------------------------------------

    async def _consult_gate(self, schedule: Schedule) -> GateOutcome:
        """Run ``schedule.gate_command`` and report whether to skip this tick.

        FAILS OPEN ON EVERYTHING. The contract is the one
        ``scripts/heartbeat_gate.py`` documents and the only one this method
        implements: a clean exit with code :data:`GATE_SKIP_EXIT_CODE` (0)
        means "nothing present, skip"; ANY other outcome — any other exit
        code, a timeout, a missing or non-executable binary, an OS error, an
        unparseable stdout, an unexpected exception — means FIRE. There is
        deliberately no branch in here that turns an ambiguous result into a
        skip, because a gate that wakes too often only costs money while a
        gate that stays quiet when it should not loses work.

        ``subprocess.run`` is pushed onto a worker thread so a gate that
        sits for its full :data:`GATE_TIMEOUT_SECONDS` cannot stall the
        scheduler's event loop. ``CancelledError`` is intentionally NOT
        caught: a fire cancelled by :meth:`cancel_schedule_tasks` while its
        gate is running must stay cancelled, exactly as it does at every
        other await point in the fire path.
        """
        command = schedule.gate_command or []
        if not command:
            return GateOutcome.fire()
        started = time.monotonic()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=GATE_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
            )
        except asyncio.CancelledError:
            raise
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            self._log.warning(
                "scheduler.gate_timeout",
                schedule_id=schedule.id,
                timeout_seconds=GATE_TIMEOUT_SECONDS,
                duration_seconds=elapsed,
            )
            return GateOutcome.fire(
                reason=f"gate timed out after {GATE_TIMEOUT_SECONDS:.0f}s",
                duration_seconds=elapsed,
            )
        except Exception as exc:
            # Missing binary, not executable, permission denied, a bad argv
            # type that only blows up at spawn — all of it fires.
            elapsed = time.monotonic() - started
            self._log.warning(
                "scheduler.gate_error",
                schedule_id=schedule.id,
                error=str(exc),
                duration_seconds=elapsed,
            )
            return GateOutcome.fire(
                reason=f"gate error: {type(exc).__name__}: {exc}"[
                    :GATE_REASON_MAX_CHARS
                ],
                duration_seconds=elapsed,
            )

        elapsed = time.monotonic() - started
        stdout = (proc.stdout or "")[:GATE_OUTPUT_MAX_CHARS]
        if proc.returncode != GATE_SKIP_EXIT_CODE:
            # The expected wake path. Not a warning: a gate saying "there is
            # work" is the normal, healthy answer most of the time.
            self._log.info(
                "scheduler.gate_wake",
                schedule_id=schedule.id,
                exit_code=proc.returncode,
                duration_seconds=elapsed,
            )
            return GateOutcome.fire(
                exit_code=proc.returncode,
                reason=_gate_reason(proc.stdout or ""),
                output=stdout,
                duration_seconds=elapsed,
            )
        return GateOutcome(
            skip=True,
            exit_code=proc.returncode,
            reason=_gate_reason(proc.stdout or ""),
            output=stdout,
            duration_seconds=elapsed,
        )

    async def _record_gate_skip(
        self, schedule: Schedule, outcome: GateOutcome
    ) -> None:
        """Audit a suppressed tick.

        This row is the ONLY record that a tick happened at all — no
        ``audit.schedule.fire`` is emitted for a gated-off tick — so counting
        ``audit.schedule.gate_skip`` against ``audit.schedule.fire`` gives the
        measured suppression rate rather than an estimated one. No store
        mutation: ``_poll_once`` already advanced ``last_fired_at`` via
        ``mark_dispatched`` (so the next cron tick computes correctly), and a
        tick that never ran is neither a success nor a failure, so
        ``last_status`` and ``consecutive_failures`` are left untouched.
        """
        audit_event(
            "audit.schedule.gate_skip",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule.id,
            gate_command=list(schedule.gate_command or ()),
            exit_code=outcome.exit_code,
            reason=outcome.reason,
            gate_output=outcome.output,
            duration_seconds=outcome.duration_seconds,
        )
        self._log.info(
            "scheduler.gate_skip",
            schedule_id=schedule.id,
            reason=outcome.reason,
            duration_seconds=outcome.duration_seconds,
        )

    async def _guarded_fire(self, schedule: Schedule, *, occurrence_id: int | None = None) -> None:
        """Claim, run and settle one occurrence.

        Execution is recorded in the ledger (claim -> run -> typed result) and
        delivery is a separate outbox step.  ``occurrence_id=None`` creates an
        ad-hoc occurrence (tests / a future ``fire_now``).
        """
        if occurrence_id is None:
            occurrence_id = self._ledger.create_manual(
                schedule_id=schedule.id, job_kind=schedule.job_kind, run_safety=schedule.run_safety)
        self._active_fires.add(occurrence_id)
        try:
            await self._guarded_fire_claimed(schedule, occurrence_id)
        finally:
            self._active_fires.discard(occurrence_id)

    async def _guarded_fire_claimed(self, schedule: Schedule, occurrence_id: int) -> None:
        async with self._semaphore:
            lease = 2 * schedule.timeout_seconds + self._config.scheduler.lease_grace_seconds
            if not self._ledger.claim(occurrence_id, owner=self._owner, lease_seconds=lease):
                return
            # Optional pre-flight gate. Runs BEFORE audit.schedule.fire so a
            # suppressed tick emits exactly one row (gate_skip) and the fire
            # count stays a count of real fires. Fails open on everything.
            gate = await self._consult_gate(schedule)
            if gate.skip:
                await self._skip_for_gate(schedule, occurrence_id, gate)
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

            try:
                deliver = await self._execute_started(schedule, occurrence_id, run_id, started_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - bookkeeping blew up AFTER the run started
                await self._abort_started(schedule, occurrence_id, run_id, exc)
                return
        if deliver:
            await self._deliver_due()

    async def _execute_started(self, schedule: Schedule, occurrence_id: int, run_id: int,
                               started_at: datetime) -> bool:
        """Run a started occurrence to its terminal state. True = the caller should deliver the outbox."""
        if schedule.job_kind == "google_heartbeat":
            await self._run_heartbeat(schedule, occurrence_id, run_id)
            await self._deliver_due()
            return False
        meta: dict[str, object] = {}
        try:
            result = await self._run_job(schedule, meta)
        except asyncio.CancelledError:
            if schedule.id not in self._deleted and schedule.run_safety == "read_only":
                # SHUTDOWN of a read-only run: hand it back to the queue for the next start.
                self._ledger.repend_running(occurrence_id, run_id, owner=self._owner,
                                            detail="shutdown: read-only run returned to the queue")
                self._audit_shutdown(schedule, occurrence_id, "repended")
                raise
            verdict = meta.get("_cancel_verdict")
            status, effects, detail = verdict if isinstance(verdict, tuple) else (
                "unknown", "unknown", "scheduler task cancelled; outcome unverified")
            self._ledger.finish_run(occurrence_id, run_id, status=status, side_effects=effects,
                                    error=detail, detail=detail)
            if schedule.id not in self._deleted:
                # SHUTDOWN of side-effecting work: say plainly that the outcome is unknown.
                self._audit_shutdown(schedule, occurrence_id, status)
                await self._handle_failure(schedule, reason=f"shutdown: {status}", kind="interrupted")
            raise
        except _TurnTimeout as exc:
            self._ledger.finish_run(occurrence_id, run_id, status=exc.status, side_effects=exc.side_effects,
                                    error=exc.detail, detail=f"timeout: {exc.detail}")
            await self._handle_failure(
                schedule, reason=f"timeout after {schedule.timeout_seconds}s ({exc.status}: {exc.detail})",
                kind="timeout")
            return False
        except Exception as exc:
            self._ledger.finish_run(occurrence_id, run_id, status="failed", side_effects="unknown",
                                    error=f"{type(exc).__name__}: {exc}"[:300])
            await self._handle_failure(schedule, reason=f"error: {exc}", kind="fail")
            return False

        if result.outcome == "failed":
            self._ledger.finish_run(occurrence_id, run_id, status="failed", side_effects=result.side_effects,
                                    result=result.to_json(), error=result.error, detail=result.error)
            await self._handle_failure(schedule, reason=result.error or "job failed",
                                       kind=str(result.data.get("kind", "fail")))
            return False

        if result.outcome == "quiet":
            self._ledger.finish_run(occurrence_id, run_id, status="succeeded", side_effects=result.side_effects,
                                    result=result.to_json(), delivery_status="suppressed",
                                    detail=result.quiet_reason)
            await self._record_success(schedule, result.output, started_at=started_at, suppressed=True,
                                      usage=meta.get("_usage"))  # type: ignore[arg-type]
            return False

        dest = schedule.destination
        written = self._ledger.finish_run(
            occurrence_id, run_id, status="succeeded", side_effects=result.side_effects,
            result=result.to_json(), outbox=[(dest.transport, dest.channel, result.output)],
            chunk_chars=self._config.scheduler.delivery_chunk_chars)
        if not written:
            return  # recovered/cancelled meanwhile: never enqueue a duplicate delivery
        await self._record_success(schedule, result.output, started_at=started_at,
                                   usage=meta.get("_usage"))  # type: ignore[arg-type]
        return True

    async def _abort_started(self, schedule: Schedule, occurrence_id: int, run_id: int, exc: Exception) -> None:
        """An exception escaped after ``start_run``: never leave the occurrence ``running`` for a live owner.

        The outcome is genuinely unverifiable (the run may have acted), so it ends ``unknown`` and is not
        re-run.  If even this write fails, ``recover_expired`` reclaims the row because this process no
        longer has a fire task for it.
        """
        detail = f"internal error after run start: {type(exc).__name__}: {exc}"[:300]
        self._log.error("scheduler.fire_aborted", schedule_id=schedule.id, occurrence_id=occurrence_id, error=detail)
        try:
            closed = self._ledger.finish_run(occurrence_id, run_id, status="unknown", side_effects="unknown",
                                             error=detail, detail=detail)
        except Exception as inner:  # noqa: BLE001
            self._log.error("scheduler.fire_abort_unrecorded", occurrence_id=occurrence_id, error=str(inner)[:200])
            return  # orphan recovery closes it on a later poll
        if closed:
            await self._handle_failure(schedule, reason=detail, kind="interrupted")

    # ------------------------------------------------------------------
    # Built-in read-only Gmail + Calendar heartbeat (no worker turn, no LLM)
    # ------------------------------------------------------------------

    def _audit_shutdown(self, schedule: Schedule, occurrence_id: int, outcome: str) -> None:
        audit_event("audit.schedule.shutdown_interrupted", audit_dir=self._audit_dir, actor="anna",
                    fsync_on_write=self._fsync, level="WARNING", schedule_id=schedule.id,
                    occurrence_id=occurrence_id, outcome=outcome, run_safety=schedule.run_safety,
                    kind="unknown" if outcome in ("unknown", "blocked") else outcome)

    async def _run_heartbeat(self, schedule: Schedule, occurrence_id: int, run_id: int) -> None:
        cfg = self._config
        finish = self._ledger.finish_run

        tz = ZoneInfo(schedule.timezone)
        job = HeartbeatJob(config=cfg, clients=self._google_clients, ledger=self._ledger,
                           classifier=self._heartbeat_classifier)
        dest = f"{schedule.destination.transport}:{schedule.destination.channel}"
        allowed = dest in cfg.heartbeat.allowed_destinations

        async def fail(exec_status: str, kind: str, error: str) -> None:
            """Whole-job failure: same transition alerting as a failed check (never a quiet 'healthy' tick)."""
            now_f = datetime.now(UTC)
            outcome = job.job_failure(schedule, kind, error, now_f, tz)
            specs = job.outbox_specs(schedule, outcome, now_f, tz) if allowed else []
            finish(occurrence_id, run_id, status=exec_status, side_effects="none", result=outcome.summary,
                   error=error, detail=error, outbox=specs or None,
                   chunk_chars=cfg.scheduler.delivery_chunk_chars, heartbeat=outcome.commit)
            audit_event("audit.schedule.heartbeat", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, level="WARNING", schedule_id=schedule.id,
                        occurrence_id=occurrence_id, status="failed", error=error[:200],
                        job_alert=outcome.summary.get("job_alert"))
            await self._mark_failed(schedule.id, reason=error, when=now_f, kind="fail")
            if outcome.notices and not specs:  # cannot go to the (non-allowlisted) destination: tell the admin
                await self._warn_admin(outcome.notices[0].text)

        if not cfg.heartbeat.enabled:
            await fail("failed", "misconfigured", "heartbeat is disabled (set heartbeat.enabled: true)")
            return
        if not allowed:
            await fail("failed", "misconfigured", f"destination {dest!r} is not in heartbeat.allowed_destinations")
            return
        occ = self._ledger.get(occurrence_id)
        budget = max(schedule.timeout_seconds, heartbeat_min_timeout(cfg.heartbeat))
        try:
            outcome = await asyncio.wait_for(job.run(schedule, scheduled_for=occ.scheduled_for), timeout=budget)
        except asyncio.CancelledError:
            if schedule.id in self._deleted:
                finish(occurrence_id, run_id, status="cancelled", side_effects="none",
                       error="schedule deleted while the heartbeat ran", detail="cancelled")
            else:  # shutdown: a read-only heartbeat is simply run again after the restart
                self._ledger.repend_running(occurrence_id, run_id, owner=self._owner,
                                            detail="shutdown: read-only heartbeat returned to the queue")
                self._audit_shutdown(schedule, occurrence_id, "repended")
            raise
        except TimeoutError:
            await fail("timeout", "api_error", f"heartbeat timed out after {budget:.0f}s")
            return
        except Exception as exc:  # noqa: BLE001
            await fail("failed", "api_error", f"heartbeat error: {type(exc).__name__}: {str(exc)[:150]}")
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
            await self._mark_failed(schedule.id, reason=outcome.status, when=now, kind="fail")

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
        usage_total: dict[str, object] = {}
        reply = await self._turn(schedule, meta)
        _accumulate_turn_usage(usage_total, meta)
        meta["_usage"] = usage_total
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
            _accumulate_turn_usage(usage_total, meta)
            meta["_usage"] = usage_total
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
            # One row per claim: a lease only ever covers the send that is actually in flight, so a row
            # that has not been attempted can never expire into "uncertain".
            items = self._ledger.claim_outbox(now=now_utc(), lease_seconds=cfg.send_timeout_seconds + 30,
                                              limit=1, exclude=frozenset(attempted))
            if not items:
                return
            for item in items:
                attempted.add(item.id)
                await self._send_item(item)

    async def _warn_admin(self, message: str, exclude: str | None = None) -> None:
        try:
            await asyncio.wait_for(self._alerter.warn(message, exclude_channel=exclude), timeout=15)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("scheduler.admin_warn_failed", error=str(exc)[:200])

    async def _report_interrupted_send(self, info: dict[str, Any], why: str) -> None:
        """An ambiguous / partial delivery must be visible: audit + admin notice, never silent truncation."""
        left = int(info.get("stranded", 0))
        audit_event("audit.schedule.delivery_interrupted", audit_dir=self._audit_dir, actor="anna",
                    fsync_on_write=self._fsync, level="WARNING", schedule_id=info["schedule_id"],
                    occurrence_id=info["occurrence_id"], outbox_id=info["id"], part=info["chunk_index"] + 1,
                    parts=info["chunk_count"], stranded_parts=left, transport=info["transport"],
                    channel=info["channel"], reason=why)
        self._record_settled_posts()
        await self._warn_admin(
            f"Delivery of scheduled output '{info['schedule_id']}' (run #{info['occurrence_id']}) to "
            f"{info['transport']}:{info['channel']} was interrupted at part {info['chunk_index'] + 1} of "
            f"{info['chunk_count']} ({why}). That part may or may not have arrived and was NOT resent"
            + (f"; the remaining {left} part(s) were not sent" if left else "")
            + ". Review it in the dashboard (Runs): mark delivered / resend / abandon.",
            exclude=info["transport"])

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
            await self._report_interrupted_send(self._ledger.interrupted_send(item), "send cancelled mid-flight")
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
                self._record_settled_posts()
                if outcome == "uncertain" and self._ledger.interrupted_send(item)["stranded"]:
                    await self._report_interrupted_send(self._ledger.interrupted_send(item), f"{type(exc).__name__}")
                elif self._ledger.get(item.occurrence_id).job_kind != "agent_prompt":
                    await self._warn_admin(
                        f"A {outcome} delivery of heartbeat output to {item.transport}:{item.channel} could not be "
                        f"confirmed ({type(exc).__name__}). It will be repeated on the next successful heartbeat.",
                        exclude=item.transport)
                await self._handle_failure(
                    self._store.get(item.schedule_id) or _stub_schedule(item.schedule_id),
                    reason=f"send failed ({outcome}): {exc}", kind="delivery")
            return
        self._ledger.outbox_sent(item)
        self._record_settled_posts()

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
        recorded_conv_key: str | None = None,
        usage: dict[str, object] | None = None,
    ) -> None:
        """Record a successful fire: mark_fired (resets failures, sets
        last_fired_at + last_status="complete"), audit, and log.

        Shared by the normal-send path and the quiet-sentinel path so both
        use identical bookkeeping. ``suppressed`` only affects telemetry; the
        persisted success state is the same in both cases.
        ``recorded_conv_key`` is telemetry too: the conversation whose
        transcript the delivered post was appended to (see
        :meth:`_record_settled_posts`). The post is only recorded once it is
        delivered, after this row is written, so this is always ``None`` here;
        the record is audited as ``audit.schedule.delivered_post_recorded``.

        ``usage`` carries the run's token spend (see
        :func:`_accumulate_turn_usage`) and is stamped onto the audit row as
        ADDITIVE keys — ``model`` plus the four billable counters plus a
        ``duration_s`` alias of the long-standing ``duration_seconds``. Every
        pre-existing key keeps its name and value, so old log parsers are
        unaffected. Defaults to ``None`` (keyword-only) so any caller that
        does not track usage still works.
        """
        completed_at = datetime.now(UTC)
        duration = (completed_at - started_at).total_seconds()
        await self._mark_fired(schedule.id, when=completed_at)
        # Missing counters land as 0 rather than being omitted, so the row has
        # a stable shape and a summing grep never trips over an absent key.
        usage_fields: dict[str, object] = {
            "model": None,
            **{key: 0 for key in TURN_USAGE_FIELDS},
            TURN_COST_FIELD: 0.0,
        }
        if usage:
            usage_fields.update(usage)
        audit_event(
            "audit.schedule.complete",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule.id,
            duration_seconds=duration,
            output_length=len(reply),
            suppressed=suppressed,
            recorded_conv_key=recorded_conv_key,
            delivery="suppressed" if suppressed else "queued",
            destination_conv_key=None if suppressed else conv_key_for_destination(
                self._config, transport=schedule.destination.transport, channel=schedule.destination.channel),
            duration_s=round(duration, 3),
            **usage_fields,
        )
        self._log.info(
            "scheduler.complete",
            schedule_id=schedule.id,
            duration_seconds=duration,
            output_length=len(reply),
            suppressed=suppressed,
            recorded_conv_key=recorded_conv_key,
        )

    async def _skip_for_gate(self, schedule: Schedule, occurrence_id: int, gate: GateOutcome) -> None:
        """Close an occurrence a ``gate_command`` suppressed.

        Neither a success nor a failure: ``last_status`` and
        ``consecutive_failures`` are untouched, but ``last_fired_at`` advances
        (as the pre-ledger scheduler did) so a rollback to that scheduler does
        not replay the tick.
        """
        self._ledger.end_unrun(occurrence_id, status="skipped_gate", detail=gate.reason or "gate skip")
        await self._record_gate_skip(schedule, gate)
        await self._mark_dispatched(schedule.id, when=datetime.now(UTC))

    def _record_settled_posts(self) -> None:
        """Write one transcript row per SETTLED outbox message (all parts together, destination from the row).

        A long report is delivered as several parts; recording each part would leave a live DM worker only
        the tail of it, so the row is written once the message settled: every part sent (full text), or
        some parts sent and the rest stranded/uncertain/failed (the sent prefix plus an explicit marker).
        Idempotent: the row carries a ``delivery_key`` (checked before writing) and the ledger remembers
        what was recorded, so a crash between the append and the bookkeeping repeats nothing.
        Exception-isolated: the post is already delivered, so a recording problem must never fail a fire.
        """
        try:
            messages = self._ledger.settled_unrecorded(since=now_utc() - timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("scheduler.post_record_failed", error=str(exc)[:200])
            return
        for msg in messages:
            try:
                self._record_one_settled(msg)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("scheduler.post_record_failed", schedule_id=msg["schedule_id"], error=str(exc)[:200])

    def _record_one_settled(self, msg: dict[str, Any]) -> None:
        occ, idx, sid = msg["occurrence_id"], msg["msg_index"], msg["schedule_id"]
        partial, sent, total = bool(msg["partial"]), msg["sent_parts"], msg["chunk_count"]
        conv_key = conv_key_for_destination(self._config, transport=msg["transport"], channel=msg["channel"])
        if conv_key is None:  # a shared channel: nothing to attach a reply to
            self._ledger.mark_recorded(occ, idx, sent_parts=sent, partial=partial, conv_key=None)
            return
        text = msg["text"]
        if partial:
            unconfirmed = msg["unconfirmed"]
            text += (f"\n\n[Partial delivery: {sent} of {total} parts of this message were sent"
                     + (f"; part {unconfirmed[0] + 1} may or may not have arrived" if unconfirmed else "")
                     + "; the remaining parts were NOT sent.]")
        # (schedule, tick) not the row id: ids are reused after pruning
        key = f"outbox:{sid}:{msg["scheduled_for"]}:{idx}:{sent}"
        if transcript_has_delivery_key(self._config, conv_key, key):
            written: str | None = conv_key
        else:
            written = record_delivered_post(self._config, transport=msg["transport"], channel=msg["channel"],
                                            text=text, source=f"schedule:{sid}", delivery_key=key)
        if written is None:
            self._log.warning("scheduler.post_record_failed", schedule_id=sid, error="transcript write failed")
            audit_event("audit.schedule.delivered_post_record_failed", audit_dir=self._audit_dir, actor="anna",
                        fsync_on_write=self._fsync, level="WARNING", schedule_id=sid, occurrence_id=occ,
                        msg_index=idx, delivery_key=key)
            self._ledger.mark_recorded(occ, idx, sent_parts=sent, partial=partial, conv_key=None)
            return
        self._ledger.mark_recorded(occ, idx, sent_parts=sent, partial=partial, conv_key=written)
        audit_event("audit.schedule.delivered_post_recorded", audit_dir=self._audit_dir, actor="anna",
                    fsync_on_write=self._fsync, schedule_id=sid, occurrence_id=occ, msg_index=idx,
                    parts=total, sent_parts=sent, partial=partial, recorded_conv_key=written, delivery_key=key)

    @staticmethod
    def _dest_conv_key(*, transport: str, channel: str) -> str:
        """Synthesize a conv_key the adapter's send path will decode.

        Mirrors the AdminAlerter convention: both Slack channel posts
        and Telegram chat sends use the ``<transport>:dm:<id>`` form,
        which both adapters understand as a one-shot post to the
        channel or chat.
        """
        return f"{transport}:dm:{channel}"

    def _interrupted_streak(self, schedule_id: str) -> int:
        """Newest-first count of runs that ended interrupted, up to the first real outcome."""
        streak = 0
        for occ in self._ledger.recent(schedule_id, 50):
            if occ.exec_status in ("unknown", "blocked") and not (occ.detail or "").startswith("timeout:"):
                streak += 1
            elif occ.exec_status in ("succeeded", "failed", "timeout", "partial", "auth_required", "unknown", "blocked"):
                break  # a completed run, or a timeout (already a counted failure): the streak ends
        return streak

    async def _handle_failure(
        self,
        schedule: Schedule,
        *,
        reason: str,
        kind: str,
    ) -> None:
        when = datetime.now(UTC)
        if kind == "interrupted" and self._interrupted_streak(schedule.id) >= self._config.scheduler.interruption_threshold:
            reason = f"{reason} (interrupted {self._config.scheduler.interruption_threshold}+ runs in a row)"
            kind = "fail"
        new_count = await self._mark_failed(schedule.id, reason=reason, when=when, kind=kind)
        audit_event(
            # restarts are not failures: anything counting audit.schedule.fail counts real failures only
            "audit.schedule.interrupted" if kind == "interrupted" else "audit.schedule.fail",
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
        if kind == "interrupted":
            return  # restart/crash is not a job failure: never counts toward auto-disable
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
