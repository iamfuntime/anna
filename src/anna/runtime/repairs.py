"""Operator repair intents for unknown runs and uncertain/failed deliveries.

The dashboard process never edits scheduler state.  It may only *queue an intent*
(``queue_repair`` inserts one row into ``repair_requests``); the daemon's scheduler
poll validates the intent against the row's CURRENT status and applies it
(``RuntimeLedger.apply_repairs``).  There is deliberately no "rerun" action for
side-effecting runs and no blind resend: an operator must first say what they
verified (completed / not run / cannot tell; delivered / not delivered).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anna.runtime.ledger import RuntimeLedger
from anna.runtime.state_read import ReadOnlyDB, Unavailable

RUN_ACTIONS = RuntimeLedger.RUN_ACTIONS
OUTBOX_ACTIONS = RuntimeLedger.OUTBOX_ACTIONS
LABELS = {
    "verified_completed": "I verified it DID complete",
    "verified_not_run": "I verified NOTHING happened",
    "accept_unknown": "I cannot tell - close it as unknown",
    "mark_delivered": "I verified it WAS delivered",
    "resend": "I verified it was NOT delivered - resend it",
    "retry": "Retry delivery (it was never sent)",
    "abandon": "Do not send it",
}


class RepairError(ValueError):
    pass


def expected_confirmation(kind: str, target_id: int, action: str) -> str:
    """The exact phrase the operator must type; ties the action to the specific row."""
    return f"{action.upper()} {kind.upper()} {target_id}"


def _age(now: datetime, value: str | None) -> str:
    from anna.doctor import _age as raw_age
    from anna.doctor import human
    return human(raw_age(now, value))


def list_attention(state_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    """Rows needing a decision. Message bodies are NOT returned (sizes and ages only)."""
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {"available": True, "runs": [], "outbox": [], "requests": [], "needs_daemon_restart": False}
    try:
        db = ReadOnlyDB(state_dir / "runtime.sqlite3")
    except Unavailable as exc:
        return {**out, "available": False, "reason": str(exc)}
    try:
        for r in db.rows("SELECT id, schedule_id, scheduled_for, exec_status, detail, started_at, job_kind FROM "
                         "schedule_occurrences WHERE exec_status IN ('unknown','blocked') ORDER BY id DESC LIMIT 100"):
            out["runs"].append({**dict(r), "age": _age(now, r["started_at"] or r["scheduled_for"]),
                                "actions": list(RUN_ACTIONS[r["exec_status"]])})
        for r in db.rows("SELECT id, occurrence_id, schedule_id, transport, channel, status, attempts, chunk_index, "
                         "chunk_count, length(text) chars, provider_outcome, created_at FROM schedule_outbox "
                         "WHERE status IN ('uncertain','failed') ORDER BY id DESC LIMIT 100"):
            out["outbox"].append({**dict(r), "age": _age(now, r["created_at"]),
                                  "actions": list(OUTBOX_ACTIONS[r["status"]])})
        if db.has_table("repair_requests"):
            for r in db.rows("SELECT id, kind, target_id, action, status, result, requested_at FROM repair_requests "
                             "ORDER BY id DESC LIMIT 25"):
                out["requests"].append(dict(r))
        else:
            out["needs_daemon_restart"] = True
    except Unavailable as exc:
        return {**out, "available": False, "reason": str(exc)}
    finally:
        db.close()
    return out


def queue_repair(state_dir: Path, *, kind: str, target_id: int, action: str, expected_status: str,
                 requested_by: str, note: str = "") -> int:
    """Validate against the row as the dashboard sees it, then queue an intent for the daemon."""
    allowed = (RUN_ACTIONS if kind == "run" else OUTBOX_ACTIONS if kind == "outbox" else None)
    if allowed is None:
        raise RepairError("unknown repair kind")
    if action not in allowed.get(expected_status, ()):
        raise RepairError(f"action {action!r} is not allowed for a {expected_status} {kind}")
    path = state_dir / "runtime.sqlite3"
    try:
        db = ReadOnlyDB(path)
        table, col = ("schedule_occurrences", "exec_status") if kind == "run" else ("schedule_outbox", "status")
        row = db.one(f"SELECT {col} s FROM {table} WHERE id=?", (target_id,))
        has_queue = db.has_table("repair_requests")
        db.close()
    except Unavailable as exc:
        raise RepairError(f"runtime ledger unavailable: {exc}") from exc
    if row is None:
        raise RepairError("no such row")
    if row["s"] != expected_status:
        raise RepairError(f"state changed (now {row['s']}); reload and look again")
    if not has_queue:
        raise RepairError("the daemon has not migrated the ledger yet: restart the daemon, then retry")
    conn = sqlite3.connect(path, timeout=2.0)
    try:
        with conn:
            dup = conn.execute("SELECT 1 FROM repair_requests WHERE kind=? AND target_id=? AND status='pending'",
                               (kind, target_id)).fetchone()
            if dup:
                raise RepairError("a request for this item is already waiting for the daemon")
            cur = conn.execute(
                "INSERT INTO repair_requests(kind,target_id,action,expected_status,note,requested_by,requested_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (kind, target_id, action, expected_status, note[:200], requested_by,
                 datetime.now(UTC).isoformat(timespec="microseconds")))
            return int(cur.lastrowid or 0)
    finally:
        conn.close()
