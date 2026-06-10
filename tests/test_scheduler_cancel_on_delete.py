"""Cancel-on-delete coverage for the Phase 2 scheduler.

When the operator deletes a schedule, ``ScheduleStore.delete`` invokes the
cancel callback (wired in ``__main__`` to
:meth:`Scheduler.cancel_schedule_tasks`) so queued and in-flight fire tasks
are cancelled instead of waking later, calling ``fire()`` on a schedule that
no longer exists, and emitting a noisy ``audit.schedule.fail`` with
"does not exist" — the 2026-06-01 morning-brief-test incident tail.

Covers the TaskNote acceptance tests:
- slow fire + delete mid-run → in-flight task cancelled, no
  ``audit.schedule.complete`` for the cancelled fire, no
  ``audit.schedule.fail`` with "does not exist";
- multiple tasks queued on the ``max_concurrent`` semaphore + delete → all
  cancelled, no fails surface.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


class _RoutingAdapter(ChannelAdapter):
    """Captures sent OutboundMessages so the test can confirm (non-)delivery."""

    name = "slack"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:  # pragma: no cover
        return

    async def stop(self) -> None:  # pragma: no cover
        return

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def subscribe(self, handler) -> None:  # pragma: no cover
        return

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    @classmethod
    def conversation_key_for(cls, event):  # pragma: no cover
        return "x"


class _NeverResolvingRouter:
    """Stand-in router whose dispatch never resolves the completion future.

    ``Scheduler.fire`` blocks on ``await completion`` forever, modeling a
    slow in-flight run — the only way it lands is via cancellation.
    """

    def __init__(self) -> None:
        self.dispatched: list[InboundEvent] = []

    async def dispatch(self, event: InboundEvent) -> None:
        self.dispatched.append(event)
        # Deliberately never resolve event.completion_future.


class _NoopAlerter:
    async def warn(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True

    async def critical(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True

    async def notify_startup(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.scheduler.poll_interval_seconds = 0
    cfg.scheduler.failure_threshold = 3
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _make_scheduler(
    cfg: AnnaConfig,
) -> tuple[Scheduler, ScheduleStore, _NeverResolvingRouter, _RoutingAdapter]:
    supervisor = Supervisor(config=cfg)
    store = ScheduleStore(config=cfg, supervisor=supervisor)
    adapter = _RoutingAdapter()
    router = _NeverResolvingRouter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=_NoopAlerter(),  # type: ignore[arg-type]
    )
    # Mirror the __main__ wiring: deletion cancels the schedule's fire tasks.
    store.set_cancel_callback(sched.cancel_schedule_tasks)
    return sched, store, router, adapter


def _make_schedule(*, id: str) -> Schedule:
    # Backdated created_at so cron "* * * * *" with last_fired_at=None is
    # immediately due on the first poll (same trick as the run-loop test).
    return Schedule(
        id=id,
        cron="* * * * *",
        timezone="UTC",
        prompt="slow brief",
        destination=ScheduleDestination(transport="slack", channel="CCANCEL"),
        timeout_seconds=300,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )


def _read_audit(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not audit_dir.exists():
        return out
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


async def _wait_for(predicate, *, attempts: int = 200, delay: float = 0.005) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_delete_mid_run_cancels_inflight_fire(tmp_path: Path) -> None:
    """Slow fire + delete mid-run: the in-flight task is cancelled, and the
    cancelled fire records neither a complete nor a does-not-exist fail."""
    cfg = _make_config(tmp_path)
    sched, store, router, adapter = _make_scheduler(cfg)
    await store.load()
    await store.create(_make_schedule(id="slow-brief"))

    # Dispatch the due schedule and wait until the fire is genuinely
    # in-flight (the router saw the synthetic event and is "running").
    await sched._poll_once()
    await _wait_for(lambda: router.dispatched)
    tasks = list(sched._tasks_by_schedule["slow-brief"])
    assert len(tasks) == 1
    assert not tasks[0].done()

    # Operator deletes the schedule mid-run.
    await store.delete("slow-brief")

    assert all(t.cancelled() for t in tasks)
    # Done-callbacks run via call_soon; give the loop a tick, then both
    # indexes must be clean.
    await _wait_for(lambda: "slow-brief" not in sched._tasks_by_schedule)
    assert not sched._inflight

    # Nothing was posted to the destination, and no zombie fires later: the
    # store no longer knows the schedule, so further polls dispatch nothing.
    assert adapter.sent == []
    await sched._poll_once()
    await asyncio.sleep(0.05)
    assert len(router.dispatched) == 1

    audits = _read_audit(cfg.audit_dir)
    event_names = [a.get("event") for a in audits]
    assert "audit.schedule.complete" not in event_names
    assert not any(
        a.get("event") == "audit.schedule.fail"
        and "does not exist" in a.get("reason", "")
        for a in audits
    )
    assert "audit.schedule.deleted" in event_names
    cancelled = [a for a in audits if a.get("event") == "audit.schedule.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["schedule_id"] == "slow-brief"
    assert cancelled[0]["cancelled"] == 1


@pytest.mark.asyncio
async def test_delete_cancels_all_queued_tasks(tmp_path: Path) -> None:
    """Multiple tasks parked on the max_concurrent semaphore + delete: all
    are cancelled, and no audit.schedule.fail surfaces afterwards."""
    cfg = _make_config(tmp_path)
    cfg.scheduler.max_concurrent = 1  # one slot: extra fires queue behind it
    sched, store, router, adapter = _make_scheduler(cfg)
    await store.load()
    await store.create(_make_schedule(id="victim"))

    # First dispatch occupies the single semaphore slot and never finishes.
    await sched._poll_once()
    await _wait_for(lambda: router.dispatched)

    # Re-dispatch twice more by resetting fire state, reproducing the
    # incident shape: leftover tasks queued on the semaphore for a schedule
    # about to be deleted.
    for _ in range(2):
        schedule = store.get("victim")
        assert schedule is not None
        schedule.state = ScheduleState()
        await sched._poll_once()
    await asyncio.sleep(0.02)  # let queued tasks start and park on the semaphore

    tasks = list(sched._tasks_by_schedule["victim"])
    assert len(tasks) == 3
    assert len(router.dispatched) == 1  # only the slot-holder reached fire()

    await store.delete("victim")

    assert all(t.cancelled() for t in tasks)
    await _wait_for(lambda: "victim" not in sched._tasks_by_schedule)
    assert not sched._inflight
    assert adapter.sent == []

    # Give any would-be zombie a chance to wake and fail loudly — none may.
    await asyncio.sleep(0.05)
    audits = _read_audit(cfg.audit_dir)
    event_names = [a.get("event") for a in audits]
    assert "audit.schedule.fail" not in event_names
    assert "audit.schedule.complete" not in event_names
    cancelled = [a for a in audits if a.get("event") == "audit.schedule.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["schedule_id"] == "victim"
    assert cancelled[0]["cancelled"] == 3
    assert cancelled[0]["still_pending"] == 0
