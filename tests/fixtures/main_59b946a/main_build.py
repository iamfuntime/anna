"""A faithful mock of the build you roll back to (origin/main 59b946a): its ``schedules.yaml`` contract.

Reproduces, line for line, the parts of main's ``ScheduleStore`` and ``Scheduler._poll_once`` that decide
what happens to ``schedules.yaml`` and when a tick fires:

* ``load``  - ``Schedule.model_validate`` per record (unknown keys are IGNORED, i.e. dropped);
* ``save``  - rewrite the whole file from the models on EVERY state change;
* ``due_schedules`` - next cron tick after ``state.last_fired_at or created_at`` is <= now;
* ``poll_once`` - ``mark_dispatched(now)`` (``last_fired_at = now``) then run.

There is no model call: "running" a schedule only appends ``(id, tick_minute)`` to ``runs``. Source:
``git show 59b946a:src/anna/runtime/schedule_store.py`` / ``scheduler.py``. Data model: ``schedule_types.py``
next to this file (golden snapshot).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from croniter import croniter

_spec = importlib.util.spec_from_file_location("main_59b946a_schedule_types", Path(__file__).with_name("schedule_types.py"))
types = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["main_59b946a_schedule_types"] = types
_spec.loader.exec_module(types)  # type: ignore[union-attr]
Schedule, ScheduleState = types.Schedule, types.ScheduleState


class MainBuild:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cache: dict[str, Schedule] = {}
        self.runs: list[tuple[str, datetime]] = []

    def load(self) -> None:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.cache = {s.id: s for s in (Schedule.model_validate(r) for r in raw.get("schedules", []))}

    def save(self) -> None:
        payload = {"version": 1, "schedules": [s.model_dump(mode="json") for s in self.cache.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        tmp.replace(self.path)

    def due_schedules(self, now: datetime) -> list[Schedule]:
        due = []
        for s in self.cache.values():
            if not s.enabled:
                continue
            baseline = s.state.last_fired_at or s.created_at
            if baseline.tzinfo is None:
                baseline = baseline.replace(tzinfo=UTC)
            nxt = croniter(s.cron, baseline.astimezone(ZoneInfo(s.timezone))).get_next(datetime)
            if nxt.astimezone(UTC) <= now:
                due.append(s)
        return due

    def poll_once(self, now: datetime) -> list[str]:
        """One scheduler cycle at ``now`` (reload, dispatch what is due, persist). Returns the ids fired."""
        self.load()  # main reloads on every poll; a file the ledger build wrote is read through main's model
        fired = []
        for s in self.due_schedules(now):
            s.state = ScheduleState(last_fired_at=now, last_status=s.state.last_status,
                                    consecutive_failures=s.state.consecutive_failures)
            self.save()  # mark_dispatched
            self.runs.append((s.id, now.replace(second=0, microsecond=0)))
            s.state = ScheduleState(last_fired_at=now, last_status="complete", consecutive_failures=0)
            self.save()  # mark_fired on completion
            fired.append(s.id)
        return fired
