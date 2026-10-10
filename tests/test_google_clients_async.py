from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.tools.google_clients import GoogleClients


def _clients(tmp_path: Path) -> GoogleClients:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    return GoogleClients(cfg)


async def test_blocking_operation_does_not_stop_heartbeat(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    gate = threading.Event()
    monkeypatch.setattr(clients, "_service", lambda slug, api: object())
    def blocked(_service: object) -> dict[str, bool]:
        gate.wait()
        return {"ok": True}

    task = asyncio.create_task(clients.run("one", "gmail", blocked))
    await asyncio.sleep(0)
    ticks = 0
    for _ in range(3):
        await asyncio.sleep(0.01)
        ticks += 1
    gate.set()
    assert await task == {"ok": True}
    assert ticks == 3
    clients.close(wait=True)


async def test_same_account_serializes_and_different_account_progresses(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    active: dict[str, int] = {"one": 0, "two": 0}
    max_active: dict[str, int] = {"one": 0, "two": 0}
    lock = threading.Lock()
    monkeypatch.setattr(clients, "_service", lambda slug, api: slug)

    def op(slug: str) -> str:
        with lock:
            active[slug] += 1
            max_active[slug] = max(max_active[slug], active[slug])
        time.sleep(0.03)
        with lock:
            active[slug] -= 1
        return slug

    results = await asyncio.gather(
        clients.run("one", "gmail", op), clients.run("one", "gmail", op), clients.run("two", "gmail", op)
    )
    assert results == ["one", "one", "two"]
    assert max_active["one"] == 1
    assert max_active["two"] == 1
    clients.close(wait=True)


async def test_timeout_and_close_bound_pending_work(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    release = threading.Event()
    monkeypatch.setattr(clients, "_service", lambda slug, api: object())
    with pytest.raises(asyncio.TimeoutError):
        await clients.run("one", "gmail", lambda _service: release.wait(), timeout_seconds=0.01)
    # The single worker is still occupied, but no new executor/workers are
    # created and close cancels queued work without claiming remote success.
    queued = asyncio.create_task(clients.run("one", "gmail", lambda _service: {"late": True}))
    clients.close(wait=False)
    release.set()
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await queued
