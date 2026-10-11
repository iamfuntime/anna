# ruff: noqa: E702
"""Regression tests for the independent reconciliation review (H1, M4, M7, M8, M9, L3, L5, L6, L7).

Everything is synthetic: temp homes, fake router/adapter, no providers or services.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from anna.runtime.delivered_posts import conv_key_for_destination
from anna.runtime.ledger import iso, now_utc
from anna.runtime.schedule_types import ScheduleState
from anna.vault.transcript_resume import render_delivered_posts
from tests.test_scheduler_gate_command import _events, _read_audit_records
from tests.test_scheduler_ledger import Router, build, make_schedule, settle

DM = "UREVIEW01"


def _tick() -> "object":
    return now_utc().replace(second=0, microsecond=0)


async def _past_tick_gap() -> None:
    while (now_utc() - _tick()).total_seconds() < 0.3:
        await asyncio.sleep(0.1)


def _transcript_rows(cfg, conv: str) -> list[dict]:
    d = cfg.transcripts_dir / conv.replace(":", "-")
    return [json.loads(line) for p in sorted(d.rglob("*.jsonl")) for line in p.read_text().splitlines()] if d.exists() else []


def _real(sched):
    return [o for o in sched._ledger.recent("s1", 100) if o.exec_status != "skipped_misfire"]


def _dead_running(sched, scheduled_for, *, safety="side_effecting"):
    led = sched._ledger
    with led._conn:
        occ = led._insert_occurrence(schedule_id="s1", scheduled_for=scheduled_for, trigger="cron",
                                     job_kind="agent_prompt", run_safety=safety, status="pending", detail=None)
    led.claim(occ, owner="999999-x-dead", lease_seconds=-1)
    led.start_run(occ, owner="999999-x-dead")
    led._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?",
                      (iso(now_utc() - timedelta(seconds=1)), occ))
    led._conn.commit()
    return occ


# ---------------------------------------------------------------------------------------- H1

async def test_h1_crash_recovery_does_not_drop_a_tick_that_came_due_during_downtime(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=1)))
    start = _tick() - timedelta(minutes=5)
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", start)
    occ = _dead_running(sched, start)
    await sched._poll_once(); await settle(sched)
    # recovery stamped last_fired_at = now (completion time) - that must not lift the cursor
    assert store.get("s1").state.last_fired_at is not None
    occs = {o.id: o for o in sched._ledger.recent("s1", 100)}
    assert occs[occ].exec_status == "unknown"
    ran = [o for o in occs.values() if o.exec_status == "succeeded"]
    assert len(ran) == 1 and ran[0].scheduled_for == iso(_tick())
    assert len(router.events) == 1 and len(adapter.sent) == 1


async def test_h1_completion_window_stamp_does_not_swallow_the_next_tick(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=1)))
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", _tick() - timedelta(minutes=1))
    # a run of this schedule finished just now: this build stamped last_fired_at = completion time
    await sched._mark_fired("s1", when=now_utc())
    await sched._poll_once(); await settle(sched)
    assert len(router.events) == 1
    assert [o.scheduled_for for o in sched._ledger.recent("s1")] == [iso(_tick())]


async def test_h1_external_stamp_still_prevents_replay(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=1)))
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", _tick() - timedelta(hours=2))
    await store.mark_dispatched("s1", when=_tick() + timedelta(milliseconds=100))  # an OLDER build wrote this
    await sched._poll_once(); await settle(sched)
    assert router.events == [] and sched._ledger.recent("s1") == []
    rec = _events(_read_audit_records(cfg.audit_dir), "audit.schedule.cursor_external_stamp")
    assert len(rec) == 1 and rec[0]["own_stamp"] is None


async def test_h1_own_stamp_after_external_advance_does_not_lift_again(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=1)))
    led = sched._ledger
    with led._conn:
        led._set_cursor("s1", _tick() - timedelta(minutes=3))
    led.note_stamp("s1", now_utc() - timedelta(minutes=2))
    await store.mark_dispatched("s1", when=now_utc())  # written by an outside actor, newer than ours
    await sched._poll_once(); await settle(sched)
    assert router.events == []  # external: treated as "already ran"
    await _past_tick_gap()
    with led._conn:
        led._set_cursor("s1", _tick() - timedelta(minutes=1))
    await sched._mark_fired("s1", when=now_utc())  # our own, later stamp
    await sched._poll_once(); await settle(sched)
    assert len(router.events) == 1


# ---------------------------------------------------------------------------------------- L3

async def test_l3_future_last_fired_at_is_ignored_and_never_pins_the_cursor(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=1)))
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", _tick() - timedelta(minutes=2))
    await store.mark_dispatched("s1", when=now_utc() + timedelta(hours=1))  # clock step / hand edit
    await sched._poll_once(); await settle(sched)
    assert len(router.events) == 1  # the due tick ran
    assert sched._ledger.cursor("s1") <= now_utc()
    assert _events(_read_audit_records(cfg.audit_dir), "audit.schedule.cursor_future_stamp_ignored")


# ---------------------------------------------------------------------------------------- M4

def _stale_work(sched, *, days=7):
    """A week-old pending cron occurrence and a week-old pending outbox message, both from this build."""
    led = sched._ledger
    old = now_utc() - timedelta(days=days)
    with led._conn:
        pend = led._insert_occurrence(schedule_id="s1", scheduled_for=old, trigger="cron", job_kind="agent_prompt",
                                      run_safety="side_effecting", status="pending", detail=None)
    done = led.create_manual(schedule_id="s1", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(done, owner=sched._owner, lease_seconds=60)
    run = led.start_run(done, owner=sched._owner)
    led.finish_run(done, run, status="succeeded", side_effects="none", outbox=[("slack", "CDEST", "week-old alert text")])
    led._conn.execute("UPDATE schedule_outbox SET created_at=?", (iso(old),))
    led._conn.commit()
    return pend, done


async def test_m4_superseded_pending_work_is_not_run_or_sent_after_an_older_build_ran(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=9)))
    pend, done = _stale_work(sched)
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", now_utc() - timedelta(days=7))
    await store.mark_dispatched("s1", when=_tick() + timedelta(milliseconds=100))  # older build ran it since
    await sched._poll_once(); await settle(sched)
    assert router.events == [] and adapter.sent == []
    assert sched._ledger.get(pend).exec_status == "skipped_misfire"
    assert [r["status"] for r in sched._ledger.outbox(done)] == ["cancelled"]
    note = _events(_read_audit_records(cfg.audit_dir), "audit.schedule.cursor_external_stamp")[0]
    assert note["superseded_occurrences"] == [pend] and len(note["superseded_outbox"]) == 1


async def test_m4_same_build_pending_work_still_recovers(tmp_path):
    await _past_tick_gap()
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(days=9)))
    pend, done = _stale_work(sched)
    with sched._ledger._conn:
        sched._ledger._set_cursor("s1", now_utc() - timedelta(days=7))
    await sched._mark_fired("s1", when=_tick() + timedelta(milliseconds=100))  # OUR stamp: no older build ran
    await sched._poll_once(); await settle(sched)
    assert "week-old alert text" in [m.text for m in adapter.sent]
    assert sched._ledger.get(pend).exec_status == "succeeded"


# ---------------------------------------------------------------------------------------- M7 / M8 / L5 / L6 / L7

def _dm_schedule(**kw):
    # yearly cron: no tick is ever due, so only the rows a test creates by hand exist
    sch = make_schedule(cron="0 0 1 1 *", created=now_utc() - timedelta(days=1), **kw)
    return sch.model_copy(update={"destination": sch.destination.model_copy(update={"channel": DM})})


def _enqueue(sched, text, *, channel=DM, chunk=50):
    led = sched._ledger
    occ = led.create_manual(schedule_id="s1", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(occ, owner=sched._owner, lease_seconds=60)
    run = led.start_run(occ, owner=sched._owner)
    led.finish_run(occ, run, status="succeeded", side_effects="none", outbox=[("slack", channel, text)], chunk_chars=chunk)
    return occ


LONG = "HEADLINE: 3 invoices overdue\n" + "".join(f"detail row {i:04d} " + "x" * 20 + "\n" for i in range(8))


async def test_m7_long_post_is_recorded_once_with_its_headline(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    _enqueue(sched, LONG)
    await sched._deliver_due()
    assert len(adapter.sent) >= 3
    rows = _transcript_rows(cfg, f"slack:dm:{DM}")
    assert len(rows) == 1 and rows[0]["text"] == LONG and rows[0]["source"] == "schedule:s1"
    lines, omitted = render_delivered_posts(rows)
    assert omitted == 0 and "HEADLINE: 3 invoices overdue" in lines[0]
    rec = _events(_read_audit_records(cfg.audit_dir), "audit.schedule.delivered_post_recorded")
    assert len(rec) == 1 and rec[0]["parts"] == len(adapter.sent) and rec[0]["partial"] is False


async def test_l5_record_follows_the_outbox_row_destination_not_the_schedules(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    _enqueue(sched, "hello there", channel="UOTHER99")  # destination edited since: schedule says DM
    await sched._deliver_due()
    assert len(_transcript_rows(cfg, "slack:dm:UOTHER99")) == 1
    assert _transcript_rows(cfg, f"slack:dm:{DM}") == []


async def test_l5_record_survives_a_deleted_schedule(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    _enqueue(sched, "hello there")
    await store.delete("s1")
    sched._ledger._conn.execute("UPDATE schedule_outbox SET status='pending'")  # undo cancel-on-delete for the test
    sched._ledger._conn.commit()
    await sched._deliver_due()
    assert len(_transcript_rows(cfg, f"slack:dm:{DM}")) == 1


async def test_l6_recording_is_idempotent_across_the_crash_window(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    _enqueue(sched, LONG)
    await sched._deliver_due()
    assert len(_transcript_rows(cfg, f"slack:dm:{DM}")) == 1
    sched._ledger._conn.execute("DELETE FROM delivered_post_records")  # crash after the append, before bookkeeping
    sched._ledger._conn.commit()
    await sched._poll_once(); await settle(sched)
    assert len([r for r in _transcript_rows(cfg, f"slack:dm:{DM}") if r["text"] == LONG]) == 1


async def test_l6_failed_transcript_write_is_audited_as_failed_not_recorded(tmp_path, monkeypatch):
    import anna.runtime.delivered_posts as dp
    monkeypatch.setattr(dp, "transcript_event", lambda *a, **k: None)
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    _enqueue(sched, "hello there")
    await sched._deliver_due()
    audits = _read_audit_records(cfg.audit_dir)
    assert not _events(audits, "audit.schedule.delivered_post_recorded")
    assert len(_events(audits, "audit.schedule.delivered_post_record_failed")) == 1


async def test_pre_migration_history_is_not_backfilled(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    occ = _enqueue(sched, "old history")
    await sched._deliver_due()
    led = sched._ledger
    led._conn.execute("DELETE FROM delivered_post_records")
    led._conn.execute("DELETE FROM ledger_migrations WHERE version=6")
    led._conn.commit()
    led._migrate_v6.__func__  # noqa: B018 - the migration exists
    for p in Path(cfg.transcripts_dir).rglob("*.jsonl"):
        p.unlink()
    led._conn.execute("DROP TABLE delivered_post_records"); led._conn.commit()
    led._migrate_v6()
    assert led._conn.execute("SELECT COUNT(*) FROM delivered_post_records WHERE occurrence_id=?", (occ,)).fetchone()[0] == 1
    sched._record_settled_posts()
    assert _transcript_rows(cfg, f"slack:dm:{DM}") == []


async def test_l7_complete_audit_keeps_its_shape_and_says_delivery_is_queued(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    await sched._guarded_fire(store.get("s1"))
    [done] = _events(_read_audit_records(cfg.audit_dir), "audit.schedule.complete")
    assert "recorded_conv_key" in done and done["recorded_conv_key"] is None
    assert done["delivery"] == "queued" and done["destination_conv_key"] == f"slack:dm:{DM}"
    assert {"model", "duration_s", "total_cost_usd"} <= set(done)


async def test_m8_interrupted_multipart_send_is_visible_recorded_and_repairable(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    occ = _enqueue(sched, LONG)
    led = sched._ledger
    [first] = led.claim_outbox(now=now_utc(), lease_seconds=60, limit=1)
    led.outbox_sent(first)
    [second] = led.claim_outbox(now=now_utc(), lease_seconds=-1, limit=1)  # in flight when the daemon died
    n = len(led.outbox(occ))
    await sched._poll_once(); await settle(sched)
    statuses = [r["status"] for r in led.outbox(occ)]
    assert statuses[:2] == ["sent", "uncertain"] and set(statuses[2:]) == {"cancelled"}
    audits = _read_audit_records(cfg.audit_dir)
    [intr] = _events(audits, "audit.schedule.delivery_interrupted")
    assert intr["part"] == 2 and intr["parts"] == n and intr["stranded_parts"] == n - 2
    assert any("interrupted at part 2" in w and "NOT resent" in w for w in sched._alerter.warned)
    [row] = _transcript_rows(cfg, f"slack:dm:{DM}")
    assert row["text"].startswith("HEADLINE") and "Partial delivery: 1 of" in row["text"] and "part 2 may or may not" in row["text"]
    # operator verifies part 2 did arrive: the held-back parts are released and go out, in order
    led._conn.execute("INSERT INTO repair_requests(kind,target_id,action,expected_status,requested_by,requested_at) "
                      "VALUES('outbox',?,'mark_delivered','uncertain','op',?)", (second.id, iso(now_utc())))
    led._conn.commit()
    await sched._poll_once(); await settle(sched)
    assert [r["status"] for r in led.outbox(occ)] == ["sent"] * n
    assert len(adapter.sent) == n - 2  # parts 3..n; parts 1 and 2 were never resent by the daemon
    texts = [r["text"] for r in _transcript_rows(cfg, f"slack:dm:{DM}")]
    assert texts[-1].startswith("HEADLINE") and "Partial delivery" not in texts[-1]


async def test_m8_abandon_keeps_the_rest_unsent(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=_dm_schedule())
    occ = _enqueue(sched, LONG)
    led = sched._ledger
    [first] = led.claim_outbox(now=now_utc(), lease_seconds=-1, limit=1)
    await sched._poll_once(); await settle(sched)
    led._conn.execute("INSERT INTO repair_requests(kind,target_id,action,expected_status,requested_by,requested_at) "
                      "VALUES('outbox',?,'abandon','uncertain','op',?)", (first.id, iso(now_utc())))
    led._conn.commit()
    await sched._poll_once(); await settle(sched)
    assert set(r["status"] for r in led.outbox(occ)) == {"cancelled"} and adapter.sent == []


# ---------------------------------------------------------------------------------------- M9

async def test_m9_exception_after_start_run_does_not_leave_the_schedule_blocked(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=make_schedule(created=now_utc() - timedelta(minutes=3)))
    real = sched._ledger.finish_run
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    sched._ledger.finish_run = flaky  # type: ignore[method-assign]
    await sched._poll_once(); await settle(sched)
    [first] = _real(sched)
    assert first.exec_status == "unknown" and "internal error" in (first.detail or "")
    assert store.get("s1").enabled and store.get("s1").state.consecutive_failures == 0
    # the schedule is dispatchable again: a later occurrence can be claimed
    nxt = sched._ledger.create_manual(schedule_id="s1", job_kind="agent_prompt", run_safety="side_effecting")
    assert sched._ledger.claim(nxt, owner=sched._owner, lease_seconds=30)


async def test_m9_even_if_the_abort_write_fails_the_orphan_is_recovered_by_the_next_poll(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=make_schedule(created=now_utc() - timedelta(minutes=3)))
    real = sched._ledger.finish_run
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    sched._ledger.finish_run = flaky  # type: ignore[method-assign]
    await sched._poll_once(); await settle(sched)
    [stuck] = _real(sched)
    assert stuck.exec_status == "running" and not sched._active_fires
    await sched._poll_once(); await settle(sched)
    assert sched._ledger.get(stuck.id).exec_status == "unknown"  # recovered, not "still alive"
    assert not _events(_read_audit_records(cfg.audit_dir), "audit.schedule.recovery_skipped")


async def test_m9_a_live_task_of_this_process_is_never_treated_as_an_orphan(tmp_path):
    cfg, store, router, adapter, sched = await build(
        tmp_path, router=Router("hang"), schedule=make_schedule(created=now_utc() - timedelta(minutes=3)))
    sched._ledger._conn.commit()
    await sched._poll_once(); await asyncio.sleep(0.05)
    [occ] = [o for o in sched._ledger.recent("s1") if o.exec_status == "running"]
    await sched._poll_once()
    assert sched._ledger.get(occ.id).exec_status == "running"
    for t in list(sched._inflight):
        t.cancel()
    await asyncio.gather(*list(sched._inflight), return_exceptions=True)
