"""Durable runtime ledger for scheduled work (SQLite, WAL).

Three record kinds, deliberately separate so *execution* and *delivery* never
share a status:

* ``schedule_occurrences`` - one row per ``(schedule_id, scheduled_for)``; the
  UNIQUE key is the idempotency guard against double dispatch and replayed cron
  ticks.  ``exec_status`` is the execution outcome, ``delivery_status`` is
  derived from the outbox.
* ``schedule_runs`` - one row per execution attempt, carrying the typed result.
* ``schedule_outbox`` - one row per destination chunk with attempts, next retry
  and the provider outcome.  Delivery retries touch only these rows; they never
  re-run execution.

Leases make claims recoverable.  Recovery rules (see ``recover_expired``):
a claim that never started running is safe to re-pend; an interrupted
``read_only`` run may be retried a bounded number of times; an interrupted
``side_effecting`` run is marked ``unknown`` and is NEVER re-run; a send that
was in flight is ``uncertain`` and is NEVER re-sent automatically.

Only the daemon mutates this file.  Everything is additive and stored apart
from ``schedules.yaml`` so a dashboard rewrite of the YAML cannot clobber it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from croniter import croniter

from anna.runtime.fsutil import private_file

ACTIVE_EXEC = ("claimed", "running")
TERMINAL_EXEC = ("succeeded", "failed", "timeout", "cancelled", "unknown", "blocked",
                 "skipped_overlap", "skipped_misfire", "partial", "auth_required", "resolved_unknown")
MAX_RECORDED_MISFIRES = 20


def process_start_token(pid: int | None = None) -> str:
    """Kernel start time of a process (Linux /proc), "" when unavailable. Defeats PID reuse."""
    try:
        raw = Path(f"/proc/{pid or os.getpid()}/stat").read_text()
        return raw.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def new_owner_id() -> str:
    """``<pid>-<starttime>-<random>``: lets another process prove this owner is dead."""
    return f"{os.getpid()}-{process_start_token() or 'x'}-{uuid.uuid4().hex[:8]}"


def owner_alive(owner: str | None) -> bool | None:
    """True = that process is provably alive, False = provably gone, None = cannot tell."""
    if not owner:
        return None
    parts = owner.split("-")
    if len(parts) != 3 or not parts[0].isdigit() or parts[1] in ("", "x"):
        return None
    token = process_start_token(int(parts[0]))
    if not Path(f"/proc/{parts[0]}").exists():
        return False if Path("/proc/self").exists() else None
    return token == parts[1]


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Occurrence:
    id: int
    schedule_id: str
    scheduled_for: str
    trigger: str
    job_kind: str
    run_safety: str
    exec_status: str
    delivery_status: str
    attempts: int
    lease_owner: str | None
    lease_expires_at: str | None
    detail: str | None


@dataclass(frozen=True)
class OutboxItem:
    id: int
    occurrence_id: int
    schedule_id: str
    transport: str
    channel: str
    chunk_index: int
    chunk_count: int
    text: str
    attempts: int


@dataclass
class HeartbeatCommit:
    """Heartbeat state applied in the SAME transaction as the run result and outbox rows.

    Cursors and notified-item keys therefore advance if and only if the
    notifications that justify them were durably queued: a crash can repeat a
    (read-only) check but never lose or duplicate an alert.
    """

    states: list[dict[str, Any]] = field(default_factory=list)
    announced_outbox_ids: list[int] = field(default_factory=list)
    notified: list[tuple[str, str, str, str]] = field(default_factory=list)  # account, service, key, change
    receipts: list[dict[str, Any]] = field(default_factory=list)
    retention_days: int = 30


@dataclass(frozen=True)
class Recovery:
    occurrence_id: int
    schedule_id: str
    action: str  # repended | unknown | skipped | failed
    detail: str


def split_chunks(text: str, limit: int) -> list[str]:
    """Split on line boundaries into pieces of at most ``limit`` characters."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()] or [text[:limit]]


class RuntimeLedger:
    def __init__(self, path: Path) -> None:
        private_file(path)
        self._path = path
        self._conn = sqlite3.connect(path, timeout=2.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS ledger_migrations "
                               "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {r[0] for r in self._conn.execute("SELECT version FROM ledger_migrations")}
        if 1 not in applied:
            self._migrate_v1()
        if 2 not in applied:
            if 1 in applied:
                self.backup(self._path.with_name(f"{self._path.name}.bak-v1-{iso(now_utc()).replace(':', '')}"))
            self._migrate_v2()
        if 3 not in applied:
            if 2 in applied:
                self.backup(self._path.with_name(f"{self._path.name}.bak-v2-{iso(now_utc()).replace(':', '')}"))
            self._migrate_v3()
        if 4 not in applied:
            if 3 in applied:
                self.backup(self._path.with_name(f"{self._path.name}.bak-v3-{iso(now_utc()).replace(':', '')}"))
            self._migrate_v4()

    def backup(self, dest: Path) -> Path:
        if dest.exists():
            raise FileExistsError(dest)
        target = sqlite3.connect(private_file(dest))
        try:
            self._conn.backup(target)
        finally:
            target.close()
        return dest

    def _migrate_v1(self) -> None:
        with self._immediate():  # DDL + ledger row commit together or not at all
            self._conn.execute("""CREATE TABLE schedule_cursors (
                schedule_id TEXT PRIMARY KEY, last_scheduled_for TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self._conn.execute("""CREATE TABLE schedule_occurrences (
                id INTEGER PRIMARY KEY, schedule_id TEXT NOT NULL, scheduled_for TEXT NOT NULL,
                trigger TEXT NOT NULL DEFAULT 'cron', job_kind TEXT NOT NULL, run_safety TEXT NOT NULL,
                exec_status TEXT NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'none',
                attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at TEXT, detail TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                UNIQUE(schedule_id, scheduled_for))""")
            self._conn.execute("CREATE INDEX occ_status ON schedule_occurrences(exec_status, lease_expires_at)")
            self._conn.execute("""CREATE TABLE schedule_runs (
                id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES schedule_occurrences(id),
                attempt INTEGER NOT NULL, status TEXT NOT NULL, side_effects TEXT NOT NULL DEFAULT 'unknown',
                owner TEXT, started_at TEXT NOT NULL, finished_at TEXT, result_json TEXT, error TEXT)""")
            self._conn.execute("""CREATE TABLE schedule_outbox (
                id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES schedule_occurrences(id),
                run_id INTEGER REFERENCES schedule_runs(id), schedule_id TEXT NOT NULL,
                transport TEXT NOT NULL, channel TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, lease_expires_at TEXT,
                provider_outcome TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(occurrence_id, chunk_index))""")
            self._conn.execute("CREATE INDEX outbox_due ON schedule_outbox(status, next_attempt_at)")
            self._conn.execute("INSERT INTO ledger_migrations(version, applied_at) VALUES(1, ?)", (iso(now_utc()),))

    def _migrate_v2(self) -> None:
        """Additive: heartbeat cursors/alert state, notified-item dedup, per-check receipts."""
        with self._immediate():
            # Recover a database left half-migrated by the earlier NON-atomic implementation
            # (RENAME committed, copy never ran): put the original table back first.
            names = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "schedule_outbox_v1" in names:
                if "schedule_outbox" in names:
                    self._conn.execute("DROP TABLE schedule_outbox")
                self._conn.execute("ALTER TABLE schedule_outbox_v1 RENAME TO schedule_outbox")
            # One occurrence can carry several independent messages (alerts, digests):
            # rebuild the outbox with a per-message index so chunk ordering and the
            # uniqueness guard apply within a message, not across the occurrence.
            self._conn.execute("ALTER TABLE schedule_outbox RENAME TO schedule_outbox_v1")
            self._conn.execute("DROP INDEX IF EXISTS outbox_due")
            self._conn.execute("""CREATE TABLE schedule_outbox (
                id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES schedule_occurrences(id),
                run_id INTEGER REFERENCES schedule_runs(id), schedule_id TEXT NOT NULL,
                transport TEXT NOT NULL, channel TEXT NOT NULL, msg_index INTEGER NOT NULL DEFAULT 0,
                chunk_index INTEGER NOT NULL, chunk_count INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL, lease_expires_at TEXT, provider_outcome TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(occurrence_id, msg_index, chunk_index))""")
            self._conn.execute("""INSERT INTO schedule_outbox(id,occurrence_id,run_id,schedule_id,transport,channel,
                chunk_index,chunk_count,text,status,attempts,next_attempt_at,lease_expires_at,provider_outcome,
                last_error,created_at,updated_at) SELECT id,occurrence_id,run_id,schedule_id,transport,channel,
                chunk_index,chunk_count,text,status,attempts,next_attempt_at,lease_expires_at,provider_outcome,
                last_error,created_at,updated_at FROM schedule_outbox_v1""")
            self._conn.execute("DROP TABLE schedule_outbox_v1")
            self._conn.execute("CREATE INDEX outbox_due ON schedule_outbox(status, next_attempt_at)")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS heartbeat_state (
                schedule_id TEXT NOT NULL, account TEXT NOT NULL, service TEXT NOT NULL,
                cursor TEXT, last_checked_at TEXT, last_ok_at TEXT, status TEXT NOT NULL DEFAULT 'ok',
                consecutive_failures INTEGER NOT NULL DEFAULT 0, alerts_sent INTEGER NOT NULL DEFAULT 0,
                last_alert_at TEXT, resync_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(schedule_id, account, service))""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS heartbeat_notified (
                schedule_id TEXT NOT NULL, account TEXT NOT NULL, service TEXT NOT NULL,
                item_key TEXT NOT NULL, change_id TEXT NOT NULL, notified_at TEXT NOT NULL,
                PRIMARY KEY(schedule_id, account, service, item_key, change_id))""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS heartbeat_receipts (
                id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES schedule_occurrences(id),
                run_id INTEGER NOT NULL REFERENCES schedule_runs(id), schedule_id TEXT NOT NULL,
                account TEXT NOT NULL, service TEXT NOT NULL, status TEXT NOT NULL, checked_at TEXT NOT NULL,
                receipt_hash TEXT NOT NULL, detail_json TEXT NOT NULL,
                UNIQUE(run_id, account, service))""")
            self._conn.execute("INSERT INTO ledger_migrations(version, applied_at) VALUES(2, ?)", (iso(now_utc()),))

    def _migrate_v3(self) -> None:
        """Additive: operator repair intents (inserted by the dashboard, applied only by the daemon)."""
        with self._immediate():
            self._conn.execute("""CREATE TABLE IF NOT EXISTS repair_requests (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target_id INTEGER NOT NULL, action TEXT NOT NULL,
                expected_status TEXT NOT NULL, note TEXT, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', result TEXT, applied_at TEXT)""")
            self._conn.execute("CREATE INDEX IF NOT EXISTS repair_pending ON repair_requests(status, id)")
            self._conn.execute("INSERT INTO ledger_migrations(version, applied_at) VALUES(3, ?)", (iso(now_utc()),))

    def _migrate_v4(self) -> None:
        """Additive: remember which stranded/uncertain heartbeat deliveries were already re-announced."""
        with self._immediate():
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(schedule_outbox)")}
            if "announced" not in cols:
                self._conn.execute("ALTER TABLE schedule_outbox ADD COLUMN announced INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("INSERT INTO ledger_migrations(version, applied_at) VALUES(4, ?)", (iso(now_utc()),))

    # --- operator repairs (daemon is the only applier)

    RUN_ACTIONS = {"unknown": ("verified_completed", "verified_not_run", "accept_unknown"),
                   "blocked": ("verified_completed", "verified_not_run", "accept_unknown")}
    OUTBOX_ACTIONS = {"uncertain": ("mark_delivered", "resend", "abandon"), "failed": ("retry", "abandon")}

    def apply_repairs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Apply queued operator intents. Never re-runs a run; never resends without an explicit resend/retry.

        Each request pins the status the operator saw (``expected_status``); if the row moved on
        meanwhile the request is rejected instead of acting on a stale view.
        """
        applied: list[dict[str, Any]] = []
        rows = self._conn.execute("SELECT * FROM repair_requests WHERE status='pending' ORDER BY id LIMIT ?",
                                  (limit,)).fetchall()
        for req in rows:
            outcome, detail = self._apply_one(req)
            with self._conn:
                self._conn.execute("UPDATE repair_requests SET status=?, result=?, applied_at=? WHERE id=?",
                                   (outcome, detail, iso(now_utc()), req["id"]))
            applied.append({"id": req["id"], "kind": req["kind"], "target_id": req["target_id"],
                            "action": req["action"], "outcome": outcome, "detail": detail,
                            "requested_by": req["requested_by"]})
        return applied

    def _apply_one(self, req: sqlite3.Row) -> tuple[str, str]:
        now = iso(now_utc())
        kind, tid, action = req["kind"], req["target_id"], req["action"]
        with self._immediate():
            if kind == "run":
                row = self._conn.execute("SELECT exec_status FROM schedule_occurrences WHERE id=?", (tid,)).fetchone()
                if row is None:
                    return "rejected", "no such run"
                if row[0] != req["expected_status"]:
                    return "rejected", f"state changed (now {row[0]})"
                if action not in self.RUN_ACTIONS.get(row[0], ()):
                    return "rejected", f"action {action!r} not allowed for a {row[0]} run"
                target = {"verified_completed": "succeeded", "verified_not_run": "cancelled",
                          "accept_unknown": "resolved_unknown"}[action]
                self._conn.execute(
                    "UPDATE schedule_occurrences SET exec_status=?, detail=?, finished_at=COALESCE(finished_at,?), "
                    "updated_at=? WHERE id=?", (target, f"operator-resolved: {action} (run was NOT re-executed)", now, now, tid))
                return "applied", f"run {tid} -> {target}; not re-executed"
            if kind == "outbox":
                row = self._conn.execute("SELECT status, occurrence_id FROM schedule_outbox WHERE id=?", (tid,)).fetchone()
                if row is None:
                    return "rejected", "no such delivery"
                if row[0] != req["expected_status"]:
                    return "rejected", f"state changed (now {row[0]})"
                if action not in self.OUTBOX_ACTIONS.get(row[0], ()):
                    return "rejected", f"action {action!r} not allowed for a {row[0]} delivery"
                if action == "mark_delivered":
                    status, outcome = "sent", "operator verified delivered"
                elif action == "abandon":
                    status, outcome = "cancelled", "operator abandoned (not resent)"
                else:  # resend / retry after the operator confirmed it did not arrive
                    status, outcome = "pending", f"operator requested {action}"
                self._conn.execute(
                    "UPDATE schedule_outbox SET status=?, provider_outcome=?, next_attempt_at=?, attempts=CASE WHEN ? "
                    "THEN 0 ELSE attempts END, updated_at=? WHERE id=?",
                    (status, outcome, now, status == "pending", now, tid))
                self._refresh_delivery(row[1])
                return "applied", f"delivery {tid} -> {status}"
        return "rejected", f"unknown target kind {kind!r}"

    @contextlib.contextmanager
    def _immediate(self):  # type: ignore[no-untyped-def]
        if self._conn.in_transaction:
            self._conn.commit()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # --- heartbeat reads

    def heartbeat_state(self, schedule_id: str, account: str, service: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM heartbeat_state WHERE schedule_id=? AND account=? AND service=?",
                                 (schedule_id, account, service)).fetchone()
        return dict(row) if row else None

    def heartbeat_notified(self, schedule_id: str, account: str, service: str,
                           pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for key, change in pairs:
            if self._conn.execute(
                    "SELECT 1 FROM heartbeat_notified WHERE schedule_id=? AND account=? AND service=? "
                    "AND item_key=? AND change_id=?", (schedule_id, account, service, key, change)).fetchone():
                found.add((key, change))
        return found

    def pending_outbox_count(self, schedule_id: str, *, now: datetime | None = None) -> int:
        """Rows that are DUE now (deferred quiet-hours rows are counted separately)."""
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM schedule_outbox WHERE schedule_id=? AND status IN ('pending','sending') "
            "AND next_attempt_at<=?", (schedule_id, iso(now or now_utc()))).fetchone()[0])

    def deferred_outbox_count(self, schedule_id: str, *, now: datetime | None = None) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM schedule_outbox WHERE schedule_id=? AND status='pending' AND next_attempt_at>?",
            (schedule_id, iso(now or now_utc()))).fetchone()[0])

    def unannounced_undelivered(self, schedule_id: str, limit: int = 3) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, status, text, transport, channel, created_at FROM schedule_outbox WHERE schedule_id=? AND announced=0 "
            "AND status IN ('uncertain','failed') ORDER BY id LIMIT ?", (schedule_id, limit)).fetchall()

    def receipts(self, occurrence_id: int) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM heartbeat_receipts WHERE occurrence_id=? ORDER BY id",
                                  (occurrence_id,)).fetchall()

    def _apply_heartbeat(self, schedule_id: str, occurrence_id: int, run_id: int, commit: HeartbeatCommit) -> None:
        now = iso(now_utc())
        for st in commit.states:
            self._conn.execute(
                "INSERT INTO heartbeat_state(schedule_id,account,service,cursor,last_checked_at,last_ok_at,status,"
                "consecutive_failures,alerts_sent,last_alert_at,resync_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(schedule_id,account,service) DO UPDATE SET cursor=excluded.cursor, "
                "last_checked_at=excluded.last_checked_at, last_ok_at=excluded.last_ok_at, status=excluded.status, "
                "consecutive_failures=excluded.consecutive_failures, alerts_sent=excluded.alerts_sent, "
                "last_alert_at=excluded.last_alert_at, resync_count=excluded.resync_count, updated_at=excluded.updated_at",
                (schedule_id, st["account"], st["service"], st.get("cursor"), st.get("last_checked_at"),
                 st.get("last_ok_at"), st["status"], st.get("consecutive_failures", 0), st.get("alerts_sent", 0),
                 st.get("last_alert_at"), st.get("resync_count", 0), now))
        for account, service, key, change in commit.notified:
            self._conn.execute(
                "INSERT OR IGNORE INTO heartbeat_notified(schedule_id,account,service,item_key,change_id,notified_at)"
                " VALUES(?,?,?,?,?,?)", (schedule_id, account, service, key, change, now))
        for oid in commit.announced_outbox_ids:
            self._conn.execute("UPDATE schedule_outbox SET announced=1 WHERE id=?", (oid,))
        cutoff = iso(now_utc() - timedelta(days=commit.retention_days))
        self._conn.execute("DELETE FROM heartbeat_notified WHERE schedule_id=? AND notified_at<?", (schedule_id, cutoff))
        for r in commit.receipts:
            self._conn.execute(
                "INSERT OR IGNORE INTO heartbeat_receipts(occurrence_id,run_id,schedule_id,account,service,status,"
                "checked_at,receipt_hash,detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (occurrence_id, run_id, schedule_id, r["account"], r["service"], r["status"], r["checked_at"],
                 r["receipt_hash"], json.dumps(r)))

    # ------------------------------------------------------------- occurrences

    @staticmethod
    def _occ(row: sqlite3.Row) -> Occurrence:
        return Occurrence(**{k: row[k] for k in Occurrence.__dataclass_fields__})

    def get(self, occurrence_id: int) -> Occurrence:
        row = self._conn.execute("SELECT * FROM schedule_occurrences WHERE id=?", (occurrence_id,)).fetchone()
        if row is None:
            raise KeyError(occurrence_id)
        return self._occ(row)

    def _insert_occurrence(self, *, schedule_id: str, scheduled_for: datetime, trigger: str, job_kind: str,
                           run_safety: str, status: str, detail: str | None) -> int | None:
        now = iso(now_utc())
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO schedule_occurrences(schedule_id,scheduled_for,trigger,job_kind,run_safety,"
            "exec_status,detail,created_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (schedule_id, iso(scheduled_for), trigger, job_kind, run_safety, status, detail, now, now,
             now if status in TERMINAL_EXEC else None))
        return int(cur.lastrowid or 0) if cur.rowcount else None

    def cursor(self, schedule_id: str) -> datetime | None:
        row = self._conn.execute("SELECT last_scheduled_for FROM schedule_cursors WHERE schedule_id=?",
                                 (schedule_id,)).fetchone()
        return parse(row[0]) if row else None

    def materialize(self, *, schedule_id: str, cron: str, tz: Any, baseline: datetime, now: datetime,
                    job_kind: str, run_safety: str, overlap: str, misfire: str, grace_seconds: int) -> list[int]:
        """Create the occurrence(s) now due for a schedule; one transaction.

        ``baseline`` seeds the cursor the first time a schedule is seen (legacy
        ``last_fired_at`` or ``created_at``).  Only the newest due tick may run;
        older missed ticks are recorded ``skipped_misfire`` (bounded) so old cron
        ticks are never replayed.  The cursor advances in the same transaction.
        Returns the ids of occurrences created in ``pending``.
        """
        created: list[int] = []
        with self._conn:
            cur = self.cursor(schedule_id) or baseline.astimezone(UTC)
            newest_local = croniter(cron, now.astimezone(tz)).get_prev(datetime)
            newest = newest_local.astimezone(UTC)
            if newest <= cur:
                if self.cursor(schedule_id) is None:
                    self._set_cursor(schedule_id, cur)
                return created
            # Older missed ticks (bounded) between the cursor and the newest tick.
            it = croniter(cron, cur.astimezone(tz))
            older = 0
            while older < MAX_RECORDED_MISFIRES:
                tick = it.get_next(datetime).astimezone(UTC)
                if tick >= newest:
                    break
                self._insert_occurrence(schedule_id=schedule_id, scheduled_for=tick, trigger="cron",
                                        job_kind=job_kind, run_safety=run_safety, status="skipped_misfire",
                                        detail="older missed tick; only the newest due tick runs")
                older += 1
            status, detail = "pending", None
            if misfire == "skip_late" and (now - newest).total_seconds() > grace_seconds:
                status, detail = "skipped_misfire", f"late by more than {grace_seconds}s"
            elif self._schedule_busy(schedule_id, overlap):
                status, detail = "skipped_overlap", f"previous occurrence still active (policy={overlap})"
            occ = self._insert_occurrence(schedule_id=schedule_id, scheduled_for=newest, trigger="cron",
                                          job_kind=job_kind, run_safety=run_safety, status=status, detail=detail)
            self._set_cursor(schedule_id, newest)
            if occ is not None and status == "pending":
                created.append(occ)
        return created

    def _schedule_busy(self, schedule_id: str, overlap: str) -> bool:
        active = self._conn.execute(
            "SELECT exec_status, COUNT(*) FROM schedule_occurrences WHERE schedule_id=? "
            "AND exec_status IN ('pending','claimed','running') GROUP BY exec_status", (schedule_id,)).fetchall()
        counts = {r[0]: r[1] for r in active}
        if overlap == "skip":
            return bool(counts)
        # queue: at most one waiting occurrence behind the active one
        return int(counts.get("pending", 0)) >= 1

    def _set_cursor(self, schedule_id: str, when: datetime) -> None:
        self._conn.execute(
            "INSERT INTO schedule_cursors(schedule_id,last_scheduled_for,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET last_scheduled_for=excluded.last_scheduled_for, updated_at=excluded.updated_at",
            (schedule_id, iso(when), iso(now_utc())))

    def create_manual(self, *, schedule_id: str, job_kind: str, run_safety: str) -> int:
        """An operator/test-initiated occurrence with a unique scheduled_for."""
        when = now_utc()
        with self._conn:
            occ = self._insert_occurrence(schedule_id=schedule_id, scheduled_for=when, trigger="manual",
                                          job_kind=job_kind, run_safety=run_safety, status="pending", detail=None)
        if occ is None:  # microsecond collision: practically impossible, but never silently drop
            return self.create_manual(schedule_id=schedule_id, job_kind=job_kind, run_safety=run_safety)
        return occ

    def pending(self, limit: int = 50) -> list[Occurrence]:
        rows = self._conn.execute(
            "SELECT * FROM schedule_occurrences o WHERE exec_status='pending' AND NOT EXISTS ("
            "SELECT 1 FROM schedule_occurrences a WHERE a.schedule_id=o.schedule_id AND a.id!=o.id "
            "AND a.exec_status IN ('claimed','running')) ORDER BY scheduled_for, id LIMIT ?",
            (limit,)).fetchall()
        return [self._occ(r) for r in rows]

    def claim(self, occurrence_id: int, *, owner: str, lease_seconds: float) -> bool:
        now = now_utc()
        with self._conn:
            row = self._conn.execute("SELECT schedule_id FROM schedule_occurrences WHERE id=? AND exec_status='pending'",
                                     (occurrence_id,)).fetchone()
            if row is None:
                return False
            busy = self._conn.execute(
                "SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND id!=? "
                "AND exec_status IN ('claimed','running')", (row[0], occurrence_id)).fetchone()
            if busy:
                return False
            self._conn.execute(
                "UPDATE schedule_occurrences SET exec_status='claimed', lease_owner=?, lease_expires_at=?, "
                "updated_at=? WHERE id=?",
                (owner, iso(now + timedelta(seconds=lease_seconds)), iso(now), occurrence_id))
        return True

    def start_run(self, occurrence_id: int, *, owner: str) -> int:
        """claimed -> running; opens a run row. Raises if the claim was lost."""
        now = iso(now_utc())
        with self._conn:
            changed = self._conn.execute(
                "UPDATE schedule_occurrences SET exec_status='running', attempts=attempts+1, started_at=?, "
                "updated_at=? WHERE id=? AND exec_status='claimed' AND lease_owner=?",
                (now, now, occurrence_id, owner)).rowcount
            if not changed:
                raise PermissionError("occurrence claim lost")
            attempt = self.get(occurrence_id).attempts
            cur = self._conn.execute(
                "INSERT INTO schedule_runs(occurrence_id,attempt,status,side_effects,owner,started_at) "
                "VALUES(?,?,'running','unknown',?,?)", (occurrence_id, attempt, owner, now))
        return int(cur.lastrowid or 0)

    def finish_run(self, occurrence_id: int, run_id: int, *, status: str, side_effects: str,
                   result: dict[str, Any] | None = None, error: str | None = None,
                   outbox: list[tuple[Any, ...]] | None = None, chunk_chars: int = 3500,
                   delivery_status: str | None = None, detail: str | None = None,
                   heartbeat: HeartbeatCommit | None = None) -> bool:
        """Atomically close the run, set exec status and enqueue outbox rows.

        ``outbox`` is a list of ``(transport, channel, text)``.  Returns False if
        the occurrence is no longer ``running`` (e.g. recovered or cancelled
        meanwhile) - in which case nothing is written, so a late finisher can
        never enqueue a duplicate delivery.
        """
        now = iso(now_utc())
        with self._conn:
            row = self._conn.execute("SELECT schedule_id, exec_status FROM schedule_occurrences WHERE id=?",
                                     (occurrence_id,)).fetchone()
            if row is None or row["exec_status"] != "running":
                return False
            self._conn.execute(
                "UPDATE schedule_runs SET status=?, side_effects=?, finished_at=?, result_json=?, error=? WHERE id=?",
                (status, side_effects, now, json.dumps(result) if result is not None else None, error, run_id))
            delivery = delivery_status or "none"
            if outbox:
                delivery = "pending"
                for msg_index, spec in enumerate(outbox):
                    transport, channel, text = spec[:3]
                    not_before = spec[3] if len(spec) > 3 and spec[3] else now  # quiet hours defer delivery
                    parts = split_chunks(text, chunk_chars)
                    for i, part in enumerate(parts):
                        self._conn.execute(
                            "INSERT INTO schedule_outbox(occurrence_id,run_id,schedule_id,transport,channel,"
                            "msg_index,chunk_index,chunk_count,text,next_attempt_at,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (occurrence_id, run_id, row["schedule_id"], transport, channel, msg_index, i, len(parts),
                             part, not_before, now, now))
            if heartbeat is not None:
                self._apply_heartbeat(row["schedule_id"], occurrence_id, run_id, heartbeat)
            self._conn.execute(
                "UPDATE schedule_occurrences SET exec_status=?, delivery_status=?, detail=?, lease_owner=NULL, "
                "lease_expires_at=NULL, finished_at=?, updated_at=? WHERE id=?",
                (status, delivery, detail, now, now, occurrence_id))
        return True

    def end_unrun(self, occurrence_id: int, *, status: str, detail: str) -> bool:
        """Terminal transition for a pending/claimed occurrence that never ran."""
        now = iso(now_utc())
        with self._conn:
            changed: int = self._conn.execute(
                "UPDATE schedule_occurrences SET exec_status=?, detail=?, lease_owner=NULL, lease_expires_at=NULL, "
                "finished_at=?, updated_at=? WHERE id=? AND exec_status IN ('pending','claimed')",
                (status, detail, now, now, occurrence_id)).rowcount
            return changed > 0

    def recover_expired(self, *, now: datetime, max_run_attempts: int,
                        owner_alive: Callable[[str | None], bool | None] | None = None) -> list[Recovery]:
        """Apply the recovery rules. Never re-runs side-effecting work.

        With ``owner_alive`` a claim is recovered when its lease expired AND its owner is not
        provably alive, or immediately when the owner is provably dead.  A claim whose owner is
        provably ALIVE (another live daemon, or this one) is never taken over.
        """
        out: list[Recovery] = []
        ts = iso(now)
        with self._conn:
            candidates = self._conn.execute(
                "SELECT * FROM schedule_occurrences WHERE exec_status IN ('claimed','running')").fetchall()
            rows = []
            for row in candidates:
                expired = (row["lease_expires_at"] or "") < ts
                alive = owner_alive(row["lease_owner"]) if owner_alive is not None else None
                if alive is True:
                    if expired:
                        out.append(Recovery(row["id"], row["schedule_id"], "skipped",
                                            f"lease expired but owner {row['lease_owner']} is still alive; not taking over"))
                    continue
                if expired or alive is False:
                    rows.append(row)
            for row in rows:
                occ_id, sid = row["id"], row["schedule_id"]
                if row["exec_status"] == "claimed":
                    # Crash after claim, before the run started: nothing executed.
                    self._conn.execute("UPDATE schedule_occurrences SET exec_status='pending', lease_owner=NULL, "
                                       "lease_expires_at=NULL, detail='re-pended: claim expired before run start', "
                                       "updated_at=? WHERE id=?", (ts, occ_id))
                    out.append(Recovery(occ_id, sid, "repended", "claim expired before run start"))
                    continue
                self._conn.execute("UPDATE schedule_runs SET status='abandoned', finished_at=?, "
                                   "error='lease expired while running' WHERE occurrence_id=? AND status='running'",
                                   (ts, occ_id))
                if row["run_safety"] == "read_only" and row["attempts"] < max_run_attempts:
                    self._conn.execute("UPDATE schedule_occurrences SET exec_status='pending', lease_owner=NULL, "
                                       "lease_expires_at=NULL, detail='re-pended: read-only run interrupted', "
                                       "updated_at=? WHERE id=?", (ts, occ_id))
                    out.append(Recovery(occ_id, sid, "repended", "read-only run interrupted; retrying"))
                elif row["run_safety"] == "read_only":
                    self._conn.execute("UPDATE schedule_occurrences SET exec_status='failed', lease_owner=NULL, "
                                       "lease_expires_at=NULL, detail='read-only run kept dying', finished_at=?, "
                                       "updated_at=? WHERE id=?", (ts, ts, occ_id))
                    out.append(Recovery(occ_id, sid, "failed", "read-only run exhausted attempts"))
                else:
                    self._conn.execute("UPDATE schedule_occurrences SET exec_status='unknown', lease_owner=NULL, "
                                       "lease_expires_at=NULL, detail='side effects unverifiable after interruption; "
                                       "not re-run', finished_at=?, updated_at=? WHERE id=?", (ts, ts, occ_id))
                    out.append(Recovery(occ_id, sid, "unknown", "side-effecting run interrupted; not retried"))
        return out

    def repend_running(self, occurrence_id: int, run_id: int, *, owner: str, detail: str) -> bool:
        """Graceful shutdown of a READ-ONLY run: hand it back to the queue for the next start."""
        now = iso(now_utc())
        with self._immediate():
            changed = self._conn.execute(
                "UPDATE schedule_occurrences SET exec_status='pending', lease_owner=NULL, lease_expires_at=NULL, "
                "detail=?, updated_at=? WHERE id=? AND exec_status='running' AND lease_owner=?",
                (detail, now, occurrence_id, owner)).rowcount
            if changed:
                self._conn.execute("UPDATE schedule_runs SET status='abandoned', finished_at=?, error=? WHERE id=?",
                                   (now, detail, run_id))
        return bool(changed)

    def prune(self, *, now: datetime, retention_days: float, text_retention_days: float) -> dict[str, int]:
        """Bounded retention. Anything unresolved (pending/uncertain/failed/unknown) is protected."""
        cutoff, text_cutoff = iso(now - timedelta(days=retention_days)), iso(now - timedelta(days=text_retention_days))
        counts = {"occurrences": 0, "outbox_text_purged": 0, "results_redacted": 0, "repairs": 0}
        with self._immediate():
            doomed = [r[0] for r in self._conn.execute(
                "SELECT o.id FROM schedule_occurrences o WHERE COALESCE(o.finished_at, o.created_at) < ? "
                "AND o.exec_status NOT IN ('pending','claimed','running','unknown','blocked') "
                "AND NOT EXISTS (SELECT 1 FROM schedule_outbox b WHERE b.occurrence_id=o.id "
                "AND b.status IN ('pending','sending','uncertain','failed')) "
                "AND NOT EXISTS (SELECT 1 FROM repair_requests q WHERE q.status='pending' AND "
                "((q.kind='run' AND q.target_id=o.id) OR (q.kind='outbox' AND q.target_id IN "
                "(SELECT id FROM schedule_outbox WHERE occurrence_id=o.id))))", (cutoff,))]
            for oid in doomed:
                for table in ("heartbeat_receipts", "schedule_outbox", "schedule_runs"):
                    self._conn.execute(f"DELETE FROM {table} WHERE occurrence_id=?", (oid,))
                self._conn.execute("DELETE FROM schedule_occurrences WHERE id=?", (oid,))
            counts["occurrences"] = len(doomed)
            counts["outbox_text_purged"] = self._conn.execute(
                "UPDATE schedule_outbox SET text='[purged]' WHERE status IN ('sent','cancelled') AND updated_at<? "
                "AND text!='[purged]'", (text_cutoff,)).rowcount
            for r in self._conn.execute("SELECT id, result_json FROM schedule_runs WHERE result_json LIKE '%\"notifications\"%' "
                                        "AND finished_at<?", (text_cutoff,)).fetchall():
                try:
                    data = json.loads(r["result_json"])
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("notifications"):
                    data["notifications"] = len(data["notifications"]) if isinstance(data["notifications"], list) else 0
                    self._conn.execute("UPDATE schedule_runs SET result_json=? WHERE id=?", (json.dumps(data), r["id"]))
                    counts["results_redacted"] += 1
            counts["repairs"] = self._conn.execute(
                "DELETE FROM repair_requests WHERE status!='pending' AND COALESCE(applied_at, requested_at)<?",
                (cutoff,)).rowcount
        return counts

    def cancel_schedule(self, schedule_id: str, *, detail: str) -> list[int]:
        """Cancel unrun occurrences and unsent output; return ids still running."""
        now = iso(now_utc())
        with self._conn:
            self._conn.execute(
                "UPDATE schedule_occurrences SET exec_status='cancelled', detail=?, lease_owner=NULL, "
                "lease_expires_at=NULL, finished_at=?, updated_at=? WHERE schedule_id=? "
                "AND exec_status IN ('pending','claimed')", (detail, now, now, schedule_id))
            self._conn.execute(
                "UPDATE schedule_outbox SET status='cancelled', provider_outcome='cancelled: schedule deleted', "
                "updated_at=? WHERE schedule_id=? AND status='pending'", (now, schedule_id))
            self._refresh_delivery_for_schedule(schedule_id)
            rows = self._conn.execute("SELECT id FROM schedule_occurrences WHERE schedule_id=? AND exec_status='running'",
                                      (schedule_id,)).fetchall()
        return [r[0] for r in rows]

    def active_schedule_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT DISTINCT schedule_id FROM schedule_occurrences "
                                  "WHERE exec_status IN ('pending','claimed','running')").fetchall()
        rows2 = self._conn.execute("SELECT DISTINCT schedule_id FROM schedule_outbox WHERE status='pending'").fetchall()
        return {r[0] for r in rows} | {r[0] for r in rows2}

    def recent(self, schedule_id: str | None = None, limit: int = 50) -> list[Occurrence]:
        sql = "SELECT * FROM schedule_occurrences"
        args: list[Any] = []
        if schedule_id:
            sql += " WHERE schedule_id=?"
            args.append(schedule_id)
        rows = self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._occ(r) for r in rows]

    def runs(self, occurrence_id: int) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM schedule_runs WHERE occurrence_id=? ORDER BY id", (occurrence_id,)).fetchall()

    def outbox(self, occurrence_id: int) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM schedule_outbox WHERE occurrence_id=? ORDER BY msg_index, chunk_index",
                                  (occurrence_id,)).fetchall()

    def set_exec_status(self, occurrence_id: int, status: str, detail: str) -> None:
        now = iso(now_utc())
        with self._conn:
            self._conn.execute("UPDATE schedule_occurrences SET exec_status=?, detail=?, finished_at=?, updated_at=? "
                               "WHERE id=?", (status, detail, now, now, occurrence_id))

    # ------------------------------------------------------------------ outbox

    def claim_outbox(self, *, now: datetime, lease_seconds: float, limit: int = 20,
                     exclude: frozenset[int] = frozenset()) -> list[OutboxItem]:
        """Claim due chunks. Chunk i is only claimable once chunk i-1 was sent."""
        ts = iso(now)
        items: list[OutboxItem] = []
        skip = ",".join(str(int(i)) for i in exclude) or "-1"
        with self._conn:
            rows = self._conn.execute(
                f"SELECT * FROM schedule_outbox o WHERE id NOT IN ({skip}) AND status='pending' AND next_attempt_at<=? AND (chunk_index=0 OR "
                "EXISTS (SELECT 1 FROM schedule_outbox p WHERE p.occurrence_id=o.occurrence_id AND p.msg_index=o.msg_index "
                "AND p.chunk_index=o.chunk_index-1 AND p.status='sent')) ORDER BY occurrence_id, msg_index, chunk_index LIMIT ?",
                (ts, limit)).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE schedule_outbox SET status='sending', attempts=attempts+1, lease_expires_at=?, updated_at=? "
                    "WHERE id=?", (iso(now + timedelta(seconds=lease_seconds)), ts, r["id"]))
                items.append(OutboxItem(r["id"], r["occurrence_id"], r["schedule_id"], r["transport"], r["channel"],
                                        r["chunk_index"], r["chunk_count"], r["text"], r["attempts"] + 1))
        return items

    def outbox_sent(self, item: OutboxItem, *, provider_outcome: str = "ok") -> None:
        self._outbox_done(item, "sent", provider_outcome, None)

    def outbox_failed(self, item: OutboxItem, *, error: str, retryable: bool, max_attempts: int,
                      backoff_base: float, backoff_max: float) -> str:
        """Record a failed send. retryable=False means the send may have happened (uncertain)."""
        now = now_utc()
        if not retryable:
            self._outbox_done(item, "uncertain", f"uncertain: {error[:200]}", error)
            return "uncertain"
        if item.attempts >= max_attempts:
            self._outbox_done(item, "failed", f"error: {error[:200]}", error)
            return "failed"
        delay = min(backoff_max, backoff_base * (2 ** (item.attempts - 1)))
        with self._conn:
            self._conn.execute(
                "UPDATE schedule_outbox SET status='pending', next_attempt_at=?, provider_outcome=?, last_error=?, "
                "lease_expires_at=NULL, updated_at=? WHERE id=? AND status='sending'",
                (iso(now + timedelta(seconds=delay)), f"error (will retry): {error[:200]}", error[:300],
                 iso(now), item.id))
        return "pending"

    def _outbox_done(self, item: OutboxItem, status: str, outcome: str, error: str | None) -> None:
        now = iso(now_utc())
        with self._conn:
            self._conn.execute(
                "UPDATE schedule_outbox SET status=?, provider_outcome=?, last_error=?, lease_expires_at=NULL, "
                "updated_at=? WHERE id=? AND status='sending'", (status, outcome, error, now, item.id))
            if status in ("uncertain", "failed", "cancelled"):
                self._strand_later_chunks(item.id, status)
            self._refresh_delivery(item.occurrence_id)

    def _strand_later_chunks(self, outbox_id: int, why: str) -> None:
        """Later parts of a message are never released after an earlier part ended without being sent."""
        self._conn.execute(
            "UPDATE schedule_outbox SET status='cancelled', provider_outcome=?, updated_at=? WHERE status='pending' "
            "AND (occurrence_id, msg_index) = (SELECT occurrence_id, msg_index FROM schedule_outbox WHERE id=?) "
            "AND chunk_index > (SELECT chunk_index FROM schedule_outbox WHERE id=?)",
            (f"cancelled: an earlier part of this message ended {why} (later parts are not sent out of order)",
             iso(now_utc()), outbox_id, outbox_id))

    def recover_outbox(self, *, now: datetime) -> int:
        """An in-flight send whose lease expired may or may not have been delivered: uncertain, never resent."""
        ts = iso(now)
        with self._conn:
            rows = self._conn.execute("SELECT id, occurrence_id FROM schedule_outbox WHERE status='sending' "
                                      "AND lease_expires_at<?", (ts,)).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE schedule_outbox SET status='uncertain', provider_outcome='uncertain: send interrupted', "
                    "lease_expires_at=NULL, updated_at=? WHERE id=?", (ts, r["id"]))
                self._strand_later_chunks(r["id"], "uncertain")
                self._refresh_delivery(r["occurrence_id"])
        return len(rows)

    def _refresh_delivery_for_schedule(self, schedule_id: str) -> None:
        for r in self._conn.execute("SELECT DISTINCT occurrence_id FROM schedule_outbox WHERE schedule_id=?",
                                    (schedule_id,)).fetchall():
            self._refresh_delivery(r[0])

    def _refresh_delivery(self, occurrence_id: int) -> None:
        counts = {r[0]: r[1] for r in self._conn.execute(
            "SELECT status, COUNT(*) FROM schedule_outbox WHERE occurrence_id=? GROUP BY status", (occurrence_id,))}
        if not counts:
            return
        total = sum(counts.values())
        if counts.get("uncertain"):
            status = "uncertain"
        elif counts.get("failed"):
            status = "failed"
        elif counts.get("sent", 0) == total:
            status = "delivered"
        elif counts.get("cancelled", 0) == total:
            status = "cancelled"
        elif counts.get("sent"):
            status = "partial"
        else:
            status = "pending"
        self._conn.execute("UPDATE schedule_occurrences SET delivery_status=?, updated_at=? WHERE id=?",
                           (status, iso(now_utc()), occurrence_id))

    def set_delivery_status(self, occurrence_id: int, status: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE schedule_occurrences SET delivery_status=?, updated_at=? WHERE id=?",
                               (status, iso(now_utc()), occurrence_id))
