"""Tests for the scheduler's optional per-schedule ``gate_command`` pre-flight.

The gate exists so a schedule that fires on a tight cron (``heartbeat`` at
``0,30 7-19 * * *``, 26x/day) can skip the ticks where a cheap deterministic
check proves there is nothing to look at, instead of paying for a full LLM turn
to discover the same thing. ``scripts/heartbeat_gate.py`` is the first such
gate; the contract it documents is the contract the scheduler implements:

    exit 0   nothing present -> SKIP this tick
    exit 10  something present, or a check was inconclusive -> FIRE
    exit 1   the gate itself malfunctioned -> FIRE

"The caller must treat ANY non-zero exit as 'run'. Only 0 is a skip."

FAIL OPEN is the property under test here, and most of this file exists to pin
it: a gate that times out, crashes, cannot be found, or prints garbage must
leave the fire completely unaffected. The only branch allowed to suppress is a
clean exit 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import anna.runtime.scheduler as scheduler_module
from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState
from anna.runtime.scheduler import GATE_TIMEOUT_SECONDS, Scheduler
from anna.runtime.supervisor import Supervisor
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage

# ---------------------------------------------------------------------------
# Fakes (mirrors of the ones in test_scheduler.py)
# ---------------------------------------------------------------------------


class FakeRouter:
    def __init__(self) -> None:
        self.dispatched: list[InboundEvent] = []
        self.reply: str = "Gate let me through."

    async def dispatch(self, event: InboundEvent) -> None:
        self.dispatched.append(event)
        if event.completion_future is not None and not event.completion_future.done():
            event.completion_future.set_result(self.reply)


class FakeAdapter(ChannelAdapter):
    name = "fake-slack"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:  # pragma: no cover - not exercised
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
        return "fake"


class FakeAlerter:
    def __init__(self) -> None:
        self.warned: list[str] = []

    async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warned.append(message)
        return True

    async def critical(
        self, message: str, *, exclude_channel: str | None = None
    ) -> bool:  # pragma: no cover
        self.warned.append(message)
        return True

    async def notify_startup(self, message: str) -> bool:  # pragma: no cover
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


def _make_schedule(*, gate_command: list[str] | None = None) -> Schedule:
    return Schedule(
        id="heartbeat",
        cron="0,30 7-19 * * *",
        prompt="Run the heartbeat check.",
        destination=ScheduleDestination(transport="slack", channel="CHEART"),
        timeout_seconds=300,
        enabled=True,
        ephemeral=True,
        gate_command=gate_command,
        created_at=datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc),
        state=ScheduleState(),
    )


async def _build(
    tmp_path: Path, *, gate_command: list[str] | None = None
) -> tuple[AnnaConfig, ScheduleStore, FakeRouter, FakeAdapter, Scheduler]:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule(gate_command=gate_command))
    router = FakeRouter()
    adapter = FakeAdapter()
    sched = Scheduler(
        config=cfg,
        store=store,
        router=router,  # type: ignore[arg-type]
        adapters={"slack": adapter},
        alerter=FakeAlerter(),  # type: ignore[arg-type]
    )
    return cfg, store, router, adapter, sched


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


def _py(code: str) -> list[str]:
    """An argv for a gate implemented as a one-liner in this interpreter."""
    return [sys.executable, "-c", code]


async def _assert_fired(cfg: AnnaConfig, adapter: FakeAdapter, router: FakeRouter) -> None:
    """Pin the full fail-open expectation: the turn ran, the reply landed at
    the destination, the normal fire/complete rows are present, and NO
    suppression row was written."""
    assert len(router.dispatched) == 1
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Gate let me through."
    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.fire")
    assert _events(audits, "audit.schedule.complete")
    assert not _events(audits, "audit.schedule.gate_skip")
    assert not _events(audits, "audit.schedule.fail")


# ---------------------------------------------------------------------------
# Default: no gate configured
# ---------------------------------------------------------------------------


def test_gate_command_defaults_to_none() -> None:
    """Legacy ``schedules.yaml`` records carry no ``gate_command`` key and must
    load as ungated, i.e. exactly today's behavior."""
    rebuilt = Schedule.model_validate(
        {
            "id": "legacy",
            "cron": "0 6 * * *",
            "prompt": "x",
            "destination": {"transport": "slack", "channel": "C1"},
            "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        }
    )
    assert rebuilt.gate_command is None


def test_gate_command_round_trips() -> None:
    s = _make_schedule(gate_command=["/usr/bin/gate", "--json"])
    rebuilt = Schedule.model_validate(s.model_dump())
    assert rebuilt == s
    assert rebuilt.gate_command == ["/usr/bin/gate", "--json"]


@pytest.mark.asyncio
async def test_no_gate_fires_normally(tmp_path: Path) -> None:
    """gate absent -> fires. The unchanged default path."""
    cfg, _store, router, adapter, sched = await _build(tmp_path)
    await sched._guarded_fire(_make_schedule())
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_empty_gate_command_fires_normally(tmp_path: Path) -> None:
    """An empty argv is not a gate; it must not spawn anything or suppress."""
    cfg, _store, router, adapter, sched = await _build(tmp_path, gate_command=[])
    await sched._guarded_fire(_make_schedule(gate_command=[]))
    await _assert_fired(cfg, adapter, router)


# ---------------------------------------------------------------------------
# The one suppressing outcome: a clean exit 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_exit_zero_suppresses_and_audits(tmp_path: Path) -> None:
    """gate says skip -> suppressed, and the gate_skip row is emitted.

    That row is the whole point: no ``audit.schedule.fire`` is written for a
    gated tick, so counting gate_skip against fire yields the measured
    suppression rate.
    """
    gate = _py(
        "import json,sys;"
        "sys.stdout.write(json.dumps({'decision': 'skip', 'exit_code': 0}));"
        "raise SystemExit(0)"
    )
    cfg, store, router, adapter, sched = await _build(tmp_path, gate_command=gate)

    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]

    # The turn never ran and nothing reached the destination.
    assert router.dispatched == []
    assert adapter.sent == []

    audits = _read_audit_records(cfg.audit_dir)
    skips = _events(audits, "audit.schedule.gate_skip")
    assert len(skips) == 1
    assert skips[0]["schedule_id"] == "heartbeat"
    assert skips[0]["exit_code"] == 0
    assert skips[0]["reason"] == "skip"
    assert skips[0]["gate_command"] == gate
    assert "decision" in skips[0]["gate_output"]
    assert isinstance(skips[0]["duration_seconds"], float)

    # A gated tick is not a fire, not a completion, and not a failure.
    assert not _events(audits, "audit.schedule.fire")
    assert not _events(audits, "audit.schedule.complete")
    assert not _events(audits, "audit.schedule.fail")

    # A tick that never ran must not touch success or failure state.
    state = store.get("heartbeat").state  # type: ignore[union-attr]
    assert state.last_status is None
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_gate_exit_zero_with_silent_stdout_still_suppresses(
    tmp_path: Path,
) -> None:
    """The real gate prints NOTHING on a skip unless asked. Absence of a
    reason must not turn the skip into a fire."""
    cfg, store, _router, adapter, sched = await _build(
        tmp_path, gate_command=_py("raise SystemExit(0)")
    )

    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]

    assert adapter.sent == []
    skips = _events(_read_audit_records(cfg.audit_dir), "audit.schedule.gate_skip")
    assert len(skips) == 1
    assert skips[0]["reason"] == ""


@pytest.mark.asyncio
async def test_gate_exit_zero_with_unparseable_stdout_still_suppresses(
    tmp_path: Path,
) -> None:
    """Garbage on stdout is a reason-extraction problem, not a decision
    problem: exit 0 still means skip, and the reason degrades to the last
    line of output."""
    cfg, store, _router, adapter, sched = await _build(
        tmp_path,
        gate_command=_py("print('{not json at all'); raise SystemExit(0)"),
    )

    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]

    assert adapter.sent == []
    skips = _events(_read_audit_records(cfg.audit_dir), "audit.schedule.gate_skip")
    assert len(skips) == 1
    assert skips[0]["reason"] == "{not json at all"


# ---------------------------------------------------------------------------
# Fail-open: every other outcome fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_wake_exit_code_fires(tmp_path: Path) -> None:
    """exit 10 is heartbeat_gate.py's "something present" -> fires."""
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=_py("print('WAKE: mail'); raise SystemExit(10)")
    )
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_gate_malfunction_exit_code_fires(tmp_path: Path) -> None:
    """exit 1 is heartbeat_gate.py's "the gate itself broke" -> fires."""
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=_py("raise SystemExit(1)")
    )
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.parametrize("code", [2, 3, 42, 127, 255])
@pytest.mark.asyncio
async def test_gate_any_unexpected_nonzero_exit_fires(
    tmp_path: Path, code: int
) -> None:
    """No exit code other than 0 may ever suppress, including ones the gate
    contract does not define."""
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=_py(f"raise SystemExit({code})")
    )
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_gate_timeout_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gate times out -> fires. Driven by shrinking the module-level budget
    rather than sleeping 15 real seconds."""
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=_py("import time; time.sleep(30)")
    )
    monkeypatch.setattr(scheduler_module, "GATE_TIMEOUT_SECONDS", 0.5)
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_gate_crash_fires(tmp_path: Path) -> None:
    """gate crashes (traceback, nonzero exit, stderr noise) -> fires."""
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=_py("raise RuntimeError('gate exploded')")
    )
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_gate_binary_not_found_fires(tmp_path: Path) -> None:
    """gate not found -> fires. ``subprocess`` raises FileNotFoundError before
    the child ever exists, so there is no exit code at all."""
    missing = str(tmp_path / "definitely-not-a-real-gate")
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=[missing]
    )
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_gate_not_executable_fires(tmp_path: Path) -> None:
    """A gate file that exists but cannot be exec'd (no +x) -> fires."""
    script = tmp_path / "not-executable-gate"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=[str(script)]
    )
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


@pytest.mark.asyncio
async def test_gate_subprocess_raising_oserror_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any spawn-time exception at all fails open, not just the ones with a
    dedicated branch above. Pinned with a gate that WOULD have exited 0, so a
    regression that swallowed the exception would suppress the fire."""
    cfg, store, router, adapter, sched = await _build(
        tmp_path, gate_command=_py("raise SystemExit(0)")
    )

    def _boom(*_args, **_kwargs):
        raise OSError("no file descriptors left")

    monkeypatch.setattr(subprocess, "run", _boom)
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    await _assert_fired(cfg, adapter, router)


# ---------------------------------------------------------------------------
# _consult_gate in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_gate_reports_telemetry(tmp_path: Path) -> None:
    gate = _py("print('{\"decision\": \"skip\"}'); raise SystemExit(0)")
    _cfg, store, _router, _adapter, sched = await _build(tmp_path, gate_command=gate)
    outcome = await sched._consult_gate(store.get("heartbeat"))  # type: ignore[arg-type]
    assert outcome.skip is True
    assert outcome.exit_code == 0
    assert outcome.reason == "skip"
    assert outcome.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_consult_gate_without_command_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cfg, _store, _router, _adapter, sched = await _build(tmp_path)

    def _must_not_run(*_args, **_kwargs):  # pragma: no cover - asserts absence
        raise AssertionError("ungated schedule spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", _must_not_run)
    outcome = await sched._consult_gate(_make_schedule())

    assert outcome.skip is False
    assert outcome.exit_code is None


def test_gate_timeout_budget_is_fifteen_seconds() -> None:
    """The gate is meant to be cheaper than the fire it guards; pin the cap so
    it cannot drift up into the fire's own timeout territory."""
    assert GATE_TIMEOUT_SECONDS == 15.0
