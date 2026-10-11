# ruff: noqa: E702, E701, ASYNC251, F841
"""Regressions for the cf72508 integration review (H1, M1-M5, L1-L7) plus the dashboard review notices."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, timedelta
from types import SimpleNamespace

import aiohttp
import httpx
import pytest

from anna.runtime.heartbeat import REANNOUNCE_PREFIX
from anna.runtime.ledger import RuntimeLedger, iso, new_owner_id, now_utc, owner_alive
from anna.runtime.schedule_store import ScheduleStore, ScheduleValidationError
from anna.runtime.scheduler import classify_send_error
from anna.runtime.supervisor import Supervisor
from tests.test_heartbeat import Env, HttpError, hb_schedule, make_cfg
from tests.test_scheduler_ledger import Router, build, make_schedule, settle

UTC = UTC


def connect_error():
    key = SimpleNamespace(host="slack.com", port=443, ssl=True, is_ssl=True)
    return aiohttp.ClientConnectorError(key, OSError(111, "Connect call failed"))


def chained(outer: Exception, inner: Exception) -> Exception:
    outer.__cause__ = inner
    return outer


# ---- H1 -----------------------------------------------------------------------------------------

def test_h1_connect_phase_errors_are_proof_of_non_delivery_and_ambiguous_ones_are_not():
    import telegram.error as te
    yes = [connect_error(), aiohttp.ClientProxyConnectionError(SimpleNamespace(host="p", port=1, ssl=False, is_ssl=False), OSError()),
           httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x"), ConnectionRefusedError(),
           chained(te.NetworkError("wrapped"), httpx.ConnectError("refused")),
           chained(te.TimedOut("connect timed out"), httpx.ConnectTimeout("t")), te.RetryAfter(3)]
    no = [aiohttp.ServerDisconnectedError(), httpx.ReadTimeout("x"), httpx.WriteError("x"), ConnectionResetError(),
          BrokenPipeError(), ConnectionError("generic"), te.NetworkError("plain"), te.TimedOut("read timed out"),
          chained(te.NetworkError("w"), httpx.ReadError("r")), RuntimeError("socket closed mid-write")]
    assert all(classify_send_error(e) for e in yes), [type(e).__name__ for e in yes if not classify_send_error(e)]
    assert not any(classify_send_error(e) for e in no), [type(e).__name__ for e in no if classify_send_error(e)]


async def test_h1_connect_failure_is_retried_and_alert_delivered_exactly_once(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()
    await env.tick()
    env.prov.add_mail("personal", "m1", subject="Wire transfer due")
    env.adapter.send_should_raise = connect_error()
    occ = await env.tick()
    assert env.ledger.get(occ.id).delivery_status == "pending" and env.texts() == []        # not uncertain
    calls = len(env.prov.calls)
    env.adapter.send_should_raise = None
    await env.sched._deliver_due()
    assert [t for t in env.texts() if "Wire transfer" in t].__len__() == 1 and len(env.prov.calls) == calls


async def test_h1_uncertain_heartbeat_alert_is_operator_visible_and_re_announced_once(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()
    await env.tick()
    env.prov.add_mail("personal", "m1", subject="Contract signature needed")
    env.adapter.send_should_raise = RuntimeError("socket closed mid-write")                  # genuinely ambiguous
    occ = await env.tick()
    assert env.ledger.get(occ.id).delivery_status == "uncertain"
    assert any("uncertain" in w and "heartbeat" in w.lower() for w in env.sched._alerter.warned)   # admin told now
    env.adapter.send_should_raise = None
    await env.tick()
    announce = [t for t in env.texts() if t.startswith(REANNOUNCE_PREFIX)]
    assert len(announce) == 1 and "Contract signature needed" in announce[0]               # user told on next tick
    await env.tick(); await env.tick()
    assert len([t for t in env.texts() if t.startswith(REANNOUNCE_PREFIX)]) == 1            # never repeats forever


# ---- M1 -----------------------------------------------------------------------------------------

def quiet_cfg(tmp_path, limit=2, deferred=50):
    cfg = make_cfg(tmp_path, ("personal",))
    cfg.heartbeat.quiet_hours.enabled = True
    cfg.heartbeat.quiet_hours.start, cfg.heartbeat.quiet_hours.end = "00:00", "23:59"      # always quiet
    cfg.heartbeat.max_pending_notifications = limit
    cfg.heartbeat.max_deferred_notifications = deferred
    return cfg


async def test_m1_deferred_quiet_hours_backlog_does_not_block_urgent_or_auth_alerts(tmp_path):
    env = await Env(tmp_path, cfg=quiet_cfg(tmp_path), accounts=("personal",)).start()
    await env.tick()
    for mid in ("m1", "m2", "m3"):
        env.prov.add_mail("personal", mid); await env.tick()
    assert env.ledger.deferred_outbox_count("hb") >= 3 and env.ledger.pending_outbox_count("hb") == 0
    env.prov.add_event("personal", "e1", "Board call", (now_utc() + timedelta(minutes=10)).isoformat())
    occ = await env.tick()
    cal = [r for r in env.ledger.receipts(occ.id) if r["service"] == "calendar"][0]
    assert cal["status"] == "ok" and any("Board call" in t and "min" in t for t in env.texts())     # urgent delivered now
    env.prov.fail[("personal", "gmail")] = HttpError(401)
    occ2 = await env.tick()
    st = env.ledger.heartbeat_state("hb", "personal", "gmail")
    assert st["status"] == "auth_required" and st["alerts_sent"] == 1
    assert any("re-authorization" in r["text"] for r in env.ledger.outbox(occ2.id))                 # queued (held to morning)


async def test_m1_ordinary_backlog_is_truly_bounded_but_nothing_is_lost(tmp_path):
    env = await Env(tmp_path, cfg=quiet_cfg(tmp_path, limit=50, deferred=3), accounts=("personal",)).start()
    await env.tick()
    for i in range(6):
        env.prov.add_mail("personal", f"m{i}", subject=f"S{i}"); await env.tick()
    assert env.ledger.deferred_outbox_count("hb") <= 3                                     # bounded
    gm = env.ledger.heartbeat_state("hb", "personal", "gmail")
    assert int(gm["cursor"]) < env.prov.head["personal"]                                    # withheld mail is re-read, not skipped
    env.ledger._conn.execute("UPDATE schedule_outbox SET status='sent'"); env.ledger._conn.commit()
    await env.tick()
    seen = "".join(r["text"] for r in env.ledger._conn.execute("SELECT text FROM schedule_outbox"))
    assert all(f"S{i}" in seen for i in range(6))


# ---- M2 -----------------------------------------------------------------------------------------

async def test_m2_job_timeouts_alert_once_then_recover_and_never_look_healthy(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, ("personal",))
    env = await Env(tmp_path, cfg=cfg, accounts=("personal",)).start()
    orig = env.prov.run_for()

    async def hang(slug, api, op, timeout_seconds=30.0):
        await asyncio.sleep(30)

    import anna.runtime.scheduler as sm
    monkeypatch.setattr(sm, "heartbeat_min_timeout", lambda c: 0.0)                       # let a short whole-job budget through
    env.store._check_job_kind = lambda s: None
    await env.store.update("hb", timeout_seconds=1)
    env.sched._google_clients = SimpleNamespace(run=hang)
    statuses = [(await env.tick()).exec_status for _ in range(5)]
    assert statuses == ["timeout"] * 5                                                       # whole job exceeded its budget
    alerts = [t for t in env.texts() if "could not complete" in t]
    assert len(alerts) == 1 and "timed out" in alerts[0]                                     # exactly one notice for N failures
    env.sched._google_clients = SimpleNamespace(run=orig)
    await env.tick()
    assert len([t for t in env.texts() if "working again" in t]) == 1
    await env.tick()
    assert len([t for t in env.texts() if "working again" in t]) == 1


async def test_m2_missing_client_and_exception_are_alerted(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()
    env.sched._google_clients = None
    occ = await env.tick()
    assert occ.exec_status == "failed" and any("could not complete" in t and "no Google client" in t for t in env.texts())
    await env.tick(); await env.tick()
    assert len([t for t in env.texts() if "could not complete" in t]) == 1                  # alerts on entry only
    env2 = await Env(tmp_path / "b", accounts=("personal",)).start()

    class Boom:
        async def run(self, *a, **k): raise RuntimeError("kaboom")

    env2.sched._google_clients = Boom()
    occ2 = await env2.tick()                                                                # collector errors become per-check receipts
    assert occ2.exec_status in ("partial", "failed")
    assert env2.ledger.heartbeat_state("hb", "personal", "gmail")["status"] == "api_error"


async def test_m2_short_heartbeat_timeout_is_rejected_and_clamped(tmp_path):
    cfg = make_cfg(tmp_path, ("personal",))
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    short = hb_schedule("short"); short.timeout_seconds = 60
    with pytest.raises(ScheduleValidationError, match="timeout_seconds must be at least"):
        await store.create(short)
    from anna.runtime.heartbeat import heartbeat_min_timeout
    assert heartbeat_min_timeout(cfg.heartbeat) == 210.0


# ---- M3 -----------------------------------------------------------------------------------------

async def test_m3_transient_empty_or_truncated_yaml_does_not_cancel_anything(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("hang"))
    await store.create(make_schedule("s2"))
    led = sched._ledger
    sched._materialize(store.get("s1"), now_utc() + timedelta(minutes=1))
    occ = led.create_manual(schedule_id="s2", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(occ, owner="x", lease_seconds=60); run = led.start_run(occ, owner="x")
    led.finish_run(occ, run, status="succeeded", side_effects="none", outbox=[("slack", "CDEST", "keep me")])
    led._conn.execute("UPDATE schedule_outbox SET next_attempt_at=?", (iso(now_utc() + timedelta(hours=1)),)); led._conn.commit()
    path = cfg.scheduler.resolved_state_path
    good = path.read_text()
    for broken in ("", "schedules: []\n", "version: 1\n"):
        path.write_text(broken)
        await sched._poll_once()                                                                      # ONE bad read
        path.write_text(good)
        await sched._poll_once()                                                                      # file restored
    path.unlink()
    await sched._poll_once(); path.write_text(good); await sched._poll_once()
    assert [r["status"] for r in led.outbox(occ)] == ["pending"]                                      # nothing was cancelled
    assert led.get(occ).delivery_status == "pending"
    for t in list(sched._inflight): t.cancel()
    await asyncio.gather(*sched._inflight, return_exceptions=True)


async def test_m3_confirmed_deletion_still_cancels_after_two_consecutive_absences(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    led = sched._ledger
    occ = led.create_manual(schedule_id="s1", job_kind="agent_prompt", run_safety="side_effecting")
    path = cfg.scheduler.resolved_state_path
    path.write_text("version: 1\nschedules: []\n")                                                     # operator really deleted it
    await sched._poll_once()
    assert led.get(occ).exec_status == "pending"
    await sched._poll_once()
    assert led.get(occ).exec_status == "cancelled"


# ---- M4 -----------------------------------------------------------------------------------------

async def test_m4_sender_subject_and_calendar_fields_are_inert_in_slack_without_double_escaping(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()
    env.prov.add_mail("personal", "m1", subject="<!channel> pay <https://evil.test|Verify>", sender="IT <it@evil.test> & co <@U1>")
    env.prov.add_event("personal", "e1", "<!here> invite & more <https://x.test|Join>", (now_utc() + timedelta(minutes=5)).isoformat())
    await env.tick()
    blob = "\n".join(env.texts())
    assert "<!channel>" not in blob and "<!here>" not in blob and "<@U1>" not in blob and "<https" not in blob
    assert "&lt;!channel&gt;" in blob and "&lt;!here&gt;" in blob and "IT &lt;it@evil.test&gt; &amp; co" in blob
    assert "&amp;amp;" not in blob and "&amp;lt;" not in blob                                           # escaped exactly once
    assert all(m.plain for m in env.adapter.sent)


# ---- M5 -----------------------------------------------------------------------------------------

async def test_m5_shutdown_requeues_read_only_runs_and_they_run_after_restart(tmp_path):
    sch = make_schedule("ro", run_safety="read_only")
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("hang"), schedule=sch)
    cfg.scheduler.cancel_grace_seconds = 0.05
    await sched._poll_once(); await asyncio.sleep(0.05)
    assert len(router.events) == 1
    await sched.shutdown()
    assert sched._shutting_down
    for t in list(sched._inflight):
        t.cancel()
    await asyncio.sleep(0.2)
    occ = sched._ledger.recent("ro")[0]
    assert occ.exec_status == "pending" and "shutdown" in occ.detail
    audits = "".join(p.read_text() for p in cfg.audit_dir.glob("*.jsonl"))
    assert "audit.schedule.shutdown_interrupted" in audits and "repended" in audits
    # restart: a new scheduler over the same files runs the SAME occurrence
    sched._ledger.close()
    from anna.runtime.scheduler import Scheduler
    from tests.test_scheduler import FakeAlerter
    router2 = Router("reply")
    sched2 = Scheduler(config=cfg, store=store, router=router2, adapters={"slack": adapter}, alerter=FakeAlerter())
    await sched2._poll_once(); await settle(sched2)
    assert sched2._ledger.get(occ.id).exec_status == "succeeded" and len(router2.events) == 1


async def test_m5_shutdown_reports_side_effecting_work_as_unknown_not_silent(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path, router=Router("hang"))
    cfg.scheduler.cancel_grace_seconds = 0.05
    await sched._poll_once(); await asyncio.sleep(0.05)
    await sched.shutdown()
    for t in list(sched._inflight):
        t.cancel()
    await asyncio.sleep(0.2)
    occ = sched._ledger.recent("s1")[0]
    assert occ.exec_status in ("blocked", "unknown")                                                # never re-pended
    audits = [json.loads(line) for p in cfg.audit_dir.glob("*.jsonl") for line in p.read_text().splitlines()]
    assert any(a.get("event") == "audit.schedule.shutdown_interrupted" and a.get("kind") == "unknown" for a in audits)
    await store.load()
    # a restart interruption is infrastructure, not a job failure: it is audited but never counts toward auto-disable
    assert store.get("s1").state.consecutive_failures == 0
    assert store.get("s1").state.last_fired_at is not None


async def test_m5_heartbeat_shutdown_is_requeued_not_dropped(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()

    async def hang(*a, **k): await asyncio.sleep(30)

    env.sched._google_clients = SimpleNamespace(run=hang)
    occ_id = env.ledger.create_manual(schedule_id="hb", job_kind="google_heartbeat", run_safety="read_only")
    task = env.sched._spawn(env.store.get("hb"), occ_id)
    await asyncio.sleep(0.1)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    assert env.ledger.get(occ_id).exec_status == "pending"


# ---- L1 / L2 --------------------------------------------------------------------------------------

def seed_chunks(led, n=3):
    occ = led.create_manual(schedule_id="s", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(occ, owner="x", lease_seconds=60); run = led.start_run(occ, owner="x")
    led.finish_run(occ, run, status="succeeded", side_effects="none",
                   outbox=[("slack", "C", "\n".join(f"line {i:02d} " + "x" * 20 for i in range(n * 3)))], chunk_chars=60)
    return occ


def test_l1_later_chunks_are_cancelled_when_an_earlier_one_ends_without_being_sent(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3"); occ = seed_chunks(led)
    [first] = led.claim_outbox(now=now_utc(), limit=1, lease_seconds=60)
    led.outbox_failed(first, error="ambiguous", retryable=False, max_attempts=3, backoff_base=0, backoff_max=0)
    rows = [r["status"] for r in led.outbox(occ)]
    assert rows[0] == "uncertain" and set(rows[1:]) == {"cancelled"} and len(rows) >= 3
    assert led.get(occ).delivery_status == "uncertain" and "s" not in led.active_schedule_ids()
    assert led.pending_outbox_count("s") == 0


def test_l1_recover_outbox_strands_the_remainder_too(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3"); occ = seed_chunks(led)
    led.claim_outbox(now=now_utc(), limit=1, lease_seconds=-1)
    assert led.recover_outbox(now=now_utc()) == 1
    assert {r["status"] for r in led.outbox(occ)[1:]} == {"cancelled"}


def test_l2_unattempted_rows_are_never_leased_or_marked_uncertain(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3")
    for _ in range(3):
        o = led.create_manual(schedule_id="s", job_kind="agent_prompt", run_safety="side_effecting"); led.claim(o, owner="x", lease_seconds=60)
        r = led.start_run(o, owner="x")
        led.finish_run(o, r, status="succeeded", side_effects="none", outbox=[("slack", "C", f"msg {o}")])
    got = led.claim_outbox(now=now_utc(), lease_seconds=-1, limit=1)                       # lease already expired
    assert len(got) == 1
    assert led.recover_outbox(now=now_utc()) == 1                                           # only the one actually in flight
    assert sorted(r[0] for r in led._conn.execute("SELECT status FROM schedule_outbox")) == ["pending", "pending", "uncertain"]


async def test_l2_deliver_due_sends_each_row_with_its_own_lease(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()
    for i in range(4):
        env.prov.add_mail("personal", f"m{i}", subject=f"S{i}", record=True)
    await env.tick()
    assert len(env.adapter.sent) >= 1
    assert env.ledger._conn.execute("SELECT COUNT(*) FROM schedule_outbox WHERE status='uncertain'").fetchone()[0] == 0


# ---- L3 -------------------------------------------------------------------------------------------

def test_l3_failed_migration_rolls_back_completely_and_reopens(tmp_path, monkeypatch):
    path = tmp_path / "r.sqlite3"
    real = RuntimeLedger._migrate_v2
    calls = {"n": 0}

    def crash(self):
        calls["n"] += 1
        with self._immediate():
            self._conn.execute("ALTER TABLE schedule_outbox RENAME TO schedule_outbox_v1")
            raise RuntimeError("power loss mid-migration")

    monkeypatch.setattr(RuntimeLedger, "_migrate_v2", crash)
    with pytest.raises(RuntimeError):
        RuntimeLedger(path)
    monkeypatch.setattr(RuntimeLedger, "_migrate_v2", real)
    led = RuntimeLedger(path)                                                                # was: "table already exists"
    assert led._conn.execute("SELECT MAX(version) FROM ledger_migrations").fetchone()[0] == 5
    assert led._conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='schedule_outbox_v1'").fetchone()[0] == 0


def test_l3_database_left_half_migrated_by_the_old_non_atomic_code_is_repaired(tmp_path):
    path = tmp_path / "r.sqlite3"
    led = RuntimeLedger(path)
    c = led._conn
    c.execute("DELETE FROM ledger_migrations WHERE version IN (2,3,4)")
    for t in ("heartbeat_state", "heartbeat_notified", "heartbeat_receipts", "repair_requests"):
        c.execute(f"DROP TABLE {t}")
    # state after the old code died between RENAME and COPY: original table renamed, new one created empty
    c.execute("ALTER TABLE schedule_outbox RENAME TO schedule_outbox_v1")
    c.execute("CREATE TABLE schedule_outbox (id INTEGER PRIMARY KEY, occurrence_id INTEGER, junk TEXT)")
    c.commit(); led.close()
    led2 = RuntimeLedger(path)
    cols = {r[1] for r in led2._conn.execute("PRAGMA table_info(schedule_outbox)")}
    assert {"msg_index", "chunk_index", "announced"} <= cols and "junk" not in cols


# ---- L5 -------------------------------------------------------------------------------------------

def test_l5_dead_owner_recovered_immediately_live_owner_never_stolen(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3")

    def run_with(owner, lease):
        o = led.create_manual(schedule_id=f"s{owner[:6]}", job_kind="agent_prompt", run_safety="read_only")
        led.claim(o, owner=owner, lease_seconds=lease); led.start_run(o, owner=owner)
        return o

    dead = run_with("999999999-123-aaaaaaaa", 3600)                                          # lease NOT expired, pid gone
    mine = new_owner_id(); live = run_with(mine, -5)                                          # lease expired, owner alive
    legacy = run_with("legacy-owner", -5)                                                     # unparsable: expiry rule
    out = {r.occurrence_id: r for r in led.recover_expired(now=now_utc(), max_run_attempts=3, owner_alive=owner_alive)}
    assert out[dead].action == "repended" and out[legacy].action == "repended"
    assert out[live].action == "skipped" and "still alive" in out[live].detail
    assert led.get(live).exec_status == "running" and led.get(dead).exec_status == "pending"
    assert owner_alive(mine) is True and owner_alive("999999999-123-aaaaaaaa") is False and owner_alive("x") is None


def test_l5_pid_reuse_is_detected_by_start_time(tmp_path):
    import os
    pid = os.getpid()
    assert owner_alive(f"{pid}-1-abc") is False                                               # same pid, different start time


# ---- L6 -------------------------------------------------------------------------------------------

def test_l6_retention_prunes_old_terminal_rows_and_protects_unresolved(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3")
    old = iso(now_utc() - timedelta(days=60))

    def make(status, outbox_status=None, text="body"):
        o = led.create_manual(schedule_id=f"s{status}{outbox_status}", job_kind="agent_prompt", run_safety="side_effecting")
        led.claim(o, owner="x", lease_seconds=60); r = led.start_run(o, owner="x")
        led.finish_run(o, r, status="succeeded", side_effects="none", outbox=[("slack", "C", text)] if outbox_status else None,
                       result={"notifications": ["secret subject preview"]})
        if outbox_status:
            led._conn.execute("UPDATE schedule_outbox SET status=? WHERE occurrence_id=?", (outbox_status, o))
        led._conn.execute("UPDATE schedule_occurrences SET exec_status=?, finished_at=?, created_at=? WHERE id=?", (status, old, old, o))
        led._conn.execute("UPDATE schedule_outbox SET updated_at=? WHERE occurrence_id=?", (old, o))
        led._conn.execute("UPDATE schedule_runs SET finished_at=? WHERE occurrence_id=?", (old, o))
        led._conn.commit(); return o

    done, sent = make("succeeded"), make("succeeded", "sent")
    unknown, uncertain, pending, failed = make("unknown"), make("succeeded", "uncertain"), make("succeeded", "pending"), make("failed", "failed")
    recent = led.create_manual(schedule_id="fresh", job_kind="agent_prompt", run_safety="read_only")
    counts = led.prune(now=now_utc(), retention_days=30, text_retention_days=7)
    alive = {r[0] for r in led._conn.execute("SELECT id FROM schedule_occurrences")}
    assert done not in alive and sent not in alive and counts["occurrences"] == 2
    assert {unknown, uncertain, pending, failed, recent} <= alive                              # unresolved are protected
    assert led._conn.execute("SELECT text FROM schedule_outbox WHERE occurrence_id=?", (uncertain,)).fetchone()[0] == "body"


def test_l6_text_and_notification_previews_are_purged_earlier_than_rows(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3")
    o = led.create_manual(schedule_id="s", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(o, owner="x", lease_seconds=60); r = led.start_run(o, owner="x")
    led.finish_run(o, r, status="succeeded", side_effects="none", outbox=[("slack", "C", "Sender — Subject")],
                   result={"notifications": ["Sender — Subject preview"], "status": "success"})
    led._conn.execute("UPDATE schedule_outbox SET status='sent', updated_at=?", (iso(now_utc() - timedelta(days=10)),))
    led._conn.execute("UPDATE schedule_runs SET finished_at=?", (iso(now_utc() - timedelta(days=10)),)); led._conn.commit()
    counts = led.prune(now=now_utc(), retention_days=30, text_retention_days=7)
    assert counts["outbox_text_purged"] == 1 and counts["results_redacted"] == 1 and counts["occurrences"] == 0
    assert led._conn.execute("SELECT text FROM schedule_outbox").fetchone()[0] == "[purged]"
    assert "Subject" not in led._conn.execute("SELECT result_json FROM schedule_runs").fetchone()[0]


def test_l6_pending_repair_request_protects_its_target(tmp_path):
    led = RuntimeLedger(tmp_path / "r.sqlite3")
    o = led.create_manual(schedule_id="s", job_kind="agent_prompt", run_safety="side_effecting")
    led._conn.execute("UPDATE schedule_occurrences SET exec_status='succeeded', created_at=?, finished_at=?", ((iso(now_utc() - timedelta(days=90)),) * 2))
    led._conn.execute("INSERT INTO repair_requests(kind,target_id,action,expected_status,requested_by,requested_at) VALUES('run',?,'accept_unknown','unknown','t','x')", (o,))
    led._conn.commit()
    assert led.prune(now=now_utc(), retention_days=30, text_retention_days=7)["occurrences"] == 0


async def test_l6_scheduler_poll_runs_pruning_on_its_interval(tmp_path):
    cfg, store, router, adapter, sched = await build(tmp_path)
    old = iso(now_utc() - timedelta(days=90))
    o = sched._ledger.create_manual(schedule_id="s1", job_kind="agent_prompt", run_safety="side_effecting")
    sched._ledger._conn.execute("UPDATE schedule_occurrences SET exec_status='succeeded', created_at=?, finished_at=? WHERE id=?", (old, old, o))
    sched._ledger._conn.commit()
    await sched._poll_once(); await settle(sched)
    assert sched._ledger._conn.execute("SELECT COUNT(*) FROM schedule_occurrences WHERE created_at=?", (old,)).fetchone()[0] == 0


# ---- L7 -------------------------------------------------------------------------------------------

async def test_l7_bounded_resync_says_older_unread_is_not_pending(tmp_path):
    cfg = make_cfg(tmp_path, ("personal",)); cfg.heartbeat.gmail_page_size = 2; cfg.heartbeat.gmail_max_pages = 1
    env = await Env(tmp_path, cfg=cfg, accounts=("personal",)).start()
    for i in range(5):
        env.prov.add_mail("personal", f"m{i}", subject=f"S{i}", record=False)
    occ = await env.tick()
    text = "\n".join(env.texts())
    assert "continuing on the next check" not in text and "older unread mail from this resync is not shown" in text
    rc = [r for r in env.ledger.receipts(occ.id) if r["service"] == "gmail"][0]
    assert rc["status"] == "partial" and '"bootstrap_truncated": true' in rc["detail_json"]


async def test_l7_history_pages_without_records_are_bounded_and_reported(tmp_path):
    cfg = make_cfg(tmp_path, ("personal",)); cfg.heartbeat.gmail_max_empty_pages = 3
    env = await Env(tmp_path, cfg=cfg, accounts=("personal",)).start()
    await env.tick()
    orig = env.prov.run_for()

    class EmptyPages:
        """history.list keeps returning empty pages with a nextPageToken."""
        async def run(self, slug, api, op, timeout_seconds=30.0):
            svc = SimpleNamespace()
            def history():
                return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {"nextPageToken": "t", "historyId": "999"}))
            svc.users = lambda: SimpleNamespace(history=history)
            if api == "gmail":
                return op(svc)
            return await orig(slug, api, op)

    env.sched._google_clients = EmptyPages()
    before = env.ledger.heartbeat_state("hb", "personal", "gmail")["cursor"]
    occ = await env.tick()
    rc = [r for r in env.ledger.receipts(occ.id) if r["service"] == "gmail"][0]
    detail = json.loads(rc["detail_json"])
    assert rc["status"] == "partial" and detail["pages"] == 3 and "scan incomplete" in detail["scan_incomplete"]
    assert env.ledger.heartbeat_state("hb", "personal", "gmail")["cursor"] == before         # never skips unread history


async def test_l7_time_budget_steps_cursor_back_so_unfetched_mail_is_read_next_tick(tmp_path):
    cfg = make_cfg(tmp_path, ("personal",)); cfg.heartbeat.gmail_time_budget_seconds = 5
    env = await Env(tmp_path, cfg=cfg, accounts=("personal",)).start()
    await env.tick()
    for i in range(4):
        env.prov.add_mail("personal", f"m{i}", subject=f"S{i}")
    clock = {"t": 0.0}
    import anna.runtime.heartbeat as hbm
    orig_init = hbm.HeartbeatJob.__init__

    def slow_init(self, **kw):
        orig_init(self, **kw)
        def mono():
            clock["t"] += 3.0                                    # each message fetch "takes" 3s
            return clock["t"]
        self._monotonic = mono

    hbm.HeartbeatJob.__init__ = slow_init
    try:
        occ = await env.tick()
        rc = [r for r in env.ledger.receipts(occ.id) if r["service"] == "gmail"][0]
        assert rc["status"] == "partial" and "time budget" in json.loads(rc["detail_json"])["scan_incomplete"]
        first = "\n".join(env.texts())
        assert 0 < first.count("•") < 4
        for _ in range(6):
            clock["t"] = 0.0
            await env.tick()
    finally:
        hbm.HeartbeatJob.__init__ = orig_init
    allmail = "\n".join(env.texts())
    assert all(allmail.count(f"S{i}") == 1 for i in range(4))                                 # each exactly once, none skipped


# ---- L4 -------------------------------------------------------------------------------------------

def test_l4_unseen_history_is_recorded_as_unsummarized_instead_of_freezing_coverage(tmp_path, monkeypatch):
    from anna.config import AnnaConfig
    from anna.log import transcript_event
    from anna.vault.checkpoint import write_checkpoint
    from anna.vault.transcript_resume import (
        uncovered_sequences_before,
        validated_checkpoint_coverage,
    )
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", tmp_path); cfg.vault.path = str(tmp_path / "v")
    key = "slack:dm:U1"
    for i in range(20):                                                                        # a long session nobody summarised
        transcript_event("slack", key, transcripts_dir=cfg.transcripts_dir, direction="inbound", text=f"old {i}")
    new = transcript_event("slack", key, transcripts_dir=cfg.transcripts_dir, direction="inbound", text="new")
    tail_seen = {20}                                                                           # only the newest line was shown
    gap = uncovered_sequences_before(cfg.transcripts_dir, key, new["sequence"], None, seen_sequences=tail_seen)
    assert len(gap) == 19
    path = write_checkpoint(vault_root=cfg.vault.resolved_path, transport="slack", conversation_key=key, summary="s",
                            coverage_end_sequence=new["sequence"], coverage_end_event_id=new["event_id"],
                            unsummarized_events=len(gap))
    text = path.read_text()
    assert "coverage_unsummarized_events: 19" in text and "NOT summarized" in text             # honest about it
    assert validated_checkpoint_coverage(cfg.vault.resolved_path, cfg.transcripts_dir, key) == new["sequence"]
    nxt = transcript_event("slack", key, transcripts_dir=cfg.transcripts_dir, direction="inbound", text="next")
    assert uncovered_sequences_before(cfg.transcripts_dir, key, nxt["sequence"], new["sequence"]) == []   # no longer frozen


async def test_l4_worker_advances_coverage_over_an_unseen_gap_and_writes_the_note(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk

    from anna.log import transcript_event
    from tests.test_worker_periodic_flush import (
        _FakeAssistantMessage,
        _FakeResultMessage,
        _FakeTextBlock,
        _FakeToolUseBlock,
        _ImmediateClient,
        _make_event,
        _make_worker,
    )
    for name, cls in (("AssistantMessage", _FakeAssistantMessage), ("ResultMessage", _FakeResultMessage),
                      ("TextBlock", _FakeTextBlock), ("ToolUseBlock", _FakeToolUseBlock)):
        monkeypatch.setattr(sdk, name, cls, raising=False)
    w = _make_worker(tmp_path, [])
    w._client = _ImmediateClient([_FakeTextBlock(text="ok")])
    for i in range(15):
        transcript_event("slack", w.conversation_key, transcripts_dir=w._config.transcripts_dir, direction="inbound", text=f"old {i}")
    rec = transcript_event("slack", w.conversation_key, transcripts_dir=w._config.transcripts_dir, direction="inbound", text="now")
    ev = _make_event(); ev.raw.update(anna_transcript_sequence=rec["sequence"], anna_transcript_event_id=rec["event_id"])
    await w._handle(ev)
    assert w._last_processed_transcript_sequence == rec["sequence"] and w._coverage_unsummarized == 15
    path = await w._write_checkpoint_now("summary", kind="closeout", coverage_end_sequence=rec["sequence"],
                                         coverage_end_event_id=rec["event_id"])
    assert "coverage_unsummarized_events: 15" in path.read_text()


# ---- dashboard review notices ---------------------------------------------------------------------------

def _web_client(tmp_path, host="127.0.0.1"):
    from fastapi.testclient import TestClient

    from anna.config import AnnaConfig
    from anna_web.app import create_app
    home = tmp_path / "home"; (home / "state").mkdir(parents=True)
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", home)
    return TestClient(create_app(cfg), follow_redirects=False, base_url="http://127.0.0.1:8765"), home


def test_dashboard_banners_cannot_be_forged_through_the_url(tmp_path):
    c, home = _web_client(tmp_path)
    crafted = "Verify+your+account+at+evil.test"
    for url in (f"/repairs?msg={crafted}&err={crafted}", f"/memory?msg={crafted}&err={crafted}&flash=whatever",
                "/repairs?flash=queued&n=-5"):
        page = c.get(url).text
        assert "evil.test" not in page and "Verify" not in page
    assert "Queued for the daemon" in c.get("/repairs?flash=queued&n=7").text
    assert c.post("/repairs/<script>/1", data={"action": "x", "expected_status": "unknown", "confirm": "y"}).status_code == 404
    assert c.post("/repairs/run/1", data={"action": "<b>evil</b>", "expected_status": "unknown", "confirm": "y"}).status_code == 404


def test_dashboard_rejects_foreign_host_headers_dns_rebinding(tmp_path):
    c, home = _web_client(tmp_path)
    for path in ("/health", "/health.json", "/repairs", "/memory"):
        assert c.get(path).status_code == 200
        assert c.get(path, headers={"host": "attacker.example:8765"}).status_code == 403
        assert c.get(path, headers={"host": "127.0.0.1.attacker.example"}).status_code == 403
        assert c.get(path, headers={"host": "localhost:8765"}).status_code == 200
    assert c.post("/memory/1/forget", data={"owner": "a", "scope": "b"},
                  headers={"host": "attacker.example:8765", "Origin": "http://attacker.example:8765"}).status_code == 403


async def test_l2_deliver_due_claims_exactly_one_row_per_send(tmp_path):
    env = await Env(tmp_path, accounts=("personal",)).start()
    limits: list[int] = []
    real = env.ledger.claim_outbox

    def spy(**kw):
        limits.append(kw.get("limit"))
        return real(**kw)

    env.ledger.claim_outbox = spy  # type: ignore[method-assign]
    for i in range(3):
        env.prov.add_mail("personal", f"m{i}", subject=f"S{i}")
    await env.tick()
    assert limits and set(limits) == {1}


async def test_reannouncement_never_copies_content_to_a_different_destination(tmp_path):
    from anna.runtime.schedule_types import ScheduleDestination
    env = await Env(tmp_path, accounts=("personal",)).start()
    env.cfg.heartbeat.allowed_destinations = ["slack:CDEST", "slack:COTHER"]
    await env.tick()
    env.prov.add_mail("personal", "m1", subject="Confidential merger terms")
    env.adapter.send_should_raise = RuntimeError("socket closed mid-write")
    await env.tick()
    env.adapter.send_should_raise = None
    env.store.get("hb").destination = ScheduleDestination(transport="slack", channel="COTHER")
    await env.tick()
    sent = [m for m in env.adapter.sent if m.conversation_key.endswith("COTHER")]
    assert sent and all("Confidential" not in m.text for m in sent)
    assert any("previous destination" in m.text for m in sent)
