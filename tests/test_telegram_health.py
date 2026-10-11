"""Telegram readiness semantics against a PTB-faithful fake (no network).

Real ``Updater.start_polling()`` is a setup coroutine: it returns once polling
is established and exposes readiness via ``Updater.running``. A finished
``_updater_task`` is therefore the NORMAL healthy state; treating it as a dead
receiver made the watchdog restart + alert on a healthy bot every cycle.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anna.runtime.watchdog import Watchdog
from tests.test_transport_token_missing import (
    _FakeFilter,
    _FakeMessageHandler,
    _StubAlerter,
    _make_config,
    _make_telegram_adapter,
)


class _PtbUpdater:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self._running = False
        self._fail = fail

    @property
    def running(self) -> bool:
        return self._running

    async def start_polling(self) -> Any:
        self._running = True
        await asyncio.sleep(0)
        if self._fail is not None:
            self._running = False
            raise self._fail
        return object()  # real one returns the update queue, then finishes

    async def stop(self) -> None:
        self._running = False


class _PtbApp:
    fail: Exception | None = None
    get_me_error: Exception | None = None

    def __init__(self, token: str) -> None:
        self.updater = _PtbUpdater(fail=type(self).fail)
        self.bot = SimpleNamespace(get_me=self._get_me)

    async def _get_me(self) -> Any:
        if type(self).get_me_error is not None:
            raise type(self).get_me_error
        return SimpleNamespace(username="b")

    def add_handler(self, h: Any) -> None: ...
    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def shutdown(self) -> None: ...


class _Builder:
    def token(self, t: str) -> "_Builder":
        self._t = t
        return self

    def build(self) -> _PtbApp:
        return _PtbApp(self._t)


@pytest.fixture
def ptb(monkeypatch: pytest.MonkeyPatch) -> type[_PtbApp]:
    root = types.ModuleType("telegram")
    ext = types.ModuleType("telegram.ext")
    ext.ApplicationBuilder = _Builder  # type: ignore[attr-defined]
    ext.MessageHandler = _FakeMessageHandler  # type: ignore[attr-defined]
    ext.filters = SimpleNamespace(  # type: ignore[attr-defined]
        TEXT=_FakeFilter(), VOICE=_FakeFilter(), COMMAND=_FakeFilter()
    )
    root.ext = ext  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", root)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:tok")
    _PtbApp.fail = None
    _PtbApp.get_me_error = None
    return _PtbApp


@pytest.mark.asyncio
async def test_healthy_after_polling_setup_task_finished(tmp_path: Path, ptb: Any) -> None:
    adapter = _make_telegram_adapter(tmp_path)
    await adapter.start()
    try:
        await asyncio.sleep(0.1)
        assert adapter._updater_task is None or adapter._updater_task.done()
        assert await adapter.health_check() is True
        detail = await adapter.health_detail()
        assert detail == {"receiver_alive": True, "api_ok": True, "error": None}
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_unhealthy_when_updater_stopped_or_api_down(tmp_path: Path, ptb: Any) -> None:
    adapter = _make_telegram_adapter(tmp_path)
    await adapter.start()
    try:
        ptb.get_me_error = RuntimeError("net")
        assert await adapter.health_check() is False
        assert (await adapter.health_detail())["api_ok"] is False
        ptb.get_me_error = None
        await adapter._application.updater.stop()
        assert await adapter.health_check() is False
        assert (await adapter.health_detail())["receiver_alive"] is False
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_startup_failure_still_raises(tmp_path: Path, ptb: Any) -> None:
    ptb.fail = ValueError("bad token")
    adapter = _make_telegram_adapter(tmp_path)
    with pytest.raises(RuntimeError, match="failed during startup"):
        await adapter.start()


@pytest.mark.asyncio
async def test_watchdog_no_restart_or_alert_for_healthy_telegram(
    tmp_path: Path, ptb: Any
) -> None:
    adapter = _make_telegram_adapter(tmp_path)
    await adapter.start()
    restarts: list[int] = []
    orig = adapter.restart

    async def _restart() -> None:
        restarts.append(1)
        await orig()

    adapter.restart = _restart  # type: ignore[method-assign]
    alerter = _StubAlerter()
    cfg = _make_config(tmp_path)
    wd = Watchdog(config=cfg, adapters={"telegram": adapter}, router=SimpleNamespace(), alerter=alerter)
    try:
        await asyncio.sleep(0.1)  # let the real-PTB-style setup task finish
        for _ in range(8):  # > 3 consecutive cycles
            await wd._check_transports()
        assert restarts == []
        assert alerter.warn_calls == []
        assert wd._transport_failures["telegram"] == 0
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_watchdog_still_restarts_genuinely_dead_receiver(tmp_path: Path, ptb: Any) -> None:
    adapter = _make_telegram_adapter(tmp_path)
    await adapter.start()
    await adapter._application.updater.stop()
    restarts: list[int] = []

    async def _restart() -> None:
        restarts.append(1)

    adapter.restart = _restart  # type: ignore[method-assign]
    alerter = _StubAlerter()
    wd = Watchdog(config=_make_config(tmp_path), adapters={"telegram": adapter},
                  router=SimpleNamespace(), alerter=alerter)
    for _ in range(3):
        await wd._check_transports()
    assert restarts == [1]
    assert len(alerter.warn_calls) == 1
    await adapter.stop()
