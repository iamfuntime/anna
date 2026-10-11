"""Per-schedule model pin (``Schedule.model``).

Pins the contract end to end:

* ``Schedule.model`` validates with the same grammar as ``runtime.model``
  and round-trips through ``schedules.yaml`` (legacy entries load as None).
* ``Scheduler.fire`` carries it on the dispatched ``InboundEvent``.
* The router binds it to the fire's own worker at spawn, and never to a
  DM / conversation worker.
* The worker's SDK options use it in place of ``runtime.model``; with no
  override the options are exactly ``runtime.model`` / ``fallback_model``.
* A fallback identical to the override is dropped (the CLI rejects it).
* The self-edit ``schedule_update`` tool leaves an existing pin alone,
  can set it, and can clear it; ``schedule_create`` accepts it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import anna.runtime.router as router_mod
from anna.config import AnnaConfig
from anna.runtime.router import ConversationRouter
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker
from anna.transports.base import InboundEvent


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.logging.audit.fsync_on_write = False
    cfg.admin.slack_channel_id = "CADMIN"
    cfg.runtime.model = "claude-opus-5-5"
    cfg.runtime.fallback_model = "claude-opus-5"
    return cfg


def _schedule(*, model: str | None = None, id: str = "heartbeat") -> Schedule:
    return Schedule(
        id=id,
        cron="0 7-19 * * *",
        prompt="Run the heartbeat check.",
        destination=ScheduleDestination(transport="slack", channel="USP2QLB41"),
        ephemeral=True,
        gate_command=["/bin/true"],
        model=model,
        created_at=datetime(2026, 6, 5, 14, 30, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Schedule type + yaml round-trip
# ---------------------------------------------------------------------------


def test_model_defaults_to_none() -> None:
    assert _schedule().model is None


@pytest.mark.parametrize("value", ["sonnet", "claude-sonnet-4-5", "opus"])
def test_model_accepts_config_grammar(value: str) -> None:
    assert _schedule(model=value).model == value


@pytest.mark.parametrize("value", ["Sonnet 4", "gpt-5", ""])
def test_model_rejects_malformed(value: str) -> None:
    with pytest.raises(ValidationError):
        _schedule(model=value)


@pytest.mark.asyncio
async def test_yaml_round_trip_preserves_model(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_schedule(model="sonnet"))
    await store.create(_schedule(id="morning-brief"))

    on_disk = yaml.safe_load(store.path.read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in on_disk["schedules"]}
    assert rows["heartbeat"]["model"] == "sonnet"
    assert rows["morning-brief"]["model"] is None

    # A state write (what every fire does) must not drop the field.
    await store.mark_dispatched("heartbeat", when=datetime.now(UTC))
    reloaded = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await reloaded.load()
    hb = reloaded.get("heartbeat")
    assert hb is not None
    assert hb.model == "sonnet"
    # Neighbouring fields survive untouched.
    assert hb.ephemeral is True
    assert hb.gate_command == ["/bin/true"]
    assert hb.cron == "0 7-19 * * *"


@pytest.mark.asyncio
async def test_legacy_yaml_without_model_loads_as_none(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    legacy = _schedule().model_dump(mode="json")
    legacy.pop("model")
    Path(cfg.scheduler.state_path).write_text(
        yaml.safe_dump({"version": 1, "schedules": [legacy]}), encoding="utf-8"
    )
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.load()
    assert store.get("heartbeat").model is None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Scheduler -> event
# ---------------------------------------------------------------------------


class _CaptureRouter:
    def __init__(self) -> None:
        self.dispatched: list[InboundEvent] = []

    async def dispatch(self, event: InboundEvent) -> None:
        self.dispatched.append(event)
        assert event.completion_future is not None
        event.completion_future.set_result("ok")


async def _fire(tmp_path: Path, schedule: Schedule) -> InboundEvent:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(schedule)
    router = _CaptureRouter()
    sched = Scheduler(
        config=cfg, store=store, router=router, adapters={}, alerter=None  # type: ignore[arg-type]
    )
    await sched.fire(schedule.id)
    return router.dispatched[0]


@pytest.mark.asyncio
async def test_fire_carries_schedule_model(tmp_path: Path) -> None:
    event = await _fire(tmp_path, _schedule(model="sonnet"))
    assert event.model == "sonnet"


@pytest.mark.asyncio
async def test_fire_without_model_carries_none(tmp_path: Path) -> None:
    event = await _fire(tmp_path, _schedule())
    assert event.model is None


# ---------------------------------------------------------------------------
# Router binds the override to the fire's worker only
# ---------------------------------------------------------------------------


class _RecordingWorker:
    instances: list[_RecordingWorker] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.conversation_key = kwargs["conversation_key"]
        self.transport = kwargs["transport"]
        self.model_override = kwargs.get("model_override")
        self.submitted: list[InboundEvent] = []
        _RecordingWorker.instances.append(self)

    async def start(self) -> None:
        return None

    async def submit(self, event: InboundEvent) -> None:
        self.submitted.append(event)

    async def stop(self) -> None:
        return None


def _event(conv_key: str, *, model: str | None = None, schedule_id: str | None = None) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=conv_key,
        sender_id="anna.scheduler" if schedule_id else "UOTHER",
        sender_display="x",
        text="hi",
        is_dm=schedule_id is None,
        is_thread=False,
        raw={"schedule_id": schedule_id} if schedule_id else {},
        model=model,
    )


@pytest.fixture
def router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConversationRouter:
    _RecordingWorker.instances = []
    monkeypatch.setattr(router_mod, "ConversationWorker", _RecordingWorker)
    cfg = _make_config(tmp_path)
    r = ConversationRouter(
        config=cfg,
        supervisor=Supervisor(config=cfg),
        adapters={"slack": object()},  # type: ignore[dict-item]
    )
    monkeypatch.setattr(r, "_build_visibility_callbacks", lambda _t: None)
    return r


@pytest.mark.asyncio
async def test_router_binds_model_to_schedule_worker(router: ConversationRouter) -> None:
    await router._get_or_spawn_worker(
        _event("schedule:heartbeat:2026-09-28", model="sonnet", schedule_id="heartbeat")
    )
    assert _RecordingWorker.instances[0].kwargs["model_override"] == "sonnet"


@pytest.mark.asyncio
async def test_persistent_session_unaffected_by_schedule_model(
    router: ConversationRouter,
) -> None:
    await router._get_or_spawn_worker(
        _event("schedule:heartbeat:2026-09-28", model="sonnet", schedule_id="heartbeat")
    )
    dm = await router._get_or_spawn_worker(_event("slack:dm:USP2QLB41"))
    assert dm.kwargs["model_override"] is None  # type: ignore[attr-defined]
    # The shared config was never touched.
    assert router._config.runtime.model == "claude-opus-5-5"


@pytest.mark.asyncio
async def test_reused_worker_keeps_bound_model_for_background_completion(
    router: ConversationRouter,
) -> None:
    """A background completion (no model, no schedule_id) on the schedule's
    conv_key reuses the pinned worker rather than respawning it."""
    key = "schedule:heartbeat:2026-09-28"
    first = await router._get_or_spawn_worker(
        _event(key, model="sonnet", schedule_id="heartbeat")
    )
    again = await router._get_or_spawn_worker(_event(key))
    assert again is first
    assert len(_RecordingWorker.instances) == 1


# ---------------------------------------------------------------------------
# Worker options
# ---------------------------------------------------------------------------


def _worker(tmp_path: Path, *, model_override: str | None = None, key: str = "slack:dm:U1") -> ConversationWorker:
    cfg = _make_config(tmp_path)

    async def _noop_send(_msg: Any) -> None:
        return None

    return ConversationWorker(
        conversation_key=key,
        transport="slack",
        config=cfg,
        supervisor=Supervisor(config=cfg),
        send=_noop_send,
        model_override=model_override,
    )


def test_worker_without_override_uses_runtime_models(tmp_path: Path) -> None:
    options = _worker(tmp_path)._build_options()
    assert options.model == "claude-opus-5-5"
    assert options.fallback_model == "claude-opus-5"


def test_worker_with_override_uses_it_and_keeps_global_fallback(tmp_path: Path) -> None:
    worker = _worker(tmp_path, model_override="sonnet", key="schedule:heartbeat:2026-09-28")
    options = worker._build_options()
    assert options.model == "sonnet"
    assert options.fallback_model == "claude-opus-5"
    # Global config untouched.
    assert worker._config.runtime.model == "claude-opus-5-5"


def test_worker_override_equal_to_fallback_drops_fallback(tmp_path: Path) -> None:
    worker = _worker(tmp_path, model_override="claude-opus-5")
    options = worker._build_options()
    assert options.model == "claude-opus-5"
    assert options.fallback_model is None


# ---------------------------------------------------------------------------
# Self-edit MCP tools
# ---------------------------------------------------------------------------


def _tools(tmp_path: Path):
    from anna.agents.registry import SubAgentRegistry
    from anna.skills.registry import SkillRegistry
    from anna.tools.self_edit_server import SelfEditTools

    cfg = _make_config(tmp_path)
    supervisor = Supervisor(config=cfg)
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
        schedule_store=ScheduleStore(config=cfg, supervisor=supervisor),
    )


@pytest.mark.asyncio
async def test_schedule_update_preserves_set_and_clears_model(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    store = tools.schedule_store
    await store.create(_schedule(model="sonnet"))

    # Unrelated update leaves the pin alone.
    await tools.schedule_update(id="heartbeat", creator_conv="t", enabled=False)
    assert store.get("heartbeat").model == "sonnet"
    assert store.get("heartbeat").gate_command == ["/bin/true"]

    await tools.schedule_update(id="heartbeat", creator_conv="t", model="opus")
    assert store.get("heartbeat").model == "opus"

    await tools.schedule_update(id="heartbeat", creator_conv="t", model="")
    assert store.get("heartbeat").model is None

    listing = await tools.schedule_list()
    assert "model=" not in listing["content"][0]["text"]
    await tools.schedule_update(id="heartbeat", creator_conv="t", model="sonnet")
    listing = await tools.schedule_list()
    assert "model=sonnet" in listing["content"][0]["text"]


@pytest.mark.asyncio
async def test_schedule_create_accepts_model(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    await tools.schedule_create(
        id="pinned",
        prompt="p",
        destination_transport="slack",
        destination_channel="C1",
        cron="0 6 * * *",
        model="sonnet",
        creator_conv="t",
    )
    assert tools.schedule_store.get("pinned").model == "sonnet"
