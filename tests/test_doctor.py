# ruff: noqa: E702, ASYNC251
"""anna doctor: offline, read-only, honest statuses (fake status files/ledger only)."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.doctor import collect, exit_code, render_plain
from anna.runtime.ledger import HeartbeatCommit, RuntimeLedger
from anna.runtime.memory import MemoryStore
from anna.runtime.status import StatusRecorder, read_snapshot, redact

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def cfg_for(home: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", home)
    return cfg


def ts(delta_s: float) -> str:
    return (NOW - timedelta(seconds=delta_s)).isoformat(timespec="seconds")


def write_snapshot(cfg, **over):
    data = {"schema": 1, "pid": os.getpid(), "started_at": ts(3600), "written_at": ts(30), "transports": {},
            "sdk": {}, "memory_extraction": {}, "scheduler": {}}
    data.update(over)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "runtime-status.json").write_text(json.dumps(data))


def check(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)


def tree_digest(home: Path) -> dict[str, str]:
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest() + str(p.stat().st_mtime_ns)
            for p in sorted(home.rglob("*")) if p.is_file()}


# ---- missing state / no invented health ------------------------------------------------------------

def test_missing_state_is_unknown_not_healthy_and_creates_nothing(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    rep = collect(cfg_for(home), now=NOW)
    assert rep["overall"] == "warn" and exit_code(rep) == 1
    assert check(rep, "daemon")["status"] == "unknown" and check(rep, "sdk")["status"] == "unknown"
    assert check(rep, "runs")["status"] == "unknown"
    assert list(home.iterdir()) == []                         # a read-only tool must not even create state/


def test_enabled_in_config_alone_is_not_health(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path); cfg.transports.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x"); monkeypatch.setenv("SLACK_APP_TOKEN", "y")
    write_snapshot(cfg)                                          # daemon alive, but never verified slack
    rep = collect(cfg, now=NOW)
    c = check(rep, "transport.slack")
    assert c["status"] == "unknown" and "never verified" in c["summary"]


def test_missing_credentials_fail(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path); cfg.transports.telegram.enabled = True
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    c = check(collect(cfg, now=NOW), "transport.telegram")
    assert c["status"] == "fail" and "TELEGRAM_BOT_TOKEN" in c["summary"]


# ---- receiver vs API credentials ---------------------------------------------------------------------

def test_dead_receiver_with_valid_token_is_a_failure(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path); cfg.transports.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x"); monkeypatch.setenv("SLACK_APP_TOKEN", "y")
    write_snapshot(cfg, transports={"slack": {"last_checked_at": ts(20), "healthy": False, "receiver_alive": False,
                                              "api_ok": True}})
    c = check(collect(cfg, now=NOW), "transport.slack")
    assert c["status"] == "fail" and "receiver is NOT running" in c["summary"] and "API credentials work" in c["summary"]


def test_receiver_ok_but_api_failing_and_stale_and_healthy_cases(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path); cfg.transports.slack.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x"); monkeypatch.setenv("SLACK_APP_TOKEN", "y")
    base = {"healthy": True, "receiver_alive": True, "api_ok": True, "last_healthy_at": ts(10)}
    write_snapshot(cfg, transports={"slack": {**base, "last_checked_at": ts(10), "api_ok": False, "healthy": False}})
    assert check(collect(cfg, now=NOW), "transport.slack")["status"] == "fail"
    write_snapshot(cfg, transports={"slack": {**base, "last_checked_at": ts(5000)}})
    assert check(collect(cfg, now=NOW), "transport.slack")["status"] == "warn"
    write_snapshot(cfg, transports={"slack": {**base, "last_checked_at": ts(10)}})
    assert check(collect(cfg, now=NOW), "transport.slack")["status"] == "ok"


def test_daemon_states(tmp_path):
    cfg = cfg_for(tmp_path)
    write_snapshot(cfg, pid=999999999)
    assert check(collect(cfg, now=NOW), "daemon")["status"] == "fail"                 # crashed
    write_snapshot(cfg, stopped_at=ts(60))
    assert check(collect(cfg, now=NOW), "daemon")["status"] == "warn"                 # clean stop
    write_snapshot(cfg, written_at=ts(99999))
    assert "stale" in check(collect(cfg, now=NOW), "daemon")["summary"]               # hung
    write_snapshot(cfg)
    assert check(collect(cfg, now=NOW), "daemon")["status"] == "ok"


# ---- SDK: fresh / stale / untested / error -------------------------------------------------------------

@pytest.mark.parametrize("sdk,expected", [
    ({}, "unknown"),
    ({"last_success_at": ts(120)}, "ok"),
    ({"last_success_at": ts(48 * 3600)}, "warn"),
    ({"last_success_at": ts(500), "last_error_at": ts(100), "last_error": "boom"}, "fail"),
    ({"last_success_at": ts(100), "last_error_at": ts(500)}, "ok"),                    # recovered
    ({"last_error_at": ts(100), "last_error": "x"}, "fail"),
])
def test_sdk_statuses(tmp_path, sdk, expected):
    cfg = cfg_for(tmp_path); write_snapshot(cfg, sdk=sdk)
    c = check(collect(cfg, now=NOW), "sdk")
    assert c["status"] == expected
    if expected == "unknown":
        assert "untested" in c["summary"] and "does not verify credentials" in c["summary"]


# ---- google receipts / heartbeat / runs / outbox / memory -------------------------------------------------

def make_ledger(cfg) -> RuntimeLedger:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return RuntimeLedger(cfg.state_dir / "runtime.sqlite3")


def seed_heartbeat(led: RuntimeLedger, status: str, receipts: list[tuple[str, str, str, str]]):
    occ = led.create_manual(schedule_id="hb", job_kind="google_heartbeat", run_safety="read_only")
    led.claim(occ, owner="t", lease_seconds=60); run = led.start_run(occ, owner="t")
    commit = HeartbeatCommit(receipts=[{"account": a, "service": s, "status": st, "checked_at": at,
                                        "receipt_hash": "ab" * 32} for a, s, st, at in receipts])
    led.finish_run(occ, run, status=status, side_effects="none", heartbeat=commit)
    return occ


def test_per_account_service_last_verified_receipt_and_heartbeat_partial(tmp_path):
    cfg = cfg_for(tmp_path); cfg.google.enabled = True; cfg.heartbeat.enabled = True
    cfg.google.accounts = [GoogleAccountConfig(slug=s, email=f"{s}@x.test", auth_type="oauth", credentials_file="c.json")
                           for s in ("personal", "work")]
    led = make_ledger(cfg)
    recent = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    seed_heartbeat(led, "partial", [("personal", "gmail", "ok", recent), ("personal", "calendar", "ok", recent),
                                    ("work", "gmail", "auth_required", recent)])
    led.close()
    rep = collect(cfg)                                          # real clock: receipts are fresh
    assert check(rep, "google.personal.gmail")["status"] == "ok"
    work_gmail = check(rep, "google.work.gmail")
    assert work_gmail["status"] == "fail" and "re-authorization" in work_gmail["summary"]
    assert "google_auth add work" in work_gmail["action"]
    assert check(rep, "google.work.calendar")["status"] == "unknown"                     # never verified
    hb = check(rep, "heartbeat")
    assert hb["status"] == "warn" and "PARTIAL" in hb["summary"]


def test_heartbeat_failed_and_disabled_and_never_run(tmp_path):
    cfg = cfg_for(tmp_path)
    assert check(collect(cfg, now=NOW), "heartbeat")["status"] == "disabled"
    cfg.heartbeat.enabled = True
    led = make_ledger(cfg); led.close()
    assert check(collect(cfg, now=NOW), "heartbeat")["status"] == "unknown"
    led = make_ledger(cfg); seed_heartbeat(led, "failed", []); led.close()
    assert check(collect(cfg, now=NOW), "heartbeat")["status"] == "fail"


def test_unknown_runs_and_uncertain_outbox_ages_are_reported(tmp_path):
    cfg = cfg_for(tmp_path); led = make_ledger(cfg)
    occ = led.create_manual(schedule_id="s", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(occ, owner="x", lease_seconds=60); led.start_run(occ, owner="x")
    led._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?", (ts(5), occ)); led._conn.commit()
    led.recover_expired(now=datetime.now(UTC), max_run_attempts=3)           # -> unknown
    o2 = led.create_manual(schedule_id="s2", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(o2, owner="x", lease_seconds=60); r2 = led.start_run(o2, owner="x")
    led.finish_run(o2, r2, status="succeeded", side_effects="none", outbox=[("slack", "C1", "hello world")])
    [item] = led.claim_outbox(now=datetime.now(UTC), lease_seconds=-1)
    led.recover_outbox(now=datetime.now(UTC)); led.close()
    rep = collect(cfg)
    runs, outbox = check(rep, "runs"), check(rep, "outbox")
    assert runs["status"] == "warn" and "UNKNOWN" in runs["summary"] and "never re-run" in runs["summary"]
    assert outbox["status"] == "warn" and "never resent automatically" in outbox["summary"]
    assert outbox["details"]["uncertain"]["count"] == 1
    assert "hello world" not in json.dumps(rep)                                      # bodies never appear


def test_memory_counts_queue_and_extraction_runner_honesty(tmp_path):
    cfg = cfg_for(tmp_path); cfg.memory.extraction.enabled = True
    cfg.state_dir.mkdir(parents=True)
    m = MemoryStore(cfg.state_dir / "memory.sqlite3")
    f = m.remember(owner_id="o", visibility_scope="private", kind="fact", subject="a", content="one")
    m.remember(owner_id="o", visibility_scope="private", kind="fact", subject="b", content="two"); m.forget(f.id)
    m.enqueue_extraction(owner_id="o", visibility_scope="private", conversation_key="user:o", source_event_id="e1",
                         source_text="I like tea a lot")
    m.close()
    write_snapshot(cfg, memory_extraction={"running": False, "blocked_reason": "metered credentials present"})
    c = check(collect(cfg, now=NOW), "memory")
    assert c["status"] == "warn" and "NOT running" in c["summary"] and "metered credentials present" in c["summary"]
    assert c["details"]["facts"] == {"active": 1, "forgotten": 1}
    assert c["details"]["extraction_jobs"]["pending"]["count"] == 1


# ---- safety: offline, read-only, no secrets -------------------------------------------------------------------

def test_doctor_has_no_side_effects_no_network_no_model_and_leaks_no_secrets(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path); cfg.transports.slack.enabled = True; cfg.heartbeat.enabled = True
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-SUPERSECRET-123456"); monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-SECRET2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAKME-000000")
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-SUPERSECRET-123456\n")
    led = make_ledger(cfg); seed_heartbeat(led, "succeeded", []); led.close()
    MemoryStore(cfg.state_dir / "memory.sqlite3").close()
    write_snapshot(cfg, sdk={"last_error_at": ts(5), "last_error": redact("auth failed token=abc123 xoxb-SUPERSECRET-123456")})
    before = tree_digest(tmp_path)

    def no_network(*a, **k): raise AssertionError("doctor attempted network I/O")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", no_network, raising=False)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", no_network, raising=False)

    rep = collect(cfg, now=NOW)
    out = json.dumps(rep) + render_plain(rep)
    assert tree_digest(tmp_path) == before                                      # nothing created, modified or re-timed
    for secret in ("SUPERSECRET", "xapp-SECRET2", "LEAKME", "abc123"):
        assert secret not in out
    assert check(rep, "transport.slack")["details"]["credentials_present"] == {"SLACK_BOT_TOKEN": True, "SLACK_APP_TOKEN": True}


def test_doctor_does_not_modify_an_old_schema_ledger(tmp_path):
    cfg = cfg_for(tmp_path); led = make_ledger(cfg)
    led._conn.execute("DELETE FROM ledger_migrations WHERE version=3"); led._conn.execute("DROP TABLE repair_requests")
    led._conn.commit(); led.close()
    before = tree_digest(tmp_path)
    collect(cfg, now=NOW)
    assert tree_digest(tmp_path) == before and not list(cfg.state_dir.glob("*.bak*"))     # no migration, no backup


# ---- status recorder ---------------------------------------------------------------------------------------------

def test_status_recorder_atomic_private_and_redacts(tmp_path):
    rec = StatusRecorder(tmp_path / "state" / "runtime-status.json")
    rec.transport("slack", healthy=False, receiver_alive=False, api_ok=True, error="token=abc SECRETISH xoxb-12345-abc")
    rec.sdk_error("Bearer abcdef.ghijkl")
    path = tmp_path / "state" / "runtime-status.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    snap = read_snapshot(path)
    assert snap["transports"]["slack"]["receiver_alive"] is False and "abc" not in json.dumps(snap).replace("abcdef", "")
    assert "xoxb" not in json.dumps(snap) and "abcdef.ghijkl" not in json.dumps(snap)
    assert read_snapshot(tmp_path / "nope.json") is None


async def test_watchdog_records_receiver_separately_from_api(tmp_path, monkeypatch):
    from anna.runtime import status as status_mod
    from anna.runtime.watchdog import Watchdog
    from anna.transports.base import ChannelAdapter
    rec = StatusRecorder(tmp_path / "s.json"); status_mod.set_recorder(rec)

    class A(ChannelAdapter):
        name = "slack"
        async def start(self): ...
        async def stop(self): ...
        async def send(self, m): ...
        def subscribe(self, h): ...
        async def health_check(self): return False
        @classmethod
        def conversation_key_for(cls, event): return "x"
        async def health_detail(self): return {"receiver_alive": False, "api_ok": True, "error": None}

    try:
        wd = Watchdog(config=cfg_for(tmp_path), adapters={"slack": A()}, router=object())
        await wd._record_transport("slack", wd._adapters["slack"], False)
    finally:
        status_mod.set_recorder(None)
    e = rec.snapshot()["transports"]["slack"]
    assert e["receiver_alive"] is False and e["api_ok"] is True and e["healthy"] is False


# ---- CLI --------------------------------------------------------------------------------------------------------------

def test_cli_plain_and_json_and_exit_codes(tmp_path, monkeypatch, capsys):
    from anna.cli import doctor as cli
    monkeypatch.setenv("ANNA_HOME", str(tmp_path / "h")); (tmp_path / "h").mkdir()
    monkeypatch.setattr(sys, "argv", ["anna doctor"])
    code = cli.main(); out = capsys.readouterr().out
    assert code == 1 and "[ ?? ]" in out and "offline, read-only" in out and "not verified" in out
    monkeypatch.setattr(sys, "argv", ["anna doctor", "--json"])
    code = cli.main(); data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["offline"] is True and {c["id"] for c in data["checks"]} >= {"daemon", "sdk", "runs", "outbox", "memory"}
    assert all(set(c) >= {"id", "title", "status", "summary"} for c in data["checks"])


def test_doctor_is_registered_as_a_subcommand():
    from anna.__main__ import _CLI_SUBCOMMANDS
    assert _CLI_SUBCOMMANDS["doctor"] == "anna.cli.doctor"


def test_worker_records_sdk_success_and_failure(tmp_path, monkeypatch):
    import asyncio

    import claude_agent_sdk as sdk

    from anna.runtime import status as status_mod
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
    rec = StatusRecorder(tmp_path / "s.json"); status_mod.set_recorder(rec)

    async def go():
        w = _make_worker(tmp_path, [])
        w._client = _ImmediateClient([_FakeTextBlock(text="ok")])
        await w._handle(_make_event())
        assert "last_success_at" in rec.snapshot()["sdk"] and "last_error_at" not in rec.snapshot()["sdk"]

        class Boom(_ImmediateClient):
            async def query(self, prompt): raise RuntimeError("down")

        w._client = Boom([])
        await w._handle(_make_event())

    try:
        asyncio.run(go())
    finally:
        status_mod.set_recorder(None)
    assert "last_error_at" in rec.snapshot()["sdk"]
