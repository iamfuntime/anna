"""Validate the continuous per-worker idle-close watcher.

Per v3 §6, each worker runs its own idle watcher that polls at quarter-gap
granularity and fires the router's close callback when ``last_active`` is
older than the configured DM or thread gap. The router then pops the worker
from its registry and calls ``worker.stop()``, which runs ``_closeout``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker


CONV_KEY = "slack:dm:UTEST"


def _make_worker(
    tmp_path: Path,
    *,
    on_idle_close=None,
    dm_gap_seconds: float = 1.0,
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    # Tight gap so the test doesn't take 8 hours. dm_gap_hours is a float.
    cfg.sessions.dm_gap_hours = dm_gap_seconds / 3600.0
    cfg.sessions.thread_gap_hours = dm_gap_seconds / 3600.0
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _noop_send(_msg):
        return None

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
        on_idle_close=on_idle_close,
    )


@pytest.mark.asyncio
async def test_idle_watcher_fires_close_callback_after_gap(tmp_path: Path) -> None:
    fired: list[str] = []

    async def _close(key: str) -> None:
        fired.append(key)

    worker = _make_worker(tmp_path, on_idle_close=_close, dm_gap_seconds=0.5)
    # Pre-age last_active so the very first poll trips the gap.
    worker.last_active = datetime.now(timezone.utc) - timedelta(seconds=5)

    # Run the idle watcher manually rather than via start() — this keeps
    # the test from spawning the SDK client.
    task = asyncio.create_task(worker._idle_watch())
    # Wait up to a couple of polls.
    for _ in range(40):
        if fired:
            break
        await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert fired == [CONV_KEY], f"expected exactly one fire, got {fired}"


@pytest.mark.asyncio
async def test_idle_watcher_does_not_fire_with_recent_activity(tmp_path: Path) -> None:
    fired: list[str] = []

    async def _close(key: str) -> None:
        fired.append(key)

    # gap of 1s; we'll bump last_active continuously so it never trips.
    worker = _make_worker(tmp_path, on_idle_close=_close, dm_gap_seconds=1.0)
    task = asyncio.create_task(worker._idle_watch())
    for _ in range(8):
        worker.last_active = datetime.now(timezone.utc)
        await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert fired == []


def test_idle_gap_seconds_cli_branch_uses_cli_idle_gap_minutes(tmp_path: Path) -> None:
    """CLI transport must read transports.cli.idle_gap_minutes, not sessions.*.

    The CLI conv_key shape (``cli:local:<user>``) doesn't trip ``is_dm``,
    so without the dedicated branch the worker would silently fall through
    to ``thread_gap_hours``. This test pins the branch.
    """
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.transports.cli.idle_gap_minutes = 1
    # Set the session gaps to wildly different values so a regression that
    # silently fell through would be immediately visible.
    cfg.sessions.dm_gap_hours = 8.0
    cfg.sessions.thread_gap_hours = 1.0
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _noop_send(_msg):
        return None

    worker = ConversationWorker(
        conversation_key="cli:local:funtime",
        transport="cli",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
    )

    assert worker._idle_gap_seconds() == 60.0


def test_idle_gap_seconds_preserves_is_dm_branch_for_non_cli(tmp_path: Path) -> None:
    """The new CLI branch goes first; existing dm/thread branching is intact.

    Pins the regression guard: a slack DM worker still reads dm_gap_hours,
    a slack thread worker still reads thread_gap_hours. Without this, a
    mis-ordered branch could shadow the existing path.
    """
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.transports.cli.idle_gap_minutes = 1
    cfg.sessions.dm_gap_hours = 8.0
    cfg.sessions.thread_gap_hours = 1.0
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _noop_send(_msg):
        return None

    dm_worker = ConversationWorker(
        conversation_key="slack:dm:UTEST",
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
    )
    thread_worker = ConversationWorker(
        conversation_key="slack:thread:C123:1700000000.000100",
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
    )

    assert dm_worker._idle_gap_seconds() == 8.0 * 3600.0
    assert thread_worker._idle_gap_seconds() == 1.0 * 3600.0


@pytest.mark.asyncio
async def test_idle_close_fires_exactly_once_through_router(tmp_path: Path) -> None:
    """End-to-end: router pops the worker on idle and runs stop() exactly once."""
    from anna.runtime.router import ConversationRouter
    from anna.transports.base import ChannelAdapter, InboundEvent

    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.sessions.dm_gap_hours = 0.5 / 3600.0
    cfg.sessions.thread_gap_hours = 0.5 / 3600.0
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)

    supervisor = Supervisor(config=cfg)

    class _FakeAdapter(ChannelAdapter):
        name = "slack"

        async def start(self): ...
        async def stop(self): ...
        async def send(self, message): ...
        def subscribe(self, handler): ...
        async def health_check(self): return True

        @classmethod
        def conversation_key_for(cls, event): return CONV_KEY

    router = ConversationRouter(
        config=cfg,
        supervisor=supervisor,
        adapters={"slack": _FakeAdapter()},
    )

    # Spawn a worker directly, then age its last_active so the idle watcher
    # trips. We skip dispatch() because we don't need to spawn the real SDK.
    async def _noop_send(_msg):
        return None

    worker = ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
        on_idle_close=router._idle_close_callback,
    )
    router._workers[CONV_KEY] = worker

    # Spy on worker.stop so we can count invocations.
    stop_count = 0
    real_stop = worker.stop

    async def _counted_stop():
        nonlocal stop_count
        stop_count += 1
        await real_stop()

    worker.stop = _counted_stop  # type: ignore[method-assign]

    # Age last_active and run the watcher.
    worker.last_active = datetime.now(timezone.utc) - timedelta(seconds=5)
    task = asyncio.create_task(worker._idle_watch())
    for _ in range(40):
        if CONV_KEY not in router._workers and stop_count >= 1:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert CONV_KEY not in router._workers, "router did not remove the worker"
    assert stop_count == 1, f"stop() was called {stop_count} times, expected 1"
