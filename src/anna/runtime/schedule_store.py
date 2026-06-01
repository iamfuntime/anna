"""Persistence for the Phase 2 scheduler.

ScheduleStore owns ``schedules.yaml``. All mutations go through the
supervisor lock keyed ``schedules.yaml`` so concurrent MCP-tool calls
and the scheduler coroutine cannot race. Atomic save mirrors the
core-file write pattern from :mod:`anna.runtime.supervisor`: write a
temp file, fsync if configured, ``os.replace`` into place.

Reserved-destination validation lives here rather than on the
:class:`anna.runtime.schedule_types.ScheduleDestination` model because
the check needs ``AdminConfig`` to compare against. A schedule whose
destination matches ``config.admin.slack_channel_id`` (when transport
is slack) or ``config.admin.telegram_chat_id`` (when telegram) is
rejected at create or update time; the admin channel is reserved for
:class:`anna.runtime.alerter.AdminAlerter` traffic.

``due_schedules(now)`` is the entrypoint the scheduler coroutine calls
each poll cycle. It iterates enabled schedules, computes each
schedule's next-fire time using :mod:`croniter` against the schedule's
declared timezone, and returns the subset whose next fire has passed.
For first fires (no recorded ``last_fired_at``) the baseline is
``created_at``; subsequent fires use ``last_fired_at``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from croniter import croniter

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState
from anna.runtime.supervisor import Supervisor


_STORE_KEY = "schedules.yaml"


class ScheduleValidationError(ValueError):
    """Raised when a schedule create or update fails validation."""


class ScheduleStore:
    """In-memory cache plus YAML persistence for the schedule list."""

    def __init__(self, *, config: AnnaConfig, supervisor: Supervisor) -> None:
        self._config = config
        self._supervisor = supervisor
        self._path = config.scheduler.resolved_state_path
        self._audit_dir = config.audit_dir
        self._fsync = config.logging.audit.fsync_on_write
        self._log = get_logger("anna.scheduler.store")
        self._cache: dict[str, Schedule] = {}

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> None:
        """Read ``schedules.yaml`` into the in-memory cache.

        Missing file is fine; we start with an empty cache. Malformed
        YAML or invalid schedule records raise; the operator must
        repair the file by hand. The store does not silently drop bad
        entries because a bad entry usually means the operator typo'd a
        cron expression and would rather see the error than have ANNA
        skip a schedule they thought was active.
        """
        if not self._path.exists():
            self._cache = {}
            return
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        records = raw.get("schedules", [])
        cache: dict[str, Schedule] = {}
        for record in records:
            schedule = Schedule.model_validate(record)
            if schedule.id in cache:
                raise ScheduleValidationError(
                    f"Duplicate schedule id '{schedule.id}' in {self._path}"
                )
            cache[schedule.id] = schedule
        self._cache = cache
        self._log.info("scheduler.store.loaded", count=len(self._cache), path=str(self._path))

    async def save(self) -> None:
        """Atomically persist the cache to ``schedules.yaml``.

        Always called inside the supervisor lock. The mutation helpers
        below acquire the lock; tests can call ``save`` directly when
        they already hold equivalent isolation.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "schedules": [s.model_dump(mode="json") for s in self._cache.values()],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        if self._fsync:
            with tmp.open("rb") as fp:
                os.fsync(fp.fileno())
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Read helpers (no lock needed; cache is read-mostly)
    # ------------------------------------------------------------------

    def list(self) -> list[Schedule]:
        return list(self._cache.values())

    def get(self, schedule_id: str) -> Schedule | None:
        return self._cache.get(schedule_id)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _check_destination(self, dest: ScheduleDestination) -> None:
        admin = self._config.admin
        if dest.transport == "slack" and dest.channel == admin.slack_channel_id and admin.slack_channel_id:
            raise ScheduleValidationError(
                f"Slack channel {dest.channel} is the admin channel and is reserved "
                f"for AdminAlerter traffic. Pick a different channel for scheduled output."
            )
        if dest.transport == "telegram" and dest.channel == admin.telegram_chat_id and admin.telegram_chat_id:
            raise ScheduleValidationError(
                f"Telegram chat {dest.channel} is the admin chat and is reserved "
                f"for AdminAlerter traffic. Pick a different chat for scheduled output."
            )

    def _check_cron(self, expr: str) -> None:
        if not croniter.is_valid(expr):
            raise ScheduleValidationError(
                f"Cron expression '{expr}' is not a valid 5-field cron. "
                f"Use the natural-language parser or pass a valid expression."
            )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def create(self, schedule: Schedule, *, actor_conv: str | None = None) -> Schedule:
        if schedule.id in self._cache:
            raise ScheduleValidationError(f"Schedule '{schedule.id}' already exists")
        self._check_destination(schedule.destination)
        self._check_cron(schedule.cron)
        async with await self._supervisor.acquire(_STORE_KEY):
            self._cache[schedule.id] = schedule
            await self.save()
        audit_event(
            "audit.schedule.created",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule.id,
            cron=schedule.cron,
            destination_transport=schedule.destination.transport,
            destination_channel=schedule.destination.channel,
            creator_conv=actor_conv or "",
        )
        return schedule

    async def update(
        self,
        schedule_id: str,
        *,
        actor_conv: str | None = None,
        **changes: Any,
    ) -> Schedule:
        if schedule_id not in self._cache:
            raise ScheduleValidationError(f"Schedule '{schedule_id}' does not exist")
        if "id" in changes:
            raise ScheduleValidationError("schedule.id is immutable; create a new schedule instead")
        existing = self._cache[schedule_id]
        merged = existing.model_dump()
        # Only touch fields the caller actually passed.
        applied_fields: list[str] = []
        for key, value in changes.items():
            if key not in merged and key not in {"destination", "state"}:
                raise ScheduleValidationError(f"Unknown field '{key}' for Schedule")
            merged[key] = value
            applied_fields.append(key)
        new_schedule = Schedule.model_validate(merged)
        self._check_destination(new_schedule.destination)
        self._check_cron(new_schedule.cron)
        async with await self._supervisor.acquire(_STORE_KEY):
            self._cache[schedule_id] = new_schedule
            await self.save()
        audit_event(
            "audit.schedule.updated",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule_id,
            changed_fields=applied_fields,
            actor_conv=actor_conv or "",
        )
        return new_schedule

    async def delete(self, schedule_id: str, *, actor_conv: str | None = None) -> None:
        if schedule_id not in self._cache:
            raise ScheduleValidationError(f"Schedule '{schedule_id}' does not exist")
        async with await self._supervisor.acquire(_STORE_KEY):
            del self._cache[schedule_id]
            await self.save()
        audit_event(
            "audit.schedule.deleted",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            schedule_id=schedule_id,
            actor_conv=actor_conv or "",
        )

    # ------------------------------------------------------------------
    # State mutations driven by the scheduler itself
    # ------------------------------------------------------------------

    async def mark_fired(
        self,
        schedule_id: str,
        *,
        status: str,
        when: datetime,
    ) -> None:
        async with await self._supervisor.acquire(_STORE_KEY):
            schedule = self._cache.get(schedule_id)
            if schedule is None:
                return
            schedule.state = ScheduleState(
                last_fired_at=when,
                last_status="complete",
                consecutive_failures=0,
            )
            await self.save()

    async def mark_failed(
        self,
        schedule_id: str,
        *,
        reason: str,
        when: datetime,
        kind: str = "fail",
    ) -> int:
        """Increment consecutive_failures. Returns the new count."""
        async with await self._supervisor.acquire(_STORE_KEY):
            schedule = self._cache.get(schedule_id)
            if schedule is None:
                return 0
            new_count = schedule.state.consecutive_failures + 1
            last_status = "timeout" if kind == "timeout" else "fail"
            schedule.state = ScheduleState(
                last_fired_at=when,
                last_status=last_status,
                consecutive_failures=new_count,
            )
            await self.save()
            return new_count

    async def mark_disabled(self, schedule_id: str, *, reason: str) -> None:
        async with await self._supervisor.acquire(_STORE_KEY):
            schedule = self._cache.get(schedule_id)
            if schedule is None:
                return
            disabled = schedule.model_copy(update={"enabled": False})
            self._cache[schedule_id] = disabled
            await self.save()
        audit_event(
            "audit.schedule.disabled",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            level="WARNING",
            schedule_id=schedule_id,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Due-schedule computation
    # ------------------------------------------------------------------

    def due_schedules(self, now: datetime) -> list[Schedule]:
        """Return enabled schedules whose next-fire time has passed."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        due: list[Schedule] = []
        for schedule in self._cache.values():
            if not schedule.enabled:
                continue
            try:
                tz = ZoneInfo(schedule.timezone)
            except Exception:
                self._log.warning(
                    "scheduler.store.bad_timezone",
                    schedule_id=schedule.id,
                    timezone=schedule.timezone,
                )
                continue

            baseline = schedule.state.last_fired_at or schedule.created_at
            if baseline.tzinfo is None:
                baseline = baseline.replace(tzinfo=timezone.utc)
            baseline_local = baseline.astimezone(tz)

            try:
                itr = croniter(schedule.cron, baseline_local)
                next_fire_local = itr.get_next(datetime)
            except Exception as exc:
                self._log.warning(
                    "scheduler.store.bad_cron",
                    schedule_id=schedule.id,
                    cron=schedule.cron,
                    error=str(exc),
                )
                continue

            next_fire_utc = next_fire_local.astimezone(timezone.utc)
            if next_fire_utc <= now:
                due.append(schedule)
        return due
