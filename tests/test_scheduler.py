"""Tests for the Phase 2 scheduler runtime."""

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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRouter:
    """A drop-in for ConversationRouter that resolves the completion future
    with a configured reply (or exception) without spawning a real worker."""

    def __init__(self) -> None:
        self.dispatched: list[InboundEvent] = []
        self.reply: str = "OK, scheduled output."
        self.raise_on_dispatch: Exception | None = None
        self.delay_before_resolve: float = 0.0

    async def dispatch(self, event: InboundEvent) -> None:
        self.dispatched.append(event)
        if self.raise_on_dispatch is not None:
            if event.completion_future is not None and not event.completion_future.done():
                event.completion_future.set_exception(self.raise_on_dispatch)
            return
        if self.delay_before_resolve > 0:
            await asyncio.sleep(self.delay_before_resolve)
        if event.completion_future is not None and not event.completion_future.done():
            event.completion_future.set_result(self.reply)


class FakeAdapter(ChannelAdapter):
    name = "fake-slack"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.send_should_raise: Exception | None = None

    async def start(self) -> None:  # pragma: no cover - not exercised
        return

    async def stop(self) -> None:  # pragma: no cover
        return

    async def send(self, message: OutboundMessage) -> None:
        if self.send_should_raise is not None:
            raise self.send_should_raise
        self.sent.append(message)

    def subscribe(self, handler) -> None:  # pragma: no cover
        return

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    @classmethod
    def conversation_key_for(cls, event):  # pragma: no cover
        return "fake"


class FakeAlerter:
    def __init__(self) -> None:
        self.warned: list[str] = []

    async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warned.append(message)
        return True

    async def critical(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warned.append(message)
        return True

    async def notify_startup(self, message: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.scheduler.poll_interval_seconds = 1
    cfg.scheduler.max_concurrent = 3
    cfg.scheduler.failure_threshold = 3
    cfg.logging.audit.fsync_on_write = False
    cfg.admin.slack_channel_id = "CADMIN"
    return cfg


async def _make_store_with_schedule(
    tmp_path: Path,
    *,
    schedule: Schedule | None = None,
) -> tuple[AnnaConfig, ScheduleStore]:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    if schedule is not None:
        await store.create(schedule)
    return cfg, store


def _make_schedule(
    *,
    id: str = "morning-brief",
    transport: str = "slack",
    channel: str = "CMORN",
    timeout_seconds: int = 300,
    enabled: bool = True,
    state: ScheduleState | None = None,
) -> Schedule:
    return Schedule(
        id=id,
        cron="0 6 * * *",
        prompt="Compose a morning brief.",
        destination=ScheduleDestination(transport=transport, channel=channel),  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
        enabled=enabled,
        created_at=datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc),
        state=state or ScheduleState(),
    )


def _read_audit_records(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not audit_dir.exists():
        return out
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _events(records: list[dict], name: str) -> list[dict]:
    return [r for r in records if r.get("event") == name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_builds_event_with_completion_future(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(tmp_path, schedule=_make_schedule())
    router = FakeRouter()
    adapter = FakeAdapter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )

    reply = await sched.fire("morning-brief")
    assert reply == "OK, scheduled output."
    assert len(router.dispatched) == 1
    event = router.dispatched[0]
    assert event.transport == "slack"
    assert event.conversation_key.startswith("schedule:morning-brief:")
    assert event.text == "Compose a morning brief."
    assert event.completion_future is not None


@pytest.mark.asyncio
async def test_fire_unknown_id_raises(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(tmp_path)
    sched = Scheduler(
        config=cfg,
        store=store,
        router=FakeRouter(),  # type: ignore[arg-type]
        adapters={},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match=r"does not exist"):
        await sched.fire("nope")


@pytest.mark.asyncio
async def test_guarded_fire_happy_path(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(tmp_path, schedule=_make_schedule())
    router = FakeRouter()
    router.reply = "Morning brief: nothing exploded overnight."
    adapter = FakeAdapter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )

    await sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Morning brief: nothing exploded overnight."
    assert adapter.sent[0].conversation_key == "slack:dm:CMORN"

    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.fire")
    assert _events(audits, "audit.schedule.complete")
    assert not _events(audits, "audit.schedule.fail")

    state = store.get("morning-brief").state  # type: ignore[union-attr]
    assert state.last_status == "complete"
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_guarded_fire_timeout_increments_failures(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(
        tmp_path, schedule=_make_schedule(timeout_seconds=1)
    )
    router = FakeRouter()
    router.delay_before_resolve = 5.0  # > timeout
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": FakeAdapter()},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )

    await sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]

    audits = _read_audit_records(cfg.audit_dir)
    fails = _events(audits, "audit.schedule.fail")
    assert fails and fails[0]["kind"] == "timeout"

    state = store.get("morning-brief").state  # type: ignore[union-attr]
    assert state.consecutive_failures == 1
    assert state.last_status == "timeout"


@pytest.mark.asyncio
async def test_guarded_fire_worker_exception(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(tmp_path, schedule=_make_schedule())
    router = FakeRouter()
    router.raise_on_dispatch = RuntimeError("kaboom")
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": FakeAdapter()},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )

    await sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]

    audits = _read_audit_records(cfg.audit_dir)
    fails = _events(audits, "audit.schedule.fail")
    assert fails and "kaboom" in fails[0]["reason"]
    state = store.get("morning-brief").state  # type: ignore[union-attr]
    assert state.consecutive_failures == 1


@pytest.mark.asyncio
async def test_guarded_fire_send_failure(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(tmp_path, schedule=_make_schedule())
    router = FakeRouter()
    adapter = FakeAdapter()
    adapter.send_should_raise = RuntimeError("network down")
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )

    await sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]

    audits = _read_audit_records(cfg.audit_dir)
    fails = _events(audits, "audit.schedule.fail")
    assert fails and "network down" in fails[0]["reason"]


@pytest.mark.asyncio
async def test_three_failures_auto_disable_and_alert(tmp_path: Path) -> None:
    cfg, store = await _make_store_with_schedule(tmp_path, schedule=_make_schedule())
    router = FakeRouter()
    router.raise_on_dispatch = RuntimeError("nope")
    alerter = FakeAlerter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": FakeAdapter()},
        alerter=alerter,  # type: ignore[arg-type]
    )

    for _ in range(3):
        await sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]

    # Wait briefly so the spawned alerter.warn task lands.
    await asyncio.sleep(0.05)

    schedule = store.get("morning-brief")
    assert schedule is not None
    assert schedule.enabled is False

    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.disabled")
    assert len(alerter.warned) >= 1
    assert "disabled" in alerter.warned[0].lower()


@pytest.mark.asyncio
async def test_disabled_schedules_dropped_from_due(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule(enabled=False))
    assert store.due_schedules(datetime(2030, 1, 1, tzinfo=timezone.utc)) == []


@pytest.mark.asyncio
async def test_poll_once_fires_due_schedules(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    # Schedule was created in the distant past; cron */1 minute makes it due.
    await store.create(
        Schedule(
            id="poll-test",
            cron="*/1 * * * *",
            timezone="UTC",
            prompt="x",
            destination=ScheduleDestination(transport="slack", channel="C1"),
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    router = FakeRouter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": FakeAdapter()},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )

    await sched._poll_once()
    # Give the spawned task a moment to land.
    await asyncio.gather(*list(sched._inflight), return_exceptions=True)

    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.fire")
    assert _events(audits, "audit.schedule.complete")


@pytest.mark.asyncio
async def test_dest_conv_key_format() -> None:
    assert (
        Scheduler._dest_conv_key(transport="slack", channel="C0AFD2LM38R")
        == "slack:dm:C0AFD2LM38R"
    )
    assert (
        Scheduler._dest_conv_key(transport="telegram", channel="993947726")
        == "telegram:dm:993947726"
    )


@pytest.mark.asyncio
async def test_shutdown_drains_inflight(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    router = FakeRouter()
    router.delay_before_resolve = 0.05
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": FakeAdapter()},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )
    sched._shutdown_grace_seconds = 1.0

    task = asyncio.create_task(
        sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]
    )
    sched._inflight.add(task)
    task.add_done_callback(sched._inflight.discard)

    await sched.shutdown()
    assert task.done() or task.cancelled()
