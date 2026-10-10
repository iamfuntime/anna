"""Pydantic models for the Phase 2 scheduler.

These types describe the on-disk shape of ``schedules.yaml`` and the
in-memory shape the scheduler and the MCP tools pass around. ``Schedule``
round-trips cleanly through ``model_dump`` / ``model_validate`` so the
store can serialize directly to YAML.

A ``Schedule`` always carries a resolved 5-field ``cron`` expression
(croniter operates on it). ``natural_language`` is the human-typed
string the operator passed at create time, preserved alongside the
resolved cron for round-trip readability. If the operator passes
``cron:`` directly, ``natural_language`` stays None.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ScheduleDestination(BaseModel):
    """Where a scheduled task's output goes.

    ``transport`` is the adapter name (``slack`` or ``telegram`` in
    Phase 2). ``channel`` is the channel ID (Slack) or chat ID
    (Telegram) the output posts to. Reserved-destination validation
    (forbidding the admin channel) lives in the ScheduleStore, not on
    this model, because it needs the AdminConfig to compare against.
    """

    transport: Literal["slack", "telegram"]
    channel: str


class ScheduleState(BaseModel):
    """Mutable per-schedule state, updated by the scheduler at runtime.

    ``consecutive_failures`` resets to zero on every success and
    increments on every failure. At ``config.scheduler.failure_threshold``
    the schedule auto-disables.
    """

    last_fired_at: datetime | None = None
    last_status: Literal["complete", "fail", "timeout"] | None = None
    consecutive_failures: int = 0


class Schedule(BaseModel):
    """A persistent scheduled task.

    The runtime polls the schedule store, identifies schedules whose
    next-fire time has passed (per ``cron`` plus ``timezone`` against
    ``state.last_fired_at`` or ``created_at`` for first fires), and
    dispatches each as a synthetic ``InboundEvent`` through the
    conversation router. The worker's final reply routes to
    ``destination`` via the corresponding ChannelAdapter.

    ``ephemeral`` (default False) opts the schedule into ephemeral
    fires: the dispatched event sets ``InboundEvent.ephemeral`` so the
    worker skips the resume-context injection (recent-checkpoints block
    and unsaved conversation tail) that otherwise accumulates across the
    per-day conv_key of a frequent heartbeat schedule. Omitted in legacy
    ``schedules.yaml`` entries, where it defaults to False — preserving
    the existing non-ephemeral behavior for every schedule that does not
    opt in.
    """

    id: str
    natural_language: str | None = None
    cron: str
    timezone: str = "America/New_York"
    prompt: str
    destination: ScheduleDestination
    timeout_seconds: int = 300
    enabled: bool = True
    ephemeral: bool = False
    # What the scheduler executes. Only the legacy agent prompt exists today;
    # typed deterministic jobs (heartbeat) register further kinds.
    job_kind: Literal["agent_prompt", "google_heartbeat"] = "agent_prompt"
    # Crash-recovery safety. ``side_effecting`` runs interrupted mid-flight are
    # recorded as unknown and never re-run; ``read_only`` runs may be retried.
    run_safety: Literal["side_effecting", "read_only"] = "side_effecting"
    # None inherits scheduler.default_* from config.
    overlap_policy: Literal["skip", "queue"] | None = None
    misfire_policy: Literal["run_latest", "skip_late"] | None = None
    misfire_grace_seconds: int | None = None
    created_at: datetime
    state: ScheduleState = Field(default_factory=ScheduleState)

    @model_validator(mode="after")
    def _heartbeat_is_read_only(self) -> "Schedule":
        # The built-in heartbeat only reads; an interrupted run is safe to repeat.
        if self.job_kind == "google_heartbeat" and self.run_safety != "read_only":
            self.run_safety = "read_only"
        return self
