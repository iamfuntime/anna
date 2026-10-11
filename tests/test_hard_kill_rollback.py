# ruff: noqa: E702
"""Hard-kill + rollback: why an older build re-fires an in-flight tick, and the mitigation.

``main`` knows nothing of the runtime ledger; after a rollback the ONLY signal it has about a run in flight is
``last_fired_at`` in ``schedules.yaml``.  Main stamps it at dispatch.  If this build stamped only at completion,
a SIGKILL/OOM/power-loss mid-run left the previous run's stamp, so main found the interrupted tick due and ran
it again (duplicate side effects).  A clean stop is fine because shutdown writes a stamp.  Everything here is
mocked: fake clock, the vendored main-build mock, a router that never completes, a temp home.

"Hard kill" is modelled by snapshotting the home's files while the run is in flight and abandoning the
process: no shutdown handler runs, exactly as for SIGKILL.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import anna.runtime.scheduler as scheduler_mod
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from tests.fixtures.main_59b946a.main_build import MainBuild
from tests.test_scheduler_ledger import FakeAdapter, FakeAlerter, Router, make_cfg, make_schedule
from tests.test_upgrade_rollback_acceptance import Clock, clock  # noqa: F401  (fixture)

T_TICK = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)


async def _in_flight(tmp_path: Path, clock: Clock, *, stamp_at_start: bool, monkeypatch):
    home = tmp_path / "anna"; home.mkdir()
    cfg = make_cfg(home)
    if not stamp_at_start:  # the pre-mitigation behaviour, to prove the mechanism
        async def _noop(self, schedule, when):  # noqa: ARG001
            return None
        monkeypatch.setattr(Scheduler, "_stamp_run_start", _noop)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(make_schedule(cron="0 9 * * *", created=T_TICK - timedelta(days=3), run_safety="side_effecting"))
    yesterday = T_TICK - timedelta(days=1) + timedelta(seconds=30)   # last completion stamp
    await store.mark_fired("s1", status="complete", when=yesterday)
    router = Router("hang")                                          # the run starts and never completes
    sched = Scheduler(config=cfg, store=store, router=router, adapters={"slack": FakeAdapter()}, alerter=FakeAlerter())
    clock.set(T_TICK + timedelta(seconds=10))
    await sched._poll_once()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if router.events:
            break
    assert len(router.events) == 1                                   # the run is in flight (side effects may have happened)
    return home, cfg, sched


async def _abandon(sched) -> None:
    """Test cleanup only (after the snapshot): stop the orphaned tasks of the 'dead' process."""
    for task in list(sched._inflight):
        task.cancel()
    await asyncio.gather(*sched._inflight, return_exceptions=True)


def _snapshot(home: Path, dst: Path) -> Path:
    dst.mkdir()
    shutil.copy2(home / "schedules.yaml", dst / "schedules.yaml")
    return dst / "schedules.yaml"


def _main_ticks(yaml_path: Path, now: datetime) -> list[str]:
    return MainBuild(yaml_path).poll_once(now)


async def test_hard_kill_without_dispatch_stamp_makes_main_refire_the_inflight_tick(tmp_path, clock, monkeypatch):
    home, cfg, sched = await _in_flight(tmp_path, clock, stamp_at_start=False, monkeypatch=monkeypatch)
    yaml_path = _snapshot(home, tmp_path / "killed")                 # SIGKILL: nothing more is written
    fired = _main_ticks(yaml_path, T_TICK + timedelta(minutes=2))    # rolled-back main starts
    assert fired == ["s1"]                                           # <- the duplicate: same 09:00 tick again
    await _abandon(sched)


async def test_hard_kill_with_dispatch_stamp_main_does_not_refire(tmp_path, clock, monkeypatch):
    home, cfg, sched = await _in_flight(tmp_path, clock, stamp_at_start=True, monkeypatch=monkeypatch)
    yaml_path = _snapshot(home, tmp_path / "killed")
    stamped = yaml.safe_load(yaml_path.read_text())["schedules"][0]["state"]["last_fired_at"]
    assert stamped.startswith("2026-10-10T09:00:10")                 # dispatch time, before any side effect
    assert _main_ticks(yaml_path, T_TICK + timedelta(minutes=2)) == []
    assert _main_ticks(yaml_path, T_TICK + timedelta(days=1, minutes=1)) == ["s1"]   # tomorrow's tick still runs
    await _abandon(sched)


async def test_clean_stop_control_main_does_not_refire(tmp_path, clock, monkeypatch):
    home, cfg, sched = await _in_flight(tmp_path, clock, stamp_at_start=False, monkeypatch=monkeypatch)
    sched._shutdown_grace_seconds = 0.05
    await sched.shutdown()                                           # graceful: stamps before exit
    yaml_path = _snapshot(home, tmp_path / "clean")
    assert _main_ticks(yaml_path, T_TICK + timedelta(minutes=2)) == []


async def test_dispatch_stamp_is_own_stamp_so_the_ledger_cursor_does_not_move(tmp_path, clock, monkeypatch):
    home, cfg, sched = await _in_flight(tmp_path, clock, stamp_at_start=True, monkeypatch=monkeypatch)
    led = sched._ledger
    cursor = led.cursor("s1")
    await sched._poll_once()                                         # next poll must not adopt our own stamp
    assert led.cursor("s1") == cursor
    from tests.test_scheduler_gate_command import _events, _read_audit_records
    assert not _events(_read_audit_records(cfg.audit_dir), "audit.schedule.cursor_external_stamp")
    await _abandon(sched)
