"""End-to-end integration test for the Phase 2 scheduler.

Uses a real :class:`SelfEditTools` to create schedules through the MCP path,
a real :class:`ScheduleStore` that writes its own YAML, and a real
:class:`Scheduler` with its `run()` loop. The router boundary is mocked
because a real router would spawn a worker holding a ClaudeSDKClient, which
needs a live model session that we cannot wire up in CI.

This is the §14 integration gate for the Phase 2 scheduler buildout. The
Day-1 operator test (live ANNA, real MCP tool invocation, real Slack post)
is the operator's job after this buildout merges.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from anna.agents.registry import SubAgentRegistry
from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from anna.skills.registry import SkillRegistry
from anna.tools.self_edit_server import SelfEditTools
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


class _RoutingAdapter(ChannelAdapter):
    """Captures sent OutboundMessages so the test can confirm delivery."""

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


class _ResolvingRouter:
    """Stand-in for ConversationRouter: resolves completion_future with a reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.dispatched: list[InboundEvent] = []

    async def dispatch(self, event: InboundEvent) -> None:
        self.dispatched.append(event)
        if event.completion_future is not None and not event.completion_future.done():
            event.completion_future.set_result(self.reply)


class _NoopAlerter:
    async def warn(self, *_a, **_kw) -> bool:
        return True

    async def critical(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True

    async def notify_startup(self, *_a, **_kw) -> bool:  # pragma: no cover
        return True


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.scheduler.poll_interval_seconds = 0  # tight loop for the test
    cfg.scheduler.failure_threshold = 3
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _make_self_edit_tools(cfg: AnnaConfig, store: ScheduleStore, supervisor: Supervisor) -> SelfEditTools:
    return SelfEditTools(
        config=cfg,
        supervisor=supervisor,
        agents_registry=SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=cfg.anna_home / "agents",
            audit_dir=cfg.audit_dir,
            fsync_on_write=False,
        ),
        skills_registry=SkillRegistry(
            supervisor=supervisor,
            skills_dir=cfg.anna_home / "skills",
            audit_dir=cfg.audit_dir,
            fsync_on_write=False,
        ),
        schedule_store=store,
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


@pytest.mark.asyncio
async def test_end_to_end_create_then_fire(tmp_path: Path) -> None:
    """Operator creates a schedule via MCP, the loop fires it, output lands."""
    cfg = _make_config(tmp_path)
    supervisor = Supervisor(config=cfg)
    store = ScheduleStore(config=cfg, supervisor=supervisor)
    await store.load()

    tools = _make_self_edit_tools(cfg, store, supervisor)
    adapter = _RoutingAdapter()
    router = _ResolvingRouter(reply="overnight: nothing exploded; calendar: clear")
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=_NoopAlerter(),  # type: ignore[arg-type]
    )

    # Step 1: operator creates the schedule via the MCP tool.
    await tools.schedule_create(
        id="morning-brief",
        prompt="Compose a morning brief.",
        destination_transport="slack",
        destination_channel="C0AFD2LM38R",
        natural_language="every morning at 6am",
        creator_conv="slack:dm:UTEST",
    )

    # The schedule was persisted to YAML.
    on_disk = yaml.safe_load((tmp_path / "schedules.yaml").read_text(encoding="utf-8"))
    assert any(s["id"] == "morning-brief" for s in on_disk["schedules"])
    assert any(s["cron"] == "0 6 * * *" for s in on_disk["schedules"])

    # Step 2: drive a fire directly (the run loop would do this when the
    # cron time arrives; we exercise the same code path without waiting).
    await sched._guarded_fire(store.get("morning-brief"))  # type: ignore[arg-type]

    # Step 3: the worker's reply reached the destination adapter.
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "overnight: nothing exploded; calendar: clear"
    assert adapter.sent[0].conversation_key == "slack:dm:C0AFD2LM38R"

    # Step 4: state mutated on the schedule.
    s = store.get("morning-brief")
    assert s is not None
    assert s.state.last_status == "complete"
    assert s.state.consecutive_failures == 0

    # Step 5: audit log captured create + fire + complete.
    audits = _read_audit(cfg.audit_dir)
    event_names = [a.get("event") for a in audits]
    assert "audit.schedule.created" in event_names
    assert "audit.schedule.fire" in event_names
    assert "audit.schedule.complete" in event_names

    # Step 6: reload the store from disk; state survives.
    store2 = ScheduleStore(config=cfg, supervisor=supervisor)
    await store2.load()
    s2 = store2.get("morning-brief")
    assert s2 is not None
    assert s2.state.last_status == "complete"


@pytest.mark.asyncio
async def test_run_loop_picks_up_due_schedule(tmp_path: Path) -> None:
    """The Scheduler.run() coroutine actually fires a due schedule when polled."""
    from datetime import datetime, timezone as tz_module

    from anna.runtime.schedule_types import Schedule, ScheduleDestination

    cfg = _make_config(tmp_path)
    cfg.scheduler.poll_interval_seconds = 0  # poll continuously
    supervisor = Supervisor(config=cfg)
    store = ScheduleStore(config=cfg, supervisor=supervisor)
    adapter = _RoutingAdapter()
    router = _ResolvingRouter(reply="run-loop reply")
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=_NoopAlerter(),  # type: ignore[arg-type]
    )

    # Backdate created_at to 2020 so cron "* * * * *" against last-fired = None
    # falls back to created_at, and the next-fire time is comfortably in the
    # past — the run loop's first poll cycle picks it up immediately.
    await store.create(
        Schedule(
            id="poll-target",
            cron="* * * * *",
            timezone="UTC",
            prompt="ping",
            destination=ScheduleDestination(transport="slack", channel="C0POLL"),
            created_at=datetime(2020, 1, 1, tzinfo=tz_module.utc),
        )
    )

    run_task = asyncio.create_task(sched.run())
    # Give the loop a moment to poll, fire, and resolve.
    for _ in range(60):
        await asyncio.sleep(0.025)
        if adapter.sent:
            break
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass

    assert adapter.sent, "run loop never produced a sent message"
    assert adapter.sent[0].text == "run-loop reply"
    assert adapter.sent[0].conversation_key == "slack:dm:C0POLL"
