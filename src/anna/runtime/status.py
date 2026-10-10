"""Daemon-written runtime status snapshot (``state/runtime-status.json``).

``anna doctor`` and the dashboard are separate processes and must not guess
health from config.  The daemon records *observed* facts here: when each
transport's receiver and API were last verified, and when the Claude SDK last
completed (or failed) a real turn.  Only verified observations are recorded;
there is no "enabled => healthy" shortcut, and nothing secret is stored (error
strings are reduced to an exception class name plus a short redacted message).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SECRETISH = re.compile(r"(xox[a-z]-[\w-]+|sk-[\w-]{8,}|\d{6,}:[\w-]{20,}|Bearer\s+[\w.-]+|"
                        r"(token|key|secret|password)=\S+)", re.I)


def redact(text: str, limit: int = 120) -> str:
    cleaned = _SECRETISH.sub("[redacted]", " ".join((text or "").split()))
    return cleaned[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusRecorder:
    def __init__(self, path: Path, *, flush_interval: float = 2.0) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._flush_interval = flush_interval
        self._last_flush = 0.0
        self._data: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "pid": os.getpid(), "started_at": _now(), "written_at": _now(),
            "transports": {}, "sdk": {}, "memory_extraction": {}, "scheduler": {},
        }

    # --- recording (all best-effort: status writing must never break the daemon)

    def transport(self, name: str, *, healthy: bool, receiver_alive: bool | None = None,
                  api_ok: bool | None = None, error: str | None = None) -> None:
        with self._lock:
            entry = self._data["transports"].setdefault(name, {})
            now = _now()
            entry.update(last_checked_at=now, healthy=healthy, receiver_alive=receiver_alive, api_ok=api_ok)
            if healthy:
                entry["last_healthy_at"] = now
            if error:
                entry["last_error"] = redact(error)
        self.flush(force=True)

    def sdk_success(self) -> None:
        with self._lock:
            self._data["sdk"].update(last_success_at=_now())
        self.flush()

    def sdk_error(self, error: str) -> None:
        with self._lock:
            self._data["sdk"].update(last_error_at=_now(), last_error=redact(error))
        self.flush(force=True)

    def memory_extraction(self, *, running: bool, blocked_reason: str | None = None) -> None:
        with self._lock:
            self._data["memory_extraction"] = {"running": running, "blocked_reason": blocked_reason,
                                               "updated_at": _now()}
        self.flush(force=True)

    def scheduler(self, *, running: bool) -> None:
        with self._lock:
            self._data["scheduler"] = {"running": running, "updated_at": _now()}
        self.flush(force=True)

    def stopped(self) -> None:
        with self._lock:
            self._data["stopped_at"] = _now()
        self.flush(force=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def flush(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < self._flush_interval:
            return
        try:
            with self._lock:
                self._data["written_at"] = _now()
                payload = json.dumps(self._data)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".status.", dir=self._path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
            self._last_flush = now
        except OSError:
            pass


_recorder: StatusRecorder | None = None


def set_recorder(recorder: StatusRecorder | None) -> None:
    global _recorder
    _recorder = recorder


def get_recorder() -> StatusRecorder | None:
    return _recorder


def read_snapshot(path: Path) -> dict[str, Any] | None:
    """Read-only helper for doctor/dashboard; never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
