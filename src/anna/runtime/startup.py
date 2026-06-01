"""Startup-time sentinel for clean-vs-unclean restart detection.

On clean shutdown ``__main__`` writes an ISO-8601 UTC timestamp to
``$ANNA_HOME/state/last_clean_shutdown``. On the next boot it reads and
deletes the file:

* sentinel present  -> the last shutdown was clean; the value is the
  timestamp of the prior ``__main__.shutdown.complete`` log line.
* sentinel missing  -> the prior process died without running the clean
  shutdown path (crash, ``SIGKILL``, OOM-kill, power loss).

The boot-time :class:`AdminAlerter` ping uses this to label the
restart for the operator. The file is intentionally tiny and
human-readable so the operator can ``cat`` it if curious.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


SENTINEL_NAME = "last_clean_shutdown"


def _sentinel_path(state_dir: Path) -> Path:
    return state_dir / SENTINEL_NAME


def write_clean_shutdown_sentinel(state_dir: Path, *, now: datetime | None = None) -> Path:
    """Write the clean-shutdown timestamp. Called from the shutdown path.

    Returns the path written. Caller is responsible for making this the
    *last* operation in a successful shutdown so a partial write only
    happens when the rest of shutdown succeeded.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    path = _sentinel_path(state_dir)
    path.write_text(ts + "\n", encoding="utf-8")
    return path


def read_and_clear_sentinel(state_dir: Path) -> datetime | None:
    """Read the sentinel and remove it. Called once at boot.

    Returns the timestamp if the sentinel was present and parseable.
    Returns ``None`` if it was missing (the unclean case) or unparseable.
    A read error (file gone between exists() and open()) is treated as
    missing.
    """
    path = _sentinel_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        ts = None
    # Always try to remove, even if parsing failed. We don't want stale
    # state to mislead the next boot.
    try:
        path.unlink()
    except OSError:
        pass
    return ts


def build_startup_message(
    *,
    last_clean_shutdown: datetime | None,
    boot_time: datetime,
    pid: int,
) -> str:
    """Render the operator-facing startup line.

    Two flavors:

    * clean:   ``ANNA started at <T> (PID N). Last clean shutdown: <T-1>.``
    * unclean: ``ANNA started at <T> (PID N). Previous shutdown was UNCLEAN
                (crash, kill, or power loss — no clean-shutdown
                sentinel found).``
    """
    boot_str = boot_time.replace(microsecond=0).isoformat()
    if last_clean_shutdown is not None:
        last_str = last_clean_shutdown.replace(microsecond=0).isoformat()
        return f"ANNA started at {boot_str} (PID {pid}). Last clean shutdown: {last_str}."
    return (
        f"ANNA started at {boot_str} (PID {pid}). "
        "Previous shutdown was UNCLEAN (crash, kill, or power loss "
        "— no clean-shutdown sentinel found)."
    )
