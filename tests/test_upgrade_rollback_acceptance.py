# ruff: noqa: E702
"""Two-build acceptance: main 59b946a (mock) -> this branch -> main -> this branch -> restore fields.

Everything is mocked and local: a fake clock, the vendored main-build mock (``tests/fixtures/main_59b946a``),
the real ledger scheduler of this branch with a fake router/adapter, a temp ANNA_HOME. No provider, bot,
systemd or production path is touched. The operator-side commands (backups, field restore) are the ones
``docs/trial-readiness.md`` prints.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import anna.runtime.ledger as ledger_mod
import anna.runtime.scheduler as scheduler_mod
from anna.runtime.ledger import iso
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.scheduler import Scheduler
from anna.runtime.supervisor import Supervisor
from tests.fixtures.main_59b946a.main_build import MainBuild
from tests.fixtures.main_59b946a.main_build import Schedule as MainSchedule
from tests.test_scheduler_gate_command import _events, _read_audit_records
from tests.test_scheduler_ledger import FakeAdapter, FakeAlerter, Router, make_cfg, settle

FIXTURES = Path(__file__).parent / "fixtures" / "main_59b946a"
RESTORE = Path(__file__).parents[1] / "scripts" / "restore_schedule_fields.py"
LEDGER_ERA_FIELDS = {"job_kind", "run_safety", "overlap_policy", "misfire_policy", "misfire_grace_seconds"}
T0 = datetime(2026, 10, 10, 12, 0, 10, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def advance(self, **kw: float) -> datetime:
        self.t += timedelta(**kw)
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock(T0)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return c.t if tz is None else c.t.astimezone(tz)

    monkeypatch.setattr(ledger_mod, "now_utc", lambda: c.t)
    monkeypatch.setattr(scheduler_mod, "now_utc", lambda: c.t)
    monkeypatch.setattr(scheduler_mod, "datetime", FakeDatetime)
    return c


def _golden(path: Path, now: datetime) -> None:
    text = (FIXTURES / "schedules.golden.yaml").read_text()
    for minutes in (1, 4320):
        text = text.replace(f"@AGO:{minutes}@", iso(now - timedelta(minutes=minutes)))
    text = text.replace("[/bin/true]", f"[{sys.executable}, -c, 'raise SystemExit(10)']")  # gate says "wake"
    path.write_text(text)


def _branch(cfg, router, adapter):
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    return store, Scheduler(config=cfg, store=store, router=router, adapters={"slack": adapter}, alerter=FakeAlerter())


def _executed(sched) -> set[tuple[str, str]]:
    """(schedule, tick minute) of every occurrence this build started."""
    rows = sched._ledger._conn.execute(
        "SELECT schedule_id, scheduled_for FROM schedule_occurrences WHERE started_at IS NOT NULL").fetchall()
    return {(r[0], r[1][:16]) for r in rows}


def _main_ran(main: MainBuild) -> set[tuple[str, str]]:
    return {(sid, iso(t)[:16]) for sid, t in main.runs}


def _ledger_copy(src: Path, dst: Path) -> None:
    """What the docs say: a consistent online copy of the ledger (``sqlite3 ... ".backup"`` equivalent)."""
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close(); b.close()


async def test_upgrade_rollback_retrial_with_a_real_older_build(tmp_path, clock):
    home = tmp_path / "anna"; home.mkdir()
    cfg = make_cfg(home)
    yaml_path = Path(cfg.scheduler.state_path)
    _golden(yaml_path, clock.t)

    # ---- A. the OLD build (main) is running: it fires everything due and rewrites the file ------------------
    main_a = MainBuild(yaml_path)
    assert main_a.poll_once(clock.advance(seconds=55)) == ["heartbeat", "report", "flaky"]   # 12:01:05, paused is off
    assert {m for _, m in main_a.runs} == {clock.t.replace(second=0, microsecond=0)}

    # ---- B. operator backup before the upgrade (docs: cp -p schedules.yaml schedules.yaml.pre-upgrade-<ts>) ---
    pre_upgrade = yaml_path.with_name("schedules.yaml.pre-upgrade-test")
    shutil.copy2(yaml_path, pre_upgrade)

    # ---- C. UPGRADE: first start of this build on that file, empty ledger ----------------------------------
    router, adapter = Router(), FakeAdapter()
    store, sched = _branch(cfg, router, adapter)
    await store.load()
    clock.advance(seconds=5)
    await sched._poll_once(); await settle(sched)
    assert router.events == [] and adapter.sent == []                       # nothing main already ran is replayed
    assert list(home.glob("schedules.yaml.bak-pre-ledger-*"))               # automatic one-time copy
    await store.update("report", run_safety="read_only", overlap_policy="queue", misfire_policy="skip_late",
                       misfire_grace_seconds=90)                            # operator hardening, ledger-era fields
    clock.set(T0.replace(minute=2, second=10))
    await sched._poll_once(); await settle(sched)
    assert sorted(e.raw["schedule_id"] for e in router.events) == ["flaky", "heartbeat", "report"]
    assert next(e for e in router.events if e.raw["schedule_id"] == "heartbeat").model == "sonnet"
    assert len(adapter.sent) == 3
    clock.set(T0.replace(minute=3, second=10))
    await sched._poll_once(); await settle(sched)
    assert len(router.events) == 6

    # ---- the daemon dies with work in flight (all of it from THIS build) -----------------------------------
    led = sched._ledger
    with led._conn:
        led._insert_occurrence(schedule_id="report", scheduled_for=T0.replace(minute=4, second=0), trigger="cron",
                               job_kind="agent_prompt", run_safety="read_only", status="pending", detail=None)
        dead = led._insert_occurrence(schedule_id="flaky", scheduled_for=T0.replace(minute=4, second=0), trigger="cron",
                                      job_kind="agent_prompt", run_safety="side_effecting", status="pending", detail=None)
    led.claim(dead, owner="999999-x-dead", lease_seconds=60)
    led.start_run(dead, owner="999999-x-dead")
    queued = led.create_manual(schedule_id="heartbeat", job_kind="agent_prompt", run_safety="side_effecting")
    led.claim(queued, owner=sched._owner, lease_seconds=60)
    led.finish_run(queued, led.start_run(queued, owner=sched._owner), status="succeeded", side_effects="none",
                   outbox=[("slack", "UGOLDEN01", "queued alert text")])
    sent_before_rollback = len(adapter.sent)
    clock.set(T0.replace(minute=4, second=20))
    own_cursor = led.cursor("report")
    del sched                                                              # <- process gone; nothing else runs

    # ---- D. ROLLBACK to main. Operator backups first (docs), then main runs on the SAME file ---------------
    pre_rollback = yaml_path.with_name("schedules.yaml.pre-rollback-test")
    shutil.copy2(yaml_path, pre_rollback)
    _ledger_copy(home / "state" / "runtime.sqlite3", home / "state" / "runtime.sqlite3.pre-rollback-test")
    assert LEDGER_ERA_FIELDS & set(next(s for s in yaml.safe_load(pre_rollback.read_text())["schedules"]
                                        if s["id"] == "report"))          # the branch wrote them

    main = MainBuild(yaml_path)
    assert main.poll_once(T0.replace(minute=5, second=10)) == ["heartbeat", "report", "flaky"]
    assert main.poll_once(T0.replace(minute=6, second=10)) == ["heartbeat", "report", "flaky"]
    after_main = {s["id"]: s for s in yaml.safe_load(yaml_path.read_text())["schedules"]}
    assert not (LEDGER_ERA_FIELDS & set(after_main["report"]))             # M5: main erased the ledger-era fields
    assert after_main["heartbeat"]["gate_command"] and after_main["heartbeat"]["model"] == "sonnet"  # main's own survive

    # ---- F. RESTORE the erased fields BEFORE the ledger build starts again (docs step), dry run then --apply ---
    dry = subprocess.run([sys.executable, str(RESTORE), str(yaml_path), str(pre_rollback)], capture_output=True, text=True)
    assert dry.returncode == 0 and "dry run" in dry.stdout and "report: would restore" in dry.stdout
    assert not (LEDGER_ERA_FIELDS & set(next(s for s in yaml.safe_load(yaml_path.read_text())["schedules"] if s["id"] == "report")))
    done = subprocess.run([sys.executable, str(RESTORE), str(yaml_path), str(pre_rollback), "--apply"],
                          capture_output=True, text=True)
    assert done.returncode == 0 and list(home.glob("schedules.yaml.bak-before-restore-*"))
    restored = next(s for s in yaml.safe_load(yaml_path.read_text())["schedules"] if s["id"] == "report")
    assert restored["run_safety"] == "read_only" and restored["overlap_policy"] == "queue" and restored["misfire_grace_seconds"] == 90
    assert restored["state"]["last_fired_at"].startswith("2026-10-10T12:06")   # main's state was never overwritten

    # ---- E. RETRIAL: this build again, over the stale ledger ----------------------------------------------
    clock.set(T0.replace(minute=6, second=40))
    router2, adapter2 = Router(), FakeAdapter()
    store2, sched2 = _branch(cfg, router2, adapter2)
    await store2.load()
    assert sched2._ledger.cursor("report") == own_cursor and own_cursor < T0.replace(minute=6)   # stale, as in the real case
    led2 = sched2._ledger
    led2._conn.execute("UPDATE schedule_occurrences SET lease_expires_at=? WHERE id=?", (iso(clock.t - timedelta(seconds=1)), dead))
    led2._conn.commit()
    await sched2._poll_once(); await settle(sched2)
    # nothing main already ran is replayed; the old queued work is suppressed; no remote side effect repeats
    assert router2.events == []
    assert adapter2.sent == [] and "queued alert text" not in [m.text for m in adapter2.sent]
    assert led2.get(dead).exec_status == "unknown"                          # dead owner, side-effecting: never re-run
    pend = [o for o in led2.recent("report", 50) if o.scheduled_for.startswith("2026-10-10T12:04")]
    assert [o.exec_status for o in pend] == ["skipped_misfire"]
    assert [r["status"] for r in led2.outbox(queued)] == ["cancelled"]
    audits = _read_audit_records(cfg.audit_dir)
    assert len(_events(audits, "audit.schedule.cursor_external_stamp")) == 3
    # the next tick runs exactly once per schedule; `report` has lost its ledger-era safety setting (M5)
    clock.set(T0.replace(minute=7, second=10))
    await sched2._poll_once(); await settle(sched2)
    assert sorted(e.raw["schedule_id"] for e in router2.events) == ["flaky", "heartbeat", "report"]
    [rep] = [o for o in led2.recent("report", 50) if o.scheduled_for.startswith("2026-10-10T12:07")]
    assert rep.run_safety == "read_only"                                    # the restored setting is in force again
    assert store2.get("report").misfire_grace_seconds == 90
    assert len(adapter2.sent) == 3
    assert sent_before_rollback == 6
    ran_by_branch = _executed(sched2)

    # ---- no tick was executed by both builds (the whole point) ---------------------------------------------
    both = ran_by_branch & (_main_ran(main_a) | _main_ran(main))
    assert not both, both
    assert len(_main_ran(main_a) | _main_ran(main)) == 9 and len(ran_by_branch) == 10  # 6 ticks + the dead run + 3 after retrial (manual row shares a minute)


def test_main_build_erases_exactly_the_documented_fields():
    """The loss in M5 is a property of main's model; it is asserted against the golden snapshot, not our code."""
    record = {"id": "x", "cron": "* * * * *", "prompt": "p", "destination": {"transport": "slack", "channel": "C1"},
              "created_at": "2026-10-01T00:00:00+00:00", "job_kind": "google_heartbeat", "run_safety": "read_only",
              "overlap_policy": "queue", "misfire_policy": "skip_late", "misfire_grace_seconds": 5,
              "gate_command": ["g"], "model": "sonnet", "from_newer_build": {"a": 1}}
    dumped = MainSchedule.model_validate(record).model_dump(mode="json")
    assert set(record) - set(dumped) == LEDGER_ERA_FIELDS | {"from_newer_build"}
    assert dumped["gate_command"] == ["g"] and dumped["model"] == "sonnet"


def test_restore_never_overwrites_or_touches_state(tmp_path):
    cur, old = tmp_path / "cur.yaml", tmp_path / "old.yaml"
    cur.write_text(yaml.safe_dump({"version": 1, "schedules": [
        {"id": "a", "cron": "x", "run_safety": "side_effecting", "state": {"last_fired_at": "NEW"}},
        {"id": "gone-from-old", "cron": "y"}]}))
    old.write_text(yaml.safe_dump({"version": 1, "schedules": [
        {"id": "a", "cron": "x", "run_safety": "read_only", "overlap_policy": "queue", "state": {"last_fired_at": "OLD"}},
        {"id": "deleted-since", "cron": "z", "run_safety": "read_only"}]}))
    assert subprocess.run([sys.executable, str(RESTORE), str(cur), str(old), "--apply"]).returncode == 0
    got = {s["id"]: s for s in yaml.safe_load(cur.read_text())["schedules"]}
    assert got["a"]["run_safety"] == "side_effecting"        # an existing value is never overwritten
    assert got["a"]["overlap_policy"] == "queue" and got["a"]["state"]["last_fired_at"] == "NEW"
    assert "deleted-since" not in got and got["gone-from-old"] == {"id": "gone-from-old", "cron": "y"}


def test_restore_overwrite_defaults_repairs_a_forgotten_restore(tmp_path):
    cur, old = tmp_path / "cur.yaml", tmp_path / "old.yaml"
    cur.write_text(yaml.safe_dump({"version": 1, "schedules": [
        {"id": "a", "cron": "x", "run_safety": "side_effecting", "job_kind": "agent_prompt", "overlap_policy": None}]}))
    old.write_text(yaml.safe_dump({"version": 1, "schedules": [
        {"id": "a", "cron": "x", "run_safety": "read_only", "job_kind": "agent_prompt", "overlap_policy": "queue"}]}))
    assert subprocess.run([sys.executable, str(RESTORE), str(cur), str(old), "--apply"]).returncode == 0
    assert yaml.safe_load(cur.read_text())["schedules"][0]["run_safety"] == "side_effecting"   # default: untouched
    assert subprocess.run([sys.executable, str(RESTORE), str(cur), str(old), "--apply", "--overwrite-defaults"]).returncode == 0
    got = yaml.safe_load(cur.read_text())["schedules"][0]
    assert got["run_safety"] == "read_only" and got["overlap_policy"] == "queue"
