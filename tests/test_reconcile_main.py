"""Regression tests for the origin/main reconciliation.

The ledger-based scheduler and main's per-schedule ``gate_command`` / ``model``
pin, delivered-post recording, and usage accounting must all hold together.
Synthetic fixtures only; everything lives under tmp_path.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule
from anna.runtime.supervisor import Supervisor
from tests.test_scheduler_gate_command import (
    FakeAdapter,
    FakeAlerter,
    FakeRouter,
    _build,
    _events,
    _make_config,
    _make_schedule,
    _py,
    _read_audit_records,
)

DM_USER = "UTESTUSER1"


def _occurrences(cfg) -> list[sqlite3.Row]:
    dbs = list(Path(cfg.anna_home).rglob("*.db")) + list(Path(cfg.anna_home).rglob("*.sqlite*"))
    assert dbs, "ledger db not found"
    out: list[sqlite3.Row] = []
    for db in dbs:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            out += conn.execute("SELECT * FROM schedule_occurrences").fetchall()
        except sqlite3.Error:
            pass
        conn.close()
    return out


async def test_unknown_schedule_fields_round_trip(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    path = Path(cfg.scheduler.state_path)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "schedules": [
                    {
                        "id": "future",
                        "cron": "0 6 * * *",
                        "prompt": "p",
                        "destination": {"transport": "slack", "channel": "C1"},
                        "created_at": "2026-06-01T00:00:00+00:00",
                        "gate_command": ["true"],
                        "model": "sonnet",
                        "from_a_newer_anna": {"nested": [1, 2]},
                    }
                ],
            }
        )
    )
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.load()
    got = store.get("future")
    assert got is not None and got.gate_command == ["true"] and got.model
    await store.mark_dispatched("future", when=datetime(2026, 6, 2, tzinfo=timezone.utc))
    saved = yaml.safe_load(path.read_text())["schedules"][0]
    assert saved["from_a_newer_anna"] == {"nested": [1, 2]}
    assert saved["gate_command"] == ["true"]
    assert saved["model"]


async def test_gate_skip_closes_occurrence_without_run_or_failure(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _build(tmp_path, gate_command=_py("raise SystemExit(0)"))
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    assert router.dispatched == [] and adapter.sent == []
    assert [o["exec_status"] for o in _occurrences(cfg)] == ["skipped_gate"]
    got = store.get("heartbeat")
    assert got is not None
    assert got.state.consecutive_failures == 0
    assert got.state.last_fired_at is not None  # a rollback must not replay the tick
    audits = _read_audit_records(cfg.audit_dir)
    assert len(_events(audits, "audit.schedule.gate_skip")) == 1
    assert not _events(audits, "audit.schedule.fire")


async def test_gate_wake_runs_pinned_model_and_records_post_in_dm(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    base = _make_schedule(gate_command=_py("raise SystemExit(10)"))
    sched_obj = Schedule.model_validate(
        {**base.model_dump(), "model": "sonnet",
         "destination": {"transport": "slack", "channel": DM_USER}}
    )
    await store.create(sched_obj)
    from anna.runtime.scheduler import Scheduler

    router, adapter = FakeRouter(), FakeAdapter()
    router.reply = "Synthetic alert: widget shipped."
    sched = Scheduler(config=cfg, store=store, router=router, adapters={"slack": adapter},  # type: ignore[arg-type]
                      alerter=FakeAlerter())  # type: ignore[arg-type]
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]

    assert router.dispatched[0].model == store.get("heartbeat").model  # type: ignore[union-attr]
    assert router.dispatched[0].model
    assert [m.text for m in adapter.sent] == ["Synthetic alert: widget shipped."]
    rows = [
        line
        for p in cfg.transcripts_dir.rglob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert len(rows) == 1 and "schedule:heartbeat" in rows[0] and f"slack:dm:{DM_USER}" in rows[0]
    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.delivered_post_recorded")
    assert _events(audits, "audit.schedule.complete")[0]["suppressed"] is False


async def test_suppressed_run_records_no_delivered_post(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _build(tmp_path)
    router.reply = "[[ANNA_NO_OUTPUT]]"
    await sched._guarded_fire(store.get("heartbeat"))  # type: ignore[arg-type]
    assert adapter.sent == []
    assert not list(cfg.transcripts_dir.rglob("*.jsonl"))


# ---------------------------------------------------------------------------
# Stale ledger cursor vs newer schedules.yaml last_fired_at (rollback/retrial)
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from anna.runtime.ledger import now_utc  # noqa: E402
from anna.runtime.schedule_types import ScheduleState  # noqa: E402
from tests.test_scheduler_ledger import build, make_schedule, settle  # noqa: E402


def _fired(minutes_ago: float) -> ScheduleState:
    return ScheduleState(last_fired_at=now_utc() - timedelta(minutes=minutes_ago), last_status="complete")


async def _stamp_after_latest_tick() -> ScheduleState:
    """What an older build writes: ``last_fired_at`` just after the newest every-minute tick (a PAST time)."""
    while True:
        tick = now_utc().replace(second=0, microsecond=0)
        if (now_utc() - tick).total_seconds() > 0.3:
            return ScheduleState(last_fired_at=tick + timedelta(milliseconds=100), last_status="complete")
        await asyncio.sleep(0.1)


async def test_empty_ledger_respects_last_fired_at(tmp_path):
    # first start after an upgrade from main: the legacy build stamped last_fired_at at the latest tick
    sch = make_schedule(created=now_utc() - timedelta(days=3), state=await _stamp_after_latest_tick())
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=sch)
    await sched._poll_once(); await settle(sched)
    assert router.events == []  # the legacy build already ran the latest tick


async def test_stale_ledger_cursor_does_not_replay_ticks_an_older_build_ran(tmp_path):
    # rollback -> retrial: the older build stamped last_fired_at (a PAST time, after a tick) and
    # this build never saw it; the ledger cursor is two hours stale.
    state = await _stamp_after_latest_tick()
    stamp = state.last_fired_at
    sch = make_schedule(created=now_utc() - timedelta(days=3), state=state)
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=sch)
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", now_utc() - timedelta(hours=2))
    await sched._poll_once(); await settle(sched)
    assert router.events == [] and adapter.sent == []
    assert sched._ledger.cursor("s1") == stamp
    assert sched._ledger.recent("s1", 100) == []  # not even misfire rows for already-run ticks


async def test_stale_cursor_with_old_last_fired_at_still_runs_latest_once(tmp_path):
    sch = make_schedule(created=now_utc() - timedelta(days=3), state=_fired(30))
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=sch)
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", now_utc() - timedelta(hours=2))
    await sched._poll_once(); await settle(sched)
    assert len(router.events) == 1  # genuinely missed ticks: only the newest runs


async def test_legitimate_pending_occurrence_still_recovered_when_no_older_build_ran(tmp_path):
    # Same-build crash: the YAML stamp is OURS (recorded in the ledger), so it must not supersede
    # the queued occurrence, even though it is newer than the cursor.
    sch = make_schedule(created=now_utc() - timedelta(days=3), state=_fired(1))
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=sch)
    ledger = sched._ledger
    ledger.note_stamp("s1", sch.state.last_fired_at)  # this build wrote the stamp
    with ledger._conn:
        ledger._set_cursor("s1", now_utc() - timedelta(hours=2))
        occ = ledger._insert_occurrence(schedule_id="s1", scheduled_for=now_utc() - timedelta(minutes=90),
                                        trigger="cron", job_kind="agent_prompt", run_safety="side_effecting",
                                        status="pending", detail=None)
    assert occ
    await sched._poll_once(); await settle(sched)
    await sched._poll_once(); await settle(sched)
    assert len(router.events) == 1 and len(adapter.sent) == 1  # recovered once, no duplicate effects
    assert "succeeded" in [o.exec_status for o in ledger.recent("s1", 100)]


# ---------------------------------------------------------------------------
# Restart-interrupted jobs are infrastructure, not job failures
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from anna.runtime.ledger import iso  # noqa: E402
from tests.test_scheduler_ledger import Router  # noqa: E402


async def _interrupt_once(sched, store, minutes_ago: int) -> None:
    with sched._ledger._conn:
        sched._ledger._insert_occurrence(
            schedule_id="s1", scheduled_for=now_utc() - timedelta(minutes=minutes_ago), trigger="cron",
            job_kind="agent_prompt", run_safety="side_effecting", status="pending", detail=None)
    [occ] = sched._ledger.pending()
    sched._ledger.claim(occ.id, owner="dead", lease_seconds=-1)
    sched._ledger.start_run(occ.id, owner="dead")
    sched._ledger._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?",
                                (iso(now_utc() - timedelta(seconds=1)), occ.id))
    sched._ledger._conn.commit()
    await sched._poll_once(); await settle(sched)


async def test_repeated_restart_interruptions_never_auto_disable(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    cfg.scheduler.failure_threshold = 3
    for i in range(4):  # one below interruption_threshold (5)
        await _interrupt_once(sched, store, i + 10)
    got = store.get("s1")
    assert len([o for o in sched._ledger.recent("s1", 100) if o.exec_status == "unknown"]) == 4
    assert got.enabled and got.state.consecutive_failures == 0


async def test_real_failures_still_auto_disable_and_interruptions_do_not_reset_them(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("reply"))
    cfg.scheduler.failure_threshold = 3
    await store.mark_failed("s1", reason="boom", when=now_utc(), kind="fail")
    await store.mark_failed("s1", reason="restart", when=now_utc(), kind="interrupted")
    got = store.get("s1")
    assert got.state.consecutive_failures == 1 and got.state.last_status == "fail"
    await sched._handle_failure(got, reason="restart", kind="interrupted")
    assert store.get("s1").state.consecutive_failures == 1 and store.get("s1").enabled
    await sched._handle_failure(got, reason="boom2", kind="fail")
    await sched._handle_failure(got, reason="boom3", kind="fail")
    assert not store.get("s1").enabled  # genuine failures still trip the breaker


async def test_poison_job_interrupting_every_run_still_trips_the_breaker(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    cfg.scheduler.interruption_threshold = 3
    cfg.scheduler.failure_threshold = 2
    for i in range(10):
        if not store.get("s1").enabled:
            break
        await _interrupt_once(sched, store, i + 10)
    got = store.get("s1")
    assert not got.enabled  # interruptions 3,4 counted as failures -> disabled at failure_threshold
    assert got.state.consecutive_failures == 2


# ---------------------------------------------------------------------------
# Built-in heartbeat / extraction stay opt-in; no double-post
# ---------------------------------------------------------------------------

from anna.config import AnnaConfig  # noqa: E402
from anna.runtime.schedule_store import ScheduleValidationError  # noqa: E402


def test_builtin_heartbeat_and_extraction_default_off():
    cfg = AnnaConfig()
    assert cfg.heartbeat.enabled is False
    assert cfg.memory.extraction.enabled is False
    assert cfg.heartbeat.allowed_destinations == []


async def test_builtin_heartbeat_schedule_refused_until_opted_in_and_agent_heartbeat_untouched(tmp_path):
    cfg, store, router, adapter, sched = await _build(tmp_path, gate_command=_py("raise SystemExit(10)"))
    hb = Schedule.model_validate({**_make_schedule().model_dump(), "id": "builtin", "job_kind": "google_heartbeat"})
    with pytest.raises(ScheduleValidationError, match="opt-in"):
        await store.create(hb)
    assert store.get("builtin") is None
    # the pre-existing gated agent heartbeat is exactly as before
    agent = store.get("heartbeat")
    assert agent.job_kind == "agent_prompt" and agent.gate_command
    # the agent heartbeat still posts exactly once and is recorded once (no second writer)
    router.reply = "Synthetic: one alert."
    await sched._guarded_fire(agent)
    assert [m.text for m in adapter.sent] == ["Synthetic: one alert."]


# ---------------------------------------------------------------------------
# Mocked upgrade -> rollback -> retrial acceptance (synthetic fixtures only)
# ---------------------------------------------------------------------------

from anna.runtime.schedule_types import ScheduleDestination  # noqa: E402
from anna.runtime.scheduler import Scheduler  # noqa: E402

ACCEPT_DM = "UACCEPT01"


def _fleet(n: int = 15) -> list[Schedule]:
    created = now_utc() - timedelta(days=3)
    out = [
        Schedule(id="heartbeat", cron="* * * * *", prompt="check", created_at=created,
                 destination=ScheduleDestination(transport="slack", channel=ACCEPT_DM),
                 gate_command=_py("raise SystemExit(10)"), model="sonnet",
                 state=_fired(-0.3), from_newer_build={"keep": True})
    ]
    for i in range(n - 1):
        out.append(Schedule(id=f"job{i}", cron="* * * * *", prompt="x", created_at=created,
                            destination=ScheduleDestination(transport="slack", channel="CDEST"),
                            state=_fired(-0.3)))
    return out


async def test_upgrade_rollback_retrial_acceptance(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=False)
    for s in _fleet():
        await store.create(s)

    # 1. UPGRADE: first start with an empty ledger. Everything was just fired by the old build.
    await sched._poll_once(); await settle(sched)
    assert router.events == [] and adapter.sent == []
    saved = yaml.safe_load(Path(cfg.scheduler.state_path).read_text())["schedules"]
    hb = next(s for s in saved if s["id"] == "heartbeat")
    assert hb["gate_command"] and hb["model"] and hb["from_newer_build"] == {"keep": True}

    # 2. The heartbeat becomes due: the gate wakes it, the pinned model reaches the worker,
    #    and the post is recorded once in the destination DM transcript.
    await store.mark_dispatched("heartbeat", when=now_utc() - timedelta(minutes=5))
    with sched._ledger._conn:
        sched._ledger._set_cursor("heartbeat", now_utc() - timedelta(minutes=5))
    await sched._poll_once(); await settle(sched)
    assert [e.model for e in router.events] == [store.get("heartbeat").model]
    assert [m.text for m in adapter.sent] == ["all done"]
    rows = [l for p in cfg.transcripts_dir.rglob("*.jsonl") for l in p.read_text().splitlines()]
    assert len(rows) == 1 and "schedule:heartbeat" in rows[0] and ACCEPT_DM in rows[0]

    # 3. ROLLBACK to a build without the ledger (it advances last_fired_at only), then RETRIAL:
    #    the ledger cursors are now stale for ALL 15 schedules. Nothing may replay.
    for s in store.list():
        await store.mark_dispatched(s.id, when=now_utc() + timedelta(seconds=20))
        with sched._ledger._conn:
            sched._ledger._set_cursor(s.id, now_utc() - timedelta(hours=3))
    events_before, sent_before = len(router.events), len(adapter.sent)
    retrial = Scheduler(config=cfg, store=store, router=router, adapters={"slack": adapter}, alerter=FakeAlerter())
    await retrial._poll_once(); await settle(retrial)
    assert len(router.events) == events_before and len(adapter.sent) == sent_before
    assert len(store.list()) == 15 and all(s.enabled for s in store.list())
    # the gate/model/extra fields survive the whole round trip
    final = store.get("heartbeat")
    assert final.gate_command and final.model and final.model_extra["from_newer_build"] == {"keep": True}
