"""Intents the dashboard may queue for the daemon-owned Google connector.

The dashboard process never touches the daemon's cached Google services or refreshes tokens.  It inserts a row
into ``control_requests``; the daemon's scheduler poll applies it on the account's serialized owner lane
(``invalidate``: drop cached services after a reconnect/removal; ``verify``: a live read-only check that the token
still works and still belongs to the configured mailbox).  Results are short, non-secret strings.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from anna.runtime.state_read import ReadOnlyDB, Unavailable

KINDS = ("google_invalidate", "google_verify")


class ControlError(ValueError):
    pass


def queue_control(state_dir: Path, *, kind: str, subject: str, requested_by: str = "web:operator") -> int:
    if kind not in KINDS:
        raise ControlError("unknown control kind")
    path = state_dir / "runtime.sqlite3"
    try:
        db = ReadOnlyDB(path)
        has = db.has_table("control_requests")
        db.close()
    except Unavailable as exc:
        raise ControlError("the daemon has not created its ledger yet (start it once)") from exc
    if not has:
        raise ControlError("the daemon has not migrated its ledger yet: restart the daemon, then retry")
    conn = sqlite3.connect(path, timeout=2.0)
    try:
        with conn:
            if conn.execute("SELECT 1 FROM control_requests WHERE kind=? AND subject=? AND status='pending'",
                            (kind, subject)).fetchone():
                raise ControlError("that request is already waiting for the daemon")
            cur = conn.execute(
                "INSERT INTO control_requests(kind,subject,requested_by,requested_at) VALUES(?,?,?,?)",
                (kind, subject, requested_by, datetime.now(UTC).isoformat(timespec="microseconds")))
            return int(cur.lastrowid or 0)
    finally:
        conn.close()


def latest_control(state_dir: Path, kind: str, subject: str) -> dict[str, str] | None:
    try:
        db = ReadOnlyDB(state_dir / "runtime.sqlite3")
    except Unavailable:
        return None
    try:
        if not db.has_table("control_requests"):
            return None
        row = db.one("SELECT status, result, requested_at, applied_at FROM control_requests WHERE kind=? AND subject=? "
                     "ORDER BY id DESC LIMIT 1", (kind, subject))
        return dict(row) if row else None
    except Unavailable:
        return None
    finally:
        db.close()
