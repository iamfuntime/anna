"""Validate ConversationRouter.shutdown() stops every active worker.

The router is what the process-level shutdown path in ``__main__.py`` relies
on to give each active worker a chance to run its closeout (checkpoint
write + per-core-file eviction) before the asyncio loop is torn down. The
test injects fake workers directly into ``router._workers`` to avoid the
full spawn path; the unit under test is the shutdown loop itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.router import ConversationRouter
from anna.runtime.supervisor import Supervisor


class _FakeWorker:
    """Minimal worker stand-in. Tracks whether stop() was awaited."""

    def __init__(self, key: str, transport: str = "slack") -> None:
        self.conversation_key = key
        self.transport = transport
        self.stop_called = False
        self.stop_count = 0

    async def stop(self) -> None:
        self.stop_count += 1
        await asyncio.sleep(0)  # yield once so concurrency is observable
        self.stop_called = True


class _FailingWorker(_FakeWorker):
    async def stop(self) -> None:
        self.stop_count += 1
        raise RuntimeError("simulated closeout failure")


def _make_router(tmp_path) -> ConversationRouter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)
    return ConversationRouter(config=cfg, supervisor=supervisor, adapters={})


@pytest.mark.asyncio
async def test_shutdown_with_no_workers_is_noop(tmp_path) -> None:
    router = _make_router(tmp_path)
    # Should not raise.
    await router.shutdown()


@pytest.mark.asyncio
async def test_shutdown_stops_every_active_worker(tmp_path) -> None:
    router = _make_router(tmp_path)
    workers = [
        _FakeWorker("slack:dm:U1"),
        _FakeWorker("slack:dm:U2"),
        _FakeWorker("telegram:dm:33"),
    ]
    for w in workers:
        router._workers[w.conversation_key] = w  # type: ignore[assignment]

    await router.shutdown()

    for w in workers:
        assert w.stop_called, f"worker {w.conversation_key} was not stopped"
        assert w.stop_count == 1, f"worker {w.conversation_key} stopped {w.stop_count} times"
    assert router._workers == {}, "registry should be drained"


@pytest.mark.asyncio
async def test_shutdown_continues_when_one_worker_raises(tmp_path) -> None:
    router = _make_router(tmp_path)
    good_a = _FakeWorker("slack:dm:U_GOOD_A")
    bad = _FailingWorker("slack:dm:U_BAD")
    good_b = _FakeWorker("slack:dm:U_GOOD_B")
    for w in (good_a, bad, good_b):
        router._workers[w.conversation_key] = w  # type: ignore[assignment]

    # Should not raise; failures are absorbed and logged.
    await router.shutdown()

    assert good_a.stop_called
    assert good_b.stop_called
    assert bad.stop_count == 1
    assert router._workers == {}


@pytest.mark.asyncio
async def test_shutdown_drains_registry_before_stopping(tmp_path) -> None:
    """A dispatch arriving mid-shutdown must not revive a worker we just closed."""
    router = _make_router(tmp_path)
    workers = [_FakeWorker(f"slack:dm:U{i}") for i in range(5)]
    for w in workers:
        router._workers[w.conversation_key] = w  # type: ignore[assignment]

    # Snapshot the registry from within the shutdown coroutine by patching stop().
    seen_during_stop: list[dict[str, Any]] = []

    async def _peeking_stop(self_w: _FakeWorker) -> None:
        seen_during_stop.append(dict(router._workers))
        self_w.stop_called = True
        self_w.stop_count += 1

    for w in workers:
        # Bind a method that captures the registry mid-shutdown.
        w.stop = _peeking_stop.__get__(w, _FakeWorker)  # type: ignore[method-assign]

    await router.shutdown()

    for snapshot in seen_during_stop:
        assert snapshot == {}, (
            "registry should be drained before any worker.stop() is called; "
            f"saw {list(snapshot)}"
        )
