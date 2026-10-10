# ruff: noqa: E702, B017
"""Durable occurrence/run/outbox ledger behavior for the scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from pathlib import Path

import pytest
import yaml

from anna.config import AnnaConfig
from anna.runtime.ledger import RuntimeLedger, iso, now_utc, split_chunks
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from anna.transports.base import InboundEvent, OutboundMessage, TurnCancelled
from tests.test_scheduler import FakeAdapter, FakeAlerter

UTC = UTC
EVERY_MIN = "* * * * *"


def make_cfg(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.scheduler.poll_interval_seconds = 1
    cfg.scheduler.cancel_grace_seconds = 0.05
    cfg.scheduler.delivery_backoff_base_seconds = 0.0
    cfg.logging.audit.fsync_on_write = False
    cfg.admin.slack_channel_id = "CADMIN"
    return cfg


def make_schedule(sid="s1", *, cron=EVERY_MIN, created=None, **kw) -> Schedule:
    return Schedule(id=sid, cron=cron, prompt="do it",
                    destination=ScheduleDestination(transport="slack", channel="CDEST"),
                    created_at=created or (now_utc() - timedelta(minutes=10)), **kw)


class Router:
    """Fake router. ``mode``: reply | hang | honor_cancel (stops on cancel)."""

    def __init__(self, mode="reply", reply="all done", tools=1):
        self.mode, self.reply, self.tools = mode, reply, tools
        self.events: list[InboundEvent] = []

    async def dispatch(self, event: InboundEvent) -> None:
        self.events.append(event)
        if self.mode == "hang":
            return
        if self.mode == "honor_cancel":
            if event.turn_meta is not None:
                event.turn_meta["turn_state"] = "running"
                event.turn_meta["tool_call_count"] = self.tools
            asyncio.get_running_loop().create_task(self._finish(event))
            return
        if event.turn_meta is not None:
            event.turn_meta["tool_call_count"] = self.tools
        event.completion_future.set_result(self.reply)

    async def _finish(self, event):
        await event.cancel.wait()
        event.turn_meta["turn_state"] = "finished"
        event.completion_future.set_result("partial")


async def build(tmp_path, *, router=None, schedule=None, adapter=None, cfg=None):
    cfg = cfg or make_cfg(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    if schedule is not False:
        await store.create(schedule or make_schedule())
    router = router or Router()
    adapter = adapter or FakeAdapter()
    sched = Scheduler(config=cfg, store=store, router=router, adapters={"slack": adapter}, alerter=FakeAlerter())
    return cfg, store, router, adapter, sched


async def settle(sched, rounds=60):
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        if not sched._inflight:
            return


# ---- uniqueness / cursor / misfire / overlap --------------------------------------------

async def test_poll_creates_one_unique_occurrence_and_never_replays(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    await sched._poll_once(); await settle(sched)
    occs = sched._ledger.recent("s1")
    assert len([o for o in occs if o.exec_status == "succeeded"]) == 1
    assert len(router.events) == 1 and len(adapter.sent) == 1
    # restart: a brand-new scheduler on the same files must not replay the old tick
    sched2 = Scheduler(config=cfg, store=store, router=router, adapters={"slack": adapter}, alerter=FakeAlerter())
    await sched2._poll_once(); await settle(sched2)
    assert len(router.events) == 1
    ledger = sched._ledger
    with pytest.raises(Exception):  # UNIQUE(schedule_id, scheduled_for)
        ledger._conn.execute("INSERT INTO schedule_occurrences(schedule_id,scheduled_for,job_kind,run_safety,"
                             "exec_status,created_at,updated_at) SELECT schedule_id,scheduled_for,job_kind,run_safety,"
                             "'pending','x','x' FROM schedule_occurrences LIMIT 1")


async def test_misfire_runs_only_newest_tick_after_downtime(tmp_path):
    cfg, store, router, adapter, sched = await build(
        tmp_path, schedule=make_schedule(created=now_utc() - timedelta(minutes=30)))
    await sched._poll_once(); await settle(sched)
    occs = sched._ledger.recent("s1", 100)
    ran = [o for o in occs if o.exec_status == "succeeded"]
    skipped = [o for o in occs if o.exec_status == "skipped_misfire"]
    assert len(ran) == 1 and len(skipped) >= 10 and len(router.events) == 1
    assert ran[0].scheduled_for == max(o.scheduled_for for o in occs)


async def test_skip_late_policy_drops_stale_tick(tmp_path):
    sch = make_schedule(cron="0 3 * * *", misfire_policy="skip_late", misfire_grace_seconds=60,
                        created=now_utc() - timedelta(days=2))
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=sch)
    # make sure the newest 03:00 tick is more than 60s old
    now = now_utc().replace(hour=12)
    sched._materialize(sch, now)
    statuses = {o.exec_status for o in sched._ledger.recent("s1")}
    assert statuses == {"skipped_misfire"}


async def test_overlap_skip_vs_queue(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("hang"),
                                                      schedule=make_schedule(timeout_seconds=300))
    ledger = sched._ledger
    await sched._poll_once(); await asyncio.sleep(0.02)  # first occurrence running (hangs)
    later = now_utc() + timedelta(minutes=2)
    sched._materialize(store.get("s1"), later)
    assert "skipped_overlap" in {o.exec_status for o in ledger.recent("s1")}
    # queue policy keeps exactly one waiter behind the active run
    await store.update("s1", overlap_policy="queue")
    sched._materialize(store.get("s1"), later + timedelta(minutes=2))
    sched._materialize(store.get("s1"), later + timedelta(minutes=4))
    statuses = [o.exec_status for o in ledger.recent("s1")]
    assert statuses.count("pending") == 1
    assert [o.id for o in ledger.pending()] == []  # waiter is blocked while another run is active
    for t in list(sched._inflight):
        t.cancel()
    await asyncio.gather(*sched._inflight, return_exceptions=True)


# ---- crash recovery ----------------------------------------------------------------------

async def test_crash_after_claim_before_run_is_repended_and_runs(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    sched._materialize(store.get("s1"), now_utc())
    [occ] = sched._ledger.pending()
    assert sched._ledger.claim(occ.id, owner="dead-process", lease_seconds=-1)  # lease already expired
    assert router.events == []  # crashed before dispatch
    await sched._poll_once(); await settle(sched)
    final = sched._ledger.get(occ.id)
    assert final.exec_status == "succeeded" and len(router.events) == 1


async def test_expired_running_side_effecting_is_unknown_and_never_rerun(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    sched._materialize(store.get("s1"), now_utc())
    [occ] = sched._ledger.pending()
    sched._ledger.claim(occ.id, owner="dead", lease_seconds=-1)
    sched._ledger._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?",
                                (iso(now_utc() + timedelta(seconds=-1)), occ.id))
    sched._ledger.start_run(occ.id, owner="dead")
    sched._ledger._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?",
                                (iso(now_utc() - timedelta(seconds=1)), occ.id)); sched._ledger._conn.commit()
    await sched._poll_once(); await settle(sched)
    got = sched._ledger.get(occ.id)
    assert got.exec_status == "unknown" and "not re-run" in got.detail
    assert router.events == [] and adapter.sent == []
    assert sched._ledger.runs(occ.id)[0]["status"] == "abandoned"
    assert store.get("s1").state.consecutive_failures == 1  # visible to the operator


async def test_expired_running_read_only_retries_then_gives_up(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, schedule=make_schedule(run_safety="read_only"))
    led = sched._ledger
    sched._materialize(store.get("s1"), now_utc())
    [occ] = led.pending()

    def crash():
        led.claim(occ.id, owner="dead", lease_seconds=300); led.start_run(occ.id, owner="dead")
        led._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?",
                          (iso(now_utc() - timedelta(seconds=1)), occ.id)); led._conn.commit()

    crash()
    recs = led.recover_expired(now=now_utc(), max_run_attempts=2)
    assert [r.action for r in recs] == ["repended"]
    crash()
    recs = led.recover_expired(now=now_utc(), max_run_attempts=2)
    assert [r.action for r in recs] == ["failed"]


# ---- timeouts / cancellation propagate ---------------------------------------------------

async def test_timeout_with_cooperative_worker_no_tools_is_timeout_not_unknown(tmp_path):
    cfg, store, router, adapter, sched = await build(
        tmp_path, router=Router("honor_cancel", tools=0), schedule=make_schedule(timeout_seconds=1))
    cfg.scheduler.cancel_grace_seconds = 1
    sch = store.get("s1"); sch.timeout_seconds = 0.05  # type: ignore[assignment]
    sched._materialize(sch, now_utc()); [occ] = sched._ledger.pending()
    await sched._guarded_fire(sch, occurrence_id=occ.id)
    got = sched._ledger.get(occ.id)
    assert got.exec_status == "timeout"
    assert router.events[0].cancel.is_set()  # cancellation reached the worker event
    assert sched._ledger.runs(occ.id)[0]["side_effects"] == "none"
    assert adapter.sent == []


async def test_timeout_after_tool_calls_is_unknown(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("honor_cancel", tools=2))
    cfg.scheduler.cancel_grace_seconds = 1
    sch = store.get("s1"); sch.timeout_seconds = 0.05  # type: ignore[assignment]
    sched._materialize(sch, now_utc()); [occ] = sched._ledger.pending()
    await sched._guarded_fire(sch, occurrence_id=occ.id)
    got = sched._ledger.get(occ.id)
    assert got.exec_status == "unknown" and "cannot be verified" in got.detail


async def test_timeout_without_worker_ack_is_blocked(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("hang"))
    sch = store.get("s1"); sch.timeout_seconds = 0.05  # type: ignore[assignment]
    sched._materialize(sch, now_utc()); [occ] = sched._ledger.pending()
    await sched._guarded_fire(sch, occurrence_id=occ.id)
    assert sched._ledger.get(occ.id).exec_status == "blocked"
    assert store.get("s1").state.consecutive_failures == 1


async def test_delete_running_cancels_turn_and_drops_unsent_output(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("honor_cancel", tools=0))
    store.set_cancel_callback(sched.cancel_schedule_tasks)
    await sched._poll_once(); await asyncio.sleep(0.02)
    assert router.events
    await store.delete("s1")
    await settle(sched)
    assert router.events[0].cancel.is_set()
    got = sched._ledger.recent("s1")[0]
    assert got.exec_status == "cancelled" and adapter.sent == []


async def test_delete_from_other_process_is_reconciled_on_poll(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("honor_cancel", tools=0))
    await sched._poll_once(); await asyncio.sleep(0.02)
    other = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))  # the dashboard process
    await other.delete("s1")
    await sched._poll_once(); await settle(sched)
    assert router.events[0].cancel.is_set()
    assert sched._ledger.recent("s1")[0].exec_status == "cancelled"


# ---- delivery is separate from execution -------------------------------------------------

async def test_send_failure_keeps_outbox_and_retry_does_not_rerun_execution(tmp_path):
    adapter = FakeAdapter(); adapter.send_should_raise = ConnectionError("net down")
    cfg, store, router, _, sched = await build(tmp_path, adapter=adapter)
    await sched._poll_once(); await settle(sched)
    occ = sched._ledger.recent("s1")[0]
    assert occ.exec_status == "succeeded" and occ.delivery_status == "pending"
    [row] = sched._ledger.outbox(occ.id)
    assert row["status"] == "pending" and row["attempts"] == 1 and "net down" in row["provider_outcome"]
    # daemon restarts: new ledger/scheduler objects over the same files; network is back
    sched._ledger.close()
    adapter.send_should_raise = None
    sched2 = Scheduler(config=cfg, store=store, router=router, adapters={"slack": adapter}, alerter=FakeAlerter())
    await sched2._poll_once(); await settle(sched2)
    occ = sched2._ledger.get(occ.id)
    assert occ.delivery_status == "delivered" and len(adapter.sent) == 1
    assert len(router.events) == 1  # execution was NOT re-run
    assert store.get("s1").state.consecutive_failures == 0


async def test_ambiguous_send_error_is_uncertain_and_never_resent(tmp_path):
    adapter = FakeAdapter(); adapter.send_should_raise = RuntimeError("socket closed mid-write")
    cfg, store, router, _, sched = await build(tmp_path, adapter=adapter)
    await sched._poll_once(); await settle(sched)
    occ = sched._ledger.recent("s1")[0]
    assert occ.delivery_status == "uncertain"
    adapter.send_should_raise = None
    await sched._poll_once(); await sched._deliver_due()
    assert adapter.sent == []
    assert store.get("s1").state.consecutive_failures == 1


async def test_interrupted_send_becomes_uncertain_after_lease(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    led = sched._ledger
    sched._materialize(store.get("s1"), now_utc()); [occ] = led.pending()
    led.claim(occ.id, owner="x", lease_seconds=60); run = led.start_run(occ.id, owner="x")
    led.finish_run(occ.id, run, status="succeeded", side_effects="none", outbox=[("slack", "CDEST", "hello")])
    [item] = led.claim_outbox(now=now_utc(), lease_seconds=-1)  # crashed mid-send
    assert led.recover_outbox(now=now_utc()) == 1
    assert led.get(occ.id).delivery_status == "uncertain" and led.claim_outbox(now=now_utc(), lease_seconds=60) == []


async def test_chunks_retry_only_the_failed_chunk_in_order(tmp_path):
    cfg = make_cfg(tmp_path); cfg.scheduler.delivery_chunk_chars = 20
    text = "\n".join(f"line number {i:02d}" for i in range(6))
    router = Router(reply=text)
    calls: list[str] = []

    class Flaky(FakeAdapter):
        fail_once = True

        async def send(self, message: OutboundMessage) -> None:
            if "line number 02" in message.text and self.fail_once:
                self.fail_once = False
                raise ConnectionError("blip")
            calls.append(message.text); self.sent.append(message)

    adapter = Flaky()
    cfg, store, router, _, sched = await build(tmp_path, router=router, adapter=adapter, cfg=cfg)
    await sched._poll_once(); await settle(sched)
    occ = sched._ledger.recent("s1")[0]
    rows = sched._ledger.outbox(occ.id)
    assert len(rows) >= 3 and rows[0]["status"] == "sent" and occ.delivery_status == "partial"
    assert [r["status"] for r in rows][2:] == ["pending"] * (len(rows) - 2) or rows[1]["status"] == "pending"
    await sched._poll_once()
    assert "".join(calls).replace("\n", "") == text.replace("\n", "")  # complete, ordered, no duplicates
    assert sched._ledger.get(occ.id).delivery_status == "delivered"


async def test_quiet_result_is_succeeded_with_suppressed_delivery_and_typed_result(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router(reply="[[ANNA_NO_OUTPUT]]", tools=1))
    await sched._poll_once(); await settle(sched)
    occ = sched._ledger.recent("s1")[0]
    assert (occ.exec_status, occ.delivery_status) == ("succeeded", "suppressed") and adapter.sent == []
    import json
    res = json.loads(sched._ledger.runs(occ.id)[0]["result_json"])
    assert res["outcome"] == "quiet" and res["quiet_reason"] == "quiet_sentinel" and res["tool_call_count"] == 1


def test_split_chunks_respects_limit_and_loses_nothing():
    text = "a" * 50 + "\n" + "b" * 10
    parts = split_chunks(text, 20)
    assert all(len(p) <= 20 for p in parts) and "".join(parts).replace("\n", "") == text.replace("\n", "")


# ---- store: cross-process safety, migration, rollback ------------------------------------

async def test_store_mutations_do_not_clobber_other_writers(tmp_path):
    cfg = make_cfg(tmp_path)
    daemon = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    dash = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await daemon.create(make_schedule("a")); await daemon.create(make_schedule("b"))
    await dash.load()  # stale snapshots on both sides
    await daemon.load()
    await dash.update("a", prompt="edited by dashboard")
    await daemon.mark_failed("b", reason="x", when=now_utc())  # stale cache, must not revert 'a'
    await dash.load()
    assert dash.get("a").prompt == "edited by dashboard"
    assert dash.get("b").state.consecutive_failures == 1


async def test_first_ledger_backs_up_yaml_and_legacy_files_load(tmp_path):
    cfg = make_cfg(tmp_path)
    legacy = {"version": 1, "schedules": [{
        "id": "old", "cron": EVERY_MIN, "prompt": "p", "timezone": "America/New_York",
        "destination": {"transport": "slack", "channel": "C1"}, "created_at": "2026-01-01T00:00:00+00:00",
        "state": {"last_fired_at": iso(now_utc() - timedelta(seconds=30)), "last_status": "complete",
                  "consecutive_failures": 0}}]}
    (tmp_path / "schedules.yaml").write_text(yaml.safe_dump(legacy))
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg)); await store.load()
    assert store.get("old").job_kind == "agent_prompt" and store.get("old").run_safety == "side_effecting"
    sched = Scheduler(config=cfg, store=store, router=Router(), adapters={"slack": FakeAdapter()}, alerter=FakeAlerter())
    backups = list(tmp_path.glob("schedules.yaml.bak-pre-ledger-*"))
    assert len(backups) == 1 and yaml.safe_load(backups[0].read_text()) == legacy
    # legacy last_fired_at seeds the cursor: ancient ticks since 2026-01-01 are NOT replayed
    await sched._poll_once(); await settle(sched)
    assert len([o for o in sched._ledger.recent("old", 100) if o.exec_status == "skipped_misfire"]) <= 1
    Scheduler(config=cfg, store=store, router=Router(), adapters={}, alerter=FakeAlerter())
    assert len(list(tmp_path.glob("schedules.yaml.bak-pre-ledger-*"))) == 1  # only once


def test_yaml_remains_loadable_by_old_schema_ignoring_new_fields(tmp_path):
    s = make_schedule(overlap_policy="queue", run_safety="read_only")
    dumped = s.model_dump(mode="json")
    for k in ("job_kind", "run_safety", "overlap_policy", "misfire_policy", "misfire_grace_seconds"):
        dumped.pop(k)  # what an old daemon would have written / will ignore
    assert Schedule.model_validate(dumped).overlap_policy is None


def test_ledger_reopen_preserves_state(tmp_path):
    path = tmp_path / "state" / "runtime.sqlite3"
    led = RuntimeLedger(path)
    oid = led.create_manual(schedule_id="s", job_kind="agent_prompt", run_safety="read_only")
    led.close()
    assert RuntimeLedger(path).get(oid).exec_status == "pending"


# ---- worker propagation (real ConversationWorker) ---------------------------------------

from tests.test_worker_periodic_flush import (  # noqa: E402
    _FakeAssistantMessage,
    _FakeResultMessage,
    _FakeTextBlock,
    _FakeToolUseBlock,
    _ImmediateClient,
    _make_event,
    _make_worker,
)


@pytest.fixture
def sdk_fakes(monkeypatch):
    import claude_agent_sdk as sdk
    for name, cls in (("AssistantMessage", _FakeAssistantMessage), ("ResultMessage", _FakeResultMessage),
                      ("TextBlock", _FakeTextBlock), ("ToolUseBlock", _FakeToolUseBlock)):
        monkeypatch.setattr(sdk, name, cls, raising=False)


async def test_worker_drops_cancelled_queued_event_without_running_it(tmp_path, sdk_fakes):
    worker = _make_worker(tmp_path, [])
    worker._client = _ImmediateClient([_FakeTextBlock(text="should not run")])
    ev = _make_event(future=asyncio.get_running_loop().create_future())
    cancel = asyncio.Event(); cancel.set()
    meta: dict = {}
    object.__setattr__(ev, "cancel", cancel); object.__setattr__(ev, "turn_meta", meta)
    worker._stopping = False
    await worker.submit(ev)
    run = asyncio.create_task(worker._run())
    with pytest.raises(TurnCancelled):
        await asyncio.wait_for(ev.completion_future, 2)
    run.cancel(); await asyncio.gather(run, return_exceptions=True)
    assert meta["turn_state"] == "dropped" and worker._client.queries == []


async def test_worker_interrupts_running_turn_on_cancel(tmp_path, sdk_fakes):
    sent: list = []
    worker = _make_worker(tmp_path, sent)
    gate = asyncio.Event()

    class Client(_ImmediateClient):
        interrupted = False

        async def interrupt(self):
            self.interrupted = True
            gate.set()

        async def receive_response(self):
            await gate.wait()
            yield _FakeAssistantMessage(content=[_FakeTextBlock(text="partial")])
            yield _FakeResultMessage()

    client = Client([])
    worker._client = client
    ev = _make_event(future=asyncio.get_running_loop().create_future())
    cancel = asyncio.Event(); meta: dict = {}
    object.__setattr__(ev, "cancel", cancel); object.__setattr__(ev, "turn_meta", meta)
    turn = asyncio.create_task(worker._handle(ev))
    await asyncio.sleep(0.05)
    assert meta["turn_state"] == "running" and not client.interrupted
    cancel.set()
    await asyncio.wait_for(turn, 5)
    assert client.interrupted and meta["cancel_requested"] and meta["turn_state"] == "finished"
