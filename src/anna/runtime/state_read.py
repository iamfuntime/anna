"""Strictly read-only access to runtime SQLite state for doctor and dashboard views.

Opens ``mode=ro`` URIs so it can never create, migrate or modify a database
(``RuntimeLedger``/``MemoryStore`` run migrations on open; these helpers must not).
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


class Unavailable(Exception):
    """The database or table is missing or unreadable (reported, never raised to users)."""


class ReadOnlyDB:
    """Reads a private snapshot COPY of the database.

    Even a ``mode=ro`` connection to a WAL database creates ``-wal``/``-shm`` side
    files next to the original, which would be a modification of runtime state.
    So the main file (and its ``-wal`` if the daemon is mid-write) is copied into a
    private temp directory and read from there.  The copy is not atomic with a
    concurrently writing daemon; for diagnostics a momentarily stale view is
    acceptable, a modified state directory is not.
    """

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise Unavailable(f"{path.name} does not exist")
        self._tmp = tempfile.mkdtemp(prefix="anna-ro-")
        try:
            target = Path(self._tmp) / path.name
            shutil.copyfile(path, target)
            wal = path.with_name(path.name + "-wal")
            if wal.is_file():
                shutil.copyfile(wal, target.with_name(target.name + "-wal"))
            self._conn = sqlite3.connect(target, timeout=1.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only=ON")
        except (OSError, sqlite3.Error) as exc:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise Unavailable(f"{path.name}: {type(exc).__name__}") from exc

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise Unavailable(type(exc).__name__ + ": " + str(exc)[:80]) from exc

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        found = self.rows(sql, args)
        return found[0] if found else None

    def has_table(self, name: str) -> bool:
        return self.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None

    def close(self) -> None:
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)
