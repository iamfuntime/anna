"""Schedule run board read layer (MC-06).

Builds the display rows the ``/schedules`` board renders, off the same
:class:`anna_web.schedule_store_adapter.ScheduleStoreAdapter` the editor
routes and the dashboard's schedule-health panel already use. One row
per schedule: identity (id, cron, natural-language gloss, timezone,
destination), liveness (enabled), and run state (last fire, last
status, consecutive failures) plus the **computed next fire**.

Next-fire semantics deliberately mirror the daemon's
:meth:`anna.runtime.schedule_store.ScheduleStore.due_schedules` (the
scheduler's poll-cycle source of truth) so the board never disagrees
with what the daemon will actually do:

* Baseline is ``state.last_fired_at``, falling back to ``created_at``
  for first fires. A naive baseline is assumed UTC.
* The baseline converts into the schedule's declared timezone and
  croniter computes the next tick there; the result converts back to
  UTC for display. Note the next fire is the next tick *after the
  baseline*, not after "now" — exactly like ``due_schedules``, so an
  overdue schedule shows a next-fire in the past (it is due).
* Disabled schedules never fire → ``next_fire`` is ``None``.
* A bad timezone or bad cron (hand-edited ``schedules.yaml``) degrades
  that one row to ``next_fire = None`` — the daemon skips such rows
  with a warning; the board renders an em dash.

Fail-soft contract (shared across ``anna_web.readers``): no public
function raises into a route. A missing ``schedules.yaml`` is an empty
board; an unreadable/invalid one degrades to an empty board rather
than a 500.

Unlike the sibling file-tailing readers this module is not a
threadpool-wrapped class: the schedule data already arrives through the
async :class:`ScheduleStoreAdapter`, so :func:`load_board_rows` is a
plain coroutine and the per-row computation is pure CPU-trivial code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from croniter import croniter

from anna.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anna.runtime.schedule_types import Schedule
    from anna_web.schedule_store_adapter import ScheduleStoreAdapter

# Rendered wherever a value is absent/uncomputable (never fired, bad
# cron, disabled schedule's next fire).
EMPTY_VALUE = "—"  # em dash

_log = get_logger("anna.web.schedule_board")

__all__ = [
    "EMPTY_VALUE",
    "ScheduleBoardRow",
    "build_row",
    "compute_next_fire",
    "load_board_rows",
]


@dataclass(frozen=True)
class ScheduleBoardRow:
    """One display row of the schedule run board.

    Raw datetimes (UTC-aware) ride alongside their pre-formatted
    display strings so templates stay dumb and tests can pin either the
    computation or the rendering.
    """

    id: str
    cron: str
    natural_language: str | None
    timezone: str
    destination: str  # "<transport>:<channel>", the board's display form
    enabled: bool
    last_fired_at: datetime | None
    last_fired_display: str
    last_status: str | None  # "complete" | "fail" | "timeout" | None
    consecutive_failures: int
    next_fire: datetime | None  # UTC; None when disabled/uncomputable
    next_fire_display: str

    @property
    def failing(self) -> bool:
        """True when the row should carry the failure accent."""
        return self.consecutive_failures > 0


def compute_next_fire(schedule: "Schedule") -> datetime | None:
    """Next fire time in UTC, or ``None`` when there is none to compute.

    Mirrors ``ScheduleStore.due_schedules`` exactly — see the module
    docstring for the shared semantics. Never raises: a bad timezone or
    cron expression returns ``None`` for that row.
    """
    if not schedule.enabled:
        return None
    try:
        tz = ZoneInfo(schedule.timezone)
    except Exception:
        _log.warning(
            "schedule_board.bad_timezone",
            schedule_id=schedule.id,
            timezone=schedule.timezone,
        )
        return None

    baseline = schedule.state.last_fired_at or schedule.created_at
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=timezone.utc)
    baseline_local = baseline.astimezone(tz)

    try:
        itr = croniter(schedule.cron, baseline_local)
        next_fire_local = itr.get_next(datetime)
    except Exception as exc:
        _log.warning(
            "schedule_board.bad_cron",
            schedule_id=schedule.id,
            cron=schedule.cron,
            error=str(exc),
        )
        return None

    return next_fire_local.astimezone(timezone.utc)


def _display(dt: datetime | None) -> str:
    """Mono-friendly UTC render, em dash for absent values."""
    if dt is None:
        return EMPTY_VALUE
    if dt.tzinfo is None:
        # Hand-edited YAML may carry naive timestamps; the daemon treats
        # them as UTC, so display must too.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_row(schedule: "Schedule") -> ScheduleBoardRow:
    """Build one board row from a :class:`Schedule`. Never raises."""
    next_fire = compute_next_fire(schedule)
    last_fired = schedule.state.last_fired_at
    return ScheduleBoardRow(
        id=schedule.id,
        cron=schedule.cron,
        natural_language=schedule.natural_language,
        timezone=schedule.timezone,
        destination=(
            f"{schedule.destination.transport}:{schedule.destination.channel}"
        ),
        enabled=schedule.enabled,
        last_fired_at=last_fired,
        last_fired_display=_display(last_fired),
        last_status=schedule.state.last_status,
        consecutive_failures=schedule.state.consecutive_failures,
        next_fire=next_fire,
        next_fire_display=_display(next_fire),
    )


async def load_board_rows(store: "ScheduleStoreAdapter") -> list[ScheduleBoardRow]:
    """All board rows, in store order. Fail-soft: any read failure → ``[]``.

    ``ScheduleStoreAdapter.list_all`` reloads ``schedules.yaml`` on
    every call; a missing file is already an empty list at that layer,
    while malformed YAML or an invalid record raises — caught here so a
    hand-mangled file degrades the board to its empty state instead of
    a 500 on every 10s poll.
    """
    try:
        schedules = await store.list_all()
    except Exception:
        _log.warning("schedule_board.load_failed", exc_info=True)
        return []
    return [build_row(s) for s in schedules]
