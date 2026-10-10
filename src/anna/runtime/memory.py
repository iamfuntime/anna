"""Durable, privacy-scoped fact memory backed by SQLite FTS5.

The runtime owns facts and their provenance; transcripts remain the source
record.  This deliberately small service has no model dependency, so explicit
``remember`` can commit before a caller reports success.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class MemoryFact:
    id: int
    owner_id: str
    visibility_scope: str
    kind: str
    subject: str
    content: str
    source_event_id: str | None
    status: str
    supersedes_id: int | None


class MemoryStore:
    """SQLite memory store with immutable provenance and soft forgetting."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_migrations
                (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
            )
            if self._conn.execute("SELECT 1 FROM memory_migrations WHERE version=1").fetchone():
                return
            self._conn.execute(
                """CREATE TABLE facts (
                id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL,
                visibility_scope TEXT NOT NULL, kind TEXT NOT NULL,
                subject TEXT NOT NULL, content TEXT NOT NULL,
                source_event_id TEXT, status TEXT NOT NULL DEFAULT 'active',
                supersedes_id INTEGER REFERENCES facts(id),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            self._conn.execute(
                "CREATE VIRTUAL TABLE facts_fts USING fts5(subject, content, content='facts', content_rowid='id')"
            )
            self._conn.execute(
                "CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN "
                "INSERT INTO facts_fts(rowid,subject,content) VALUES(new.id,new.subject,new.content); END"
            )
            self._conn.execute(
                "CREATE TRIGGER facts_au AFTER UPDATE ON facts BEGIN "
                "INSERT INTO facts_fts(facts_fts,rowid,subject,content) VALUES('delete',old.id,old.subject,old.content); "
                "INSERT INTO facts_fts(rowid,subject,content) VALUES(new.id,new.subject,new.content); END"
            )
            self._conn.execute(
                "CREATE INDEX facts_scope_active ON facts(owner_id, visibility_scope, status)"
            )
            self._conn.execute(
                "INSERT INTO memory_migrations(version, applied_at) VALUES(1, ?)", (self._now(),)
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def remember(self, *, owner_id: str, visibility_scope: str, kind: str, subject: str,
                 content: str, source_event_id: str | None = None) -> MemoryFact:
        """Commit a fact transactionally and return it only after commit."""
        if not all(value.strip() for value in (owner_id, visibility_scope, kind, subject, content)):
            raise ValueError("owner_id, visibility_scope, kind, subject, and content are required")
        now = self._now()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO facts(owner_id,visibility_scope,kind,subject,content,source_event_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (owner_id, visibility_scope, kind, subject, content, source_event_id, now, now),
            )
        return self.get(int(cursor.lastrowid))

    def get(self, fact_id: int) -> MemoryFact:
        row = self._conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        if row is None:
            raise KeyError(fact_id)
        return self._fact(row)

    def correct(self, fact_id: int, *, content: str, source_event_id: str | None = None) -> MemoryFact:
        prior = self.get(fact_id)
        replacement = self.remember(owner_id=prior.owner_id, visibility_scope=prior.visibility_scope,
                                    kind=prior.kind, subject=prior.subject, content=content,
                                    source_event_id=source_event_id)
        with self._conn:
            self._conn.execute("UPDATE facts SET status='superseded', updated_at=? WHERE id=?", (self._now(), fact_id))
            self._conn.execute("UPDATE facts SET supersedes_id=? WHERE id=?", (fact_id, replacement.id))
        return self.get(replacement.id)

    def forget(self, fact_id: int) -> None:
        with self._conn:
            changed = self._conn.execute("UPDATE facts SET status='forgotten', updated_at=? WHERE id=? AND status='active'", (self._now(), fact_id)).rowcount
        if not changed:
            raise KeyError(fact_id)

    def search(self, *, owner_id: str, visibility_scopes: set[str], query: str, limit: int = 8) -> list[MemoryFact]:
        if not query.strip() or not visibility_scopes:
            return []
        placeholders = ",".join("?" for _ in visibility_scopes)
        rows = self._conn.execute(
            f"SELECT f.* FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid "
            f"WHERE facts_fts MATCH ? AND f.owner_id=? AND f.visibility_scope IN ({placeholders}) "
            "AND f.status='active' ORDER BY bm25(facts_fts), f.updated_at DESC LIMIT ?",
            (query, owner_id, *sorted(visibility_scopes), limit),
        ).fetchall()
        return [self._fact(row) for row in rows]

    @staticmethod
    def _fact(row: sqlite3.Row) -> MemoryFact:
        return MemoryFact(**{key: row[key] for key in MemoryFact.__dataclass_fields__})
