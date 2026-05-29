"""Supervisor coroutine.

Per v3 section 6. Owns the ``asyncio.Lock`` around every write to the five
core identity files and around every ``agents/<slug>.md`` and
``skills/<agent>/<slug>.md`` creation. Tracks a poisoned-set of files that
the operator must clear via ``anna admin unpoison``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from anna.config import AnnaConfig
from anna.core.identity import CORE_FILES, CoreFile, count_tokens
from anna.log import audit_event, get_logger


class CoreFilePoisonedError(RuntimeError):
    """Raised when a write is attempted against a file marked poisoned."""


class Supervisor:
    """Locks and poison-flag tracking for sensitive writes.

    The supervisor is instantiated once per process. All core-file writes go
    through :meth:`write_core_file`. Sub-agent and skill creations go through
    :meth:`acquire`.
    """

    def __init__(self, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.supervisor")
        self._core_dir = config.core_dir
        self._audit_dir = config.audit_dir
        self._fsync = config.logging.audit.fsync_on_write
        self._locks: dict[str, asyncio.Lock] = {}
        self._poisoned: set[str] = set()
        self._poison_state_path = config.anna_home / "supervisor-state.json"
        self._load_poison_state()

    # ------------------------------------------------------------------
    # Poison state persistence
    # ------------------------------------------------------------------

    def _load_poison_state(self) -> None:
        if not self._poison_state_path.exists():
            return
        try:
            data = json.loads(self._poison_state_path.read_text(encoding="utf-8"))
            self._poisoned = set(data.get("poisoned", []))
        except (OSError, ValueError) as exc:
            self._log.warning("supervisor.poison_state.load_failed", error=str(exc))

    def _save_poison_state(self) -> None:
        try:
            self._poison_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._poison_state_path.write_text(
                json.dumps({"poisoned": sorted(self._poisoned)}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self._log.error("supervisor.poison_state.save_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Lock acquisition
    # ------------------------------------------------------------------

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def acquire(self, key: str) -> asyncio.Lock:
        """Return the lock for an arbitrary key (sub-agent, skill, etc.).

        The caller uses ``async with await supervisor.acquire(key)`` to
        serialize writes against the named resource.
        """
        return self._lock_for(key)

    # ------------------------------------------------------------------
    # Poison flag
    # ------------------------------------------------------------------

    def is_poisoned(self, file: str) -> bool:
        return file in self._poisoned

    def poison(self, file: str, reason: str) -> None:
        if file in self._poisoned:
            return
        self._poisoned.add(file)
        self._save_poison_state()
        audit_event(
            "audit.supervisor.poisoned",
            audit_dir=self._audit_dir,
            actor="anna",
            fsync_on_write=self._fsync,
            level="CRITICAL",
            file=file,
            reason=reason,
        )

    def unpoison(self, file: str, *, actor: str = "operator") -> bool:
        """Clear the poison flag. Returns True if the flag was set."""
        if file not in self._poisoned:
            return False
        self._poisoned.discard(file)
        self._save_poison_state()
        audit_event(
            "audit.supervisor.unpoisoned",
            audit_dir=self._audit_dir,
            actor=actor,
            fsync_on_write=self._fsync,
            file=file,
        )
        return True

    # ------------------------------------------------------------------
    # Core-file write
    # ------------------------------------------------------------------

    async def write_core_file(
        self,
        which: CoreFile,
        new_content: str,
        *,
        reason: str,
        conv_key: str,
    ) -> None:
        """Serialized write to one of the five core files.

        Refuses if the file is poisoned. On OSError the file is poisoned and
        the error is re-raised so the caller knows the write did not land.
        """
        spec = CORE_FILES[which]
        if self.is_poisoned(spec.name):
            raise CoreFilePoisonedError(
                f"refusing write to poisoned core file {spec.name}; "
                f"run 'anna-admin unpoison {spec.name}' to clear"
            )

        async with self._lock_for(spec.name):
            self._core_dir.mkdir(parents=True, exist_ok=True)
            path = self._core_dir / spec.name
            old_text = path.read_text(encoding="utf-8") if path.exists() else ""
            old_tokens = count_tokens(old_text)
            new_tokens = count_tokens(new_content)

            try:
                # Write through a temp file then rename so a crash mid-write
                # never leaves the core file half-baked. fsync the directory
                # to make the rename durable when the operator turns on
                # ``fsync_on_write``.
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(new_content, encoding="utf-8")
                if self._fsync:
                    with tmp.open("rb") as fp:
                        os.fsync(fp.fileno())
                tmp.replace(path)
            except OSError as exc:
                self.poison(spec.name, f"write failed: {exc}")
                audit_event(
                    "audit.core_file.write_failed",
                    audit_dir=self._audit_dir,
                    actor="anna",
                    conv_key=conv_key,
                    fsync_on_write=self._fsync,
                    level="CRITICAL",
                    file=spec.name,
                    error=str(exc),
                    reason=reason,
                )
                raise

            audit_event(
                "audit.core_file.write",
                audit_dir=self._audit_dir,
                actor="anna",
                conv_key=conv_key,
                fsync_on_write=self._fsync,
                file=spec.name,
                old_tokens=old_tokens,
                new_tokens=new_tokens,
                reason=reason,
            )

            # Soft-warning: log a WARNING when the file is within 10% of cap.
            if new_tokens >= int(spec.token_cap * 0.9):
                self._log.warning(
                    "supervisor.core_file.near_cap",
                    file=spec.name,
                    tokens=new_tokens,
                    cap=spec.token_cap,
                )

    # ------------------------------------------------------------------
    # Test/inspection
    # ------------------------------------------------------------------

    @property
    def poisoned_files(self) -> frozenset[str]:
        return frozenset(self._poisoned)

    @property
    def core_dir(self) -> Path:
        return self._core_dir
