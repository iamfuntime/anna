from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.tools.google_auth import GoogleAuthError
from anna.tools.google_clients import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GoogleBackpressureError,
    GoogleClients,
)


def _clients(tmp_path: Path) -> GoogleClients:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.google.accounts.extend(
        [
            GoogleAccountConfig(slug="one", email="one@example.test", auth_type="oauth", credentials_file="one.json"),
            GoogleAccountConfig(slug="two", email="two@example.test", auth_type="oauth", credentials_file="two.json"),
        ]
    )
    return GoogleClients(cfg)


async def test_blocking_operation_does_not_stop_heartbeat(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    gate = threading.Event()
    monkeypatch.setattr(clients, "_service", lambda slug, api, lane: object())

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
    monkeypatch.setattr(clients, "_service", lambda slug, api, lane: slug)

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
    assert max_active == {"one": 1, "two": 1}
    clients.close(wait=True)


async def test_timeout_keeps_provider_work_serialized_until_it_returns(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    release = threading.Event()
    started: list[str] = []
    monkeypatch.setattr(clients, "_service", lambda slug, api, lane: api)
    with pytest.raises(asyncio.TimeoutError):
        await clients.run("one", "gmail", lambda _: release.wait(), timeout_seconds=0.01)
    queued = asyncio.create_task(clients.run("one", "calendar", lambda api: started.append(api)))
    await asyncio.sleep(0.01)
    assert started == []
    release.set()
    assert await queued is None
    assert started == ["calendar"]
    clients.close(wait=True)


async def test_same_account_gmail_and_calendar_share_refresh_lane(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_service(slug: str, api: str, lane: object) -> str:
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return api

    monkeypatch.setattr(clients, "_service", fake_service)
    assert await asyncio.gather(
        clients.run("one", "gmail", lambda api: api),
        clients.run("one", "calendar", lambda api: api),
    ) == ["gmail", "calendar"]
    assert maximum == 1
    clients.close(wait=True)


async def test_queue_is_bounded_and_cancelled_queued_call_never_executes(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    release = threading.Event()
    executed: list[str] = []
    monkeypatch.setattr(clients, "_service", lambda slug, api, lane: api)

    first = asyncio.create_task(clients.run("one", "gmail", lambda _: release.wait()))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(clients.run("one", "calendar", lambda api: executed.append(api)))
    await asyncio.sleep(0.01)
    with pytest.raises(GoogleBackpressureError):
        await clients.run("one", "gmail", lambda _: None)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    assert await first is True
    await asyncio.sleep(0)
    assert executed == []
    clients.close(wait=True)


async def test_unknown_slug_does_not_allocate_owner_lane(tmp_path: Path) -> None:
    clients = _clients(tmp_path)
    with pytest.raises(GoogleAuthError):
        await clients.run("unknown", "gmail", lambda _: None)
    assert clients._lanes == {}


def test_service_configures_httplib2_deadline(tmp_path: Path, monkeypatch) -> None:
    """Discovery receives a fresh transport with the configured socket timeout."""
    import google_auth_httplib2
    import googleapiclient.discovery
    import httplib2

    clients = _clients(tmp_path)
    captured: dict[str, object] = {}
    transport = object()
    credentials = object()
    monkeypatch.setattr("anna.tools.google_clients.load_credentials", lambda *args, **kwargs: credentials)
    monkeypatch.setattr(httplib2, "Http", lambda *, timeout: captured.setdefault("timeout", timeout) and transport)
    authorized_transport = object()
    monkeypatch.setattr(
        google_auth_httplib2,
        "AuthorizedHttp",
        lambda creds, *, http: captured.update(credentials=creds, transport=http) or authorized_transport,
    )

    def build(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(googleapiclient.discovery, "build", build)
    lane = clients._lane("one")
    clients._service("one", "gmail", lane)
    assert captured["timeout"] == GOOGLE_HTTP_TIMEOUT_SECONDS
    assert captured["credentials"] is credentials
    assert captured["transport"] is transport
    assert captured["kwargs"] == {"http": authorized_transport, "cache_discovery": False}
    clients.close(wait=True)


async def test_invalidate_and_close_are_safe_while_provider_is_in_flight(tmp_path: Path, monkeypatch) -> None:
    clients = _clients(tmp_path)
    release = threading.Event()
    closed = threading.Event()

    class Service:
        def close(self) -> None:
            closed.set()

    def service(slug: str, api: str, lane: object) -> Service:
        result = Service()
        lane.cache[api] = result
        return result

    monkeypatch.setattr(clients, "_service", service)
    running = asyncio.create_task(clients.run("one", "gmail", lambda _: release.wait()))
    await asyncio.sleep(0.01)
    clients.invalidate("one")
    clients.close(wait=False)
    with pytest.raises(RuntimeError):
        await clients.run("one", "calendar", lambda _: None)
    release.set()
    assert await running is True
    for _ in range(10):
        if closed.is_set():
            break
        await asyncio.sleep(0.01)
    assert closed.is_set()
