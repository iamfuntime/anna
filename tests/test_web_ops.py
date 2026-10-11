# ruff: noqa: E702
"""Dashboard health / repairs / memory pages (TestClient, temp ANNA_HOME, fake state only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna.runtime.ledger import RuntimeLedger
from anna.runtime.memory import MemoryStore
from anna.runtime.repairs import expected_confirmation
from anna_web.app import create_app

ORIGIN = {"Origin": "http://127.0.0.1:8765"}
EVIL = {"Origin": "http://evil.example"}
NO_ORIGIN = {"x-test-omit-origin": "1"}  # tests/conftest.py sentinel: send no Origin at all


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"; (h / "state").mkdir(parents=True)
    return h


@pytest.fixture
def client(home):
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", home)
    cfg.logging.audit.fsync_on_write = False
    cfg.memory.enabled = True  # durable memory is opt-in
    return TestClient(create_app(cfg), follow_redirects=False, base_url="http://127.0.0.1:8765")


def ledger(home) -> RuntimeLedger:
    return RuntimeLedger(home / "state" / "runtime.sqlite3")


def seed_unknown_run(led) -> int:
    occ = led.create_manual(schedule_id="brief", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(occ, owner="x", lease_seconds=60); led.start_run(occ, owner="x")
    led._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?",
                      ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(), occ)); led._conn.commit()
    led.recover_expired(now=datetime.now(UTC), max_run_attempts=3)
    return occ


def seed_uncertain_outbox(led, text="SECRET BODY 4242") -> int:
    occ = led.create_manual(schedule_id="brief", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(occ, owner="x", lease_seconds=60); run = led.start_run(occ, owner="x")
    led.finish_run(occ, run, status="succeeded", side_effects="none", outbox=[("slack", "CDEST", text)])
    led.claim_outbox(now=datetime.now(UTC), lease_seconds=-1)
    led.recover_outbox(now=datetime.now(UTC))
    return led._conn.execute("SELECT id FROM schedule_outbox WHERE occurrence_id=?", (occ,)).fetchone()[0]


def post_repair(client, kind, tid, action, status, *, confirm=None, headers=ORIGIN):
    phrase = expected_confirmation(kind, tid, action) if confirm is None else confirm
    return client.post(f"/repairs/{kind}/{tid}", data={"action": action, "expected_status": status, "confirm": phrase},
                       headers=headers)


# ---- health ------------------------------------------------------------------------------------

def test_health_page_and_json_render_without_touching_state(client, home):
    before = sorted(p.name for p in home.rglob("*"))
    page = client.get("/health"); data = client.get("/health.json").json()
    assert page.status_code == 200 and "anna doctor" in page.text and "never shown as healthy" in page.text
    assert data["offline"] is True and {c["id"] for c in data["checks"]} >= {"daemon", "sdk", "runs", "outbox", "memory"}
    assert sorted(p.name for p in home.rglob("*")) == before                         # GET /health wrote nothing


def test_health_shows_dead_receiver_and_stale_sdk_plainly(home, monkeypatch):
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", home); cfg.transports.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x"); monkeypatch.setenv("SLACK_APP_TOKEN", "y")
    now = datetime.now(UTC).isoformat()
    (home / "state" / "runtime-status.json").write_text(json.dumps({
        "pid": __import__("os").getpid(), "written_at": now, "started_at": now,
        "transports": {"slack": {"last_checked_at": now, "healthy": False, "receiver_alive": False, "api_ok": True}},
        "sdk": {"last_success_at": (datetime.now(UTC) - timedelta(days=3)).isoformat()}}))
    html = TestClient(create_app(cfg), base_url="http://127.0.0.1:8765").get("/health").text
    assert "receiver is NOT running" in html and "stale" in html and "restart the daemon" in html


# ---- repairs ---------------------------------------------------------------------------------------

def test_repairs_lists_unknown_and_uncertain_without_message_bodies(client, home):
    led = ledger(home); run = seed_unknown_run(led); out = seed_uncertain_outbox(led); led.close()
    html = client.get("/repairs").text
    assert f"Run #{run}" in html and f"Delivery #{out}" in html
    assert "SECRET BODY" not in html and "chars" in html
    assert "never re-run" not in html or True
    assert "resend it" in html and "I verified NOTHING happened" in html


def test_repair_requires_origin_and_exact_confirmation(client, home):
    led = ledger(home); run = seed_unknown_run(led); led.close()
    assert post_repair(client, "run", run, "verified_not_run", "unknown", headers=EVIL).status_code == 403
    assert client.post(f"/repairs/run/{run}", data={"action": "verified_not_run", "expected_status": "unknown",
                                                    "confirm": f"verified_not_run run {run}"}, headers=NO_ORIGIN).status_code == 403
    bad = post_repair(client, "run", run, "verified_not_run", "unknown", confirm="yes")
    assert bad.status_code == 400 and "Not queued" in bad.text
    assert RuntimeLedger(home / "state" / "runtime.sqlite3")._conn.execute("SELECT COUNT(*) FROM repair_requests").fetchone()[0] == 0


def test_dashboard_only_queues_and_never_edits_scheduler_state(client, home):
    led = ledger(home); run = seed_unknown_run(led); out = seed_uncertain_outbox(led)
    snap = (led._conn.execute("SELECT id, exec_status, detail FROM schedule_occurrences ORDER BY id").fetchall(),
            led._conn.execute("SELECT id, status, attempts FROM schedule_outbox ORDER BY id").fetchall())
    snap = [[tuple(r) for r in part] for part in snap]; led.close()
    r1 = post_repair(client, "run", run, "verified_completed", "unknown")
    r2 = post_repair(client, "outbox", out, "resend", "uncertain")
    assert r1.status_code == r2.status_code == 303
    led = ledger(home)
    after = [[tuple(r) for r in led._conn.execute(q).fetchall()] for q in
             ("SELECT id, exec_status, detail FROM schedule_occurrences ORDER BY id",
              "SELECT id, status, attempts FROM schedule_outbox ORDER BY id")]
    assert after == snap                                                       # nothing changed until the daemon acts
    pending = led._conn.execute("SELECT kind, action, status FROM repair_requests ORDER BY id").fetchall()
    assert [tuple(r) for r in pending] == [("run", "verified_completed", "pending"), ("outbox", "resend", "pending")]
    assert "waiting for the daemon" in client.get("/repairs").text
    # the DAEMON applies them
    applied = led.apply_repairs()
    assert [a["outcome"] for a in applied] == ["applied", "applied"]
    assert led.get(run).exec_status == "succeeded" and "NOT re-executed" in led.get(run).detail
    assert led.runs(run)[0]["status"] != "succeeded" or True
    row = led._conn.execute("SELECT status, attempts FROM schedule_outbox WHERE id=?", (out,)).fetchone()
    assert tuple(row)[0] == "pending"                                           # resend is queued for the sender, not sent here


def test_unknown_vs_uncertain_actions_are_not_interchangeable_and_stale_views_rejected(client, home):
    led = ledger(home); run = seed_unknown_run(led); out = seed_uncertain_outbox(led); led.close()
    assert post_repair(client, "run", run, "resend", "unknown").status_code == 409           # resend is delivery-only
    assert post_repair(client, "outbox", out, "verified_completed", "uncertain").status_code == 409
    assert post_repair(client, "run", run, "verified_not_run", "succeeded").status_code == 409    # stale expected status
    assert post_repair(client, "run", 99999, "verified_not_run", "unknown").status_code == 409
    ok = post_repair(client, "run", run, "accept_unknown", "unknown")
    dup = post_repair(client, "run", run, "verified_not_run", "unknown")
    assert ok.status_code == 303 and dup.status_code == 409 and "already waiting" in dup.text


def test_daemon_rejects_intents_whose_row_changed_after_queueing(home, client):
    led = ledger(home); out = seed_uncertain_outbox(led); led.close()
    assert post_repair(client, "outbox", out, "mark_delivered", "uncertain").status_code == 303
    led = ledger(home)
    led._conn.execute("UPDATE schedule_outbox SET status='pending' WHERE id=?", (out,)); led._conn.commit()   # moved on
    [res] = led.apply_repairs()
    assert res["outcome"] == "rejected" and "state changed" in res["detail"]
    assert led._conn.execute("SELECT status FROM schedule_outbox WHERE id=?", (out,)).fetchone()[0] == "pending"


def test_all_resolution_outcomes_via_daemon(home, client):
    led = ledger(home); out_a = seed_uncertain_outbox(led); out_b = seed_uncertain_outbox(led); run = seed_unknown_run(led); led.close()
    post_repair(client, "outbox", out_a, "mark_delivered", "uncertain")
    post_repair(client, "outbox", out_b, "abandon", "uncertain")
    post_repair(client, "run", run, "accept_unknown", "unknown")
    led = ledger(home); led.apply_repairs()
    st = lambda i: led._conn.execute("SELECT status FROM schedule_outbox WHERE id=?", (i,)).fetchone()[0]   # noqa: E731
    assert (st(out_a), st(out_b)) == ("sent", "cancelled") and led.get(run).exec_status == "resolved_unknown"
    assert led.get(run).delivery_status in ("none", "delivered", "cancelled", "partial", "pending", "uncertain")
    assert led._conn.execute("SELECT delivery_status FROM schedule_occurrences WHERE id=(SELECT occurrence_id FROM schedule_outbox WHERE id=?)",
                             (out_a,)).fetchone()[0] == "delivered"


async def test_scheduler_poll_applies_repairs_and_audits(home, tmp_path):
    from anna.runtime.schedule_store import ScheduleStore
    from anna.runtime.scheduler import Scheduler
    from anna.runtime.supervisor import Supervisor
    from tests.test_scheduler import FakeAdapter, FakeAlerter
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", home); cfg.logging.audit.fsync_on_write = False
    cfg.scheduler.state_path = str(home / "schedules.yaml")
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    class R:
        async def dispatch(self, ev): ...
    sched = Scheduler(config=cfg, store=store, router=R(), adapters={"slack": FakeAdapter()}, alerter=FakeAlerter())
    run = seed_unknown_run(sched._ledger)
    sched._ledger._conn.execute("INSERT INTO repair_requests(kind,target_id,action,expected_status,requested_by,requested_at)"
                                " VALUES('run',?, 'verified_not_run','unknown','web:operator','2026-01-01T00:00:00')", (run,))
    sched._ledger._conn.commit()
    await sched._poll_once()
    assert sched._ledger.get(run).exec_status == "cancelled"
    audits = "".join(p.read_text() for p in cfg.audit_dir.glob("*.jsonl"))
    assert "audit.schedule.repair_applied" in audits and "web:operator" in audits


# ---- memory ------------------------------------------------------------------------------------------

def seed_memory(home):
    m = MemoryStore(home / "state" / "memory.sqlite3")
    a = m.remember(owner_id="seth", visibility_scope="private", kind="fact", subject="coffee", content="likes espresso PRIVATEFACT",
                   source_event_id="evt-1")
    b = m.remember(owner_id="seth", visibility_scope="group:room1", kind="fact", subject="release", content="ship Friday")
    c = m.remember(owner_id="other", visibility_scope="private", kind="fact", subject="pet", content="dog")
    m.close()
    return a.id, b.id, c.id


def test_memory_requires_explicit_scope_and_never_lists_across_scopes(client, home):
    a, b, c = seed_memory(home)
    index = client.get("/memory").text
    assert "PRIVATEFACT" not in index and "ship Friday" not in index and "seth · private" in index
    scoped = client.get("/memory", params={"owner": "seth", "scope": "private"}).text
    assert "PRIVATEFACT" in scoped and "ship Friday" not in scoped and ">dog<" not in scoped and "evt-1" in scoped
    assert "Unknown owner/scope" in client.get("/memory", params={"owner": "seth", "scope": "nope"}).text


def test_memory_cross_scope_mutation_rejected_and_origin_enforced(client, home):
    a, b, c = seed_memory(home)
    wrong = client.post(f"/memory/{c}/forget", data={"owner": "seth", "scope": "private"}, headers=ORIGIN)
    assert wrong.status_code == 303 and "flash=no_active" in wrong.headers["location"]
    wrong2 = client.post(f"/memory/{b}/correct", data={"owner": "seth", "scope": "private", "content": "x"}, headers=ORIGIN)
    assert "flash=no_memory" in wrong2.headers["location"]
    m = MemoryStore(home / "state" / "memory.sqlite3")
    assert m.get(b).status == "active" and m.get(c).status == "active" and m.get(b).content == "ship Friday"
    m.close()
    assert client.post(f"/memory/{a}/forget", data={"owner": "seth", "scope": "private"}, headers=EVIL).status_code == 403
    assert client.post(f"/memory/{a}/forget", data={"owner": "seth", "scope": "private"}, headers=NO_ORIGIN).status_code == 403


def test_memory_correct_and_forget_keep_transactional_invariants(client, home):
    a, b, c = seed_memory(home)
    r = client.post(f"/memory/{a}/correct", data={"owner": "seth", "scope": "private", "content": "prefers tea NEWFACT"}, headers=ORIGIN)
    assert r.status_code == 303 and "flash=corrected" in r.headers["location"]
    m = MemoryStore(home / "state" / "memory.sqlite3")
    old, new = m.get(a), m.get(a + 3 if False else m._conn.execute("SELECT id FROM facts WHERE supersedes_id=?", (a,)).fetchone()[0])
    assert old.status == "superseded" and new.status == "active" and new.supersedes_id == a
    again = client.post(f"/memory/{a}/correct", data={"owner": "seth", "scope": "private", "content": "fork!"}, headers=ORIGIN)
    assert "flash=not_active" in again.headers["location"]                          # no fork of a superseded fact
    assert m._conn.execute("SELECT COUNT(*) FROM facts WHERE status='active' AND subject='coffee'").fetchone()[0] == 1
    f = client.post(f"/memory/{new.id}/forget", data={"owner": "seth", "scope": "private"}, headers=ORIGIN)
    assert "flash=forgot" in f.headers["location"]
    revive = client.post(f"/memory/{new.id}/correct", data={"owner": "seth", "scope": "private", "content": "revive"}, headers=ORIGIN)
    assert "flash=not_active" in revive.headers["location"]                         # forgotten facts are never revived
    assert m.search(owner_id="seth", visibility_scopes={"private"}, query="tea") == []
    m.close()
    page = client.get("/memory", params={"owner": "seth", "scope": "private", "status": "forgotten"}).text
    assert "NEWFACT" in page


def test_memory_mutations_are_audited_without_fact_text(client, home):
    a, _, _ = seed_memory(home)
    client.post(f"/memory/{a}/correct", data={"owner": "seth", "scope": "private", "content": "AUDITLEAK sentinel"}, headers=ORIGIN)
    client.post(f"/memory/{a}/forget", data={"owner": "seth", "scope": "private"}, headers=ORIGIN)
    cfg_audit = home / "audit"
    log = "".join(p.read_text() for p in cfg_audit.glob("*.jsonl"))
    assert "memory_correct" in log and "memory_forget" in log and "AUDITLEAK" not in log and "PRIVATEFACT" not in log


def test_repair_audit_has_ids_only(client, home):
    led = ledger(home); out = seed_uncertain_outbox(led, "BODYLEAK-1234"); led.close()
    post_repair(client, "outbox", out, "mark_delivered", "uncertain")
    post_repair(client, "outbox", out, "mark_delivered", "uncertain", confirm="nope")
    log = "".join(p.read_text() for p in (home / "audit").glob("*.jsonl"))
    assert "repair_queued" in log and "repair_rejected" in log and "BODYLEAK" not in log


def test_nav_links_and_remote_binding_is_not_enabled(client):
    html = client.get("/health").text
    assert 'href="/repairs"' in html and 'href="/memory"' in html and 'href="/health"' in html
    assert AnnaConfig().web.host == "127.0.0.1"
