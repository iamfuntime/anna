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
