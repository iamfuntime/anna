"""Fail loudly when a transport boots without its token.

Part 1 of the TaskNote "ANNA: fail loudly when a transport boots without
its token". An enabled transport whose required token is missing must,
BEFORE any auth/connect attempt:

* log a WARNING naming the exact missing variable(s);
* emit the ``audit.transport.token_missing`` audit event;
* ping the operator through the AdminAlerter (best-effort); and
* skip the connect entirely instead of dying inside the auth handshake.

Token present → none of the above fires and the connect proceeds. The
connect paths are exercised against fake ``slack_bolt`` / ``telegram.ext``
modules injected into ``sys.modules`` (the ``test_main_dispatcher``
pattern) so no network is touched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation
from anna.transports.telegram import TelegramAdapter


# ---------------------------------------------------------------------------
# Shared stubs and helpers
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """Tiny stand-in for ``structlog.BoundLogger`` — collects events
    instead of writing them so the test can assert on what was logged.
    Mirrors the ``_SilentLogger`` pattern from
    ``tests/test_visibility_signal_telegram.py``.
    """

    def __init__(self) -> None:
        self.debugs: list[tuple[str, dict[str, Any]]] = []
        self.infos: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **kw: Any) -> None:
        self.debugs.append((event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self.infos.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.errors.append((event, kw))


class _StubAlerter:
    """Records AdminAlerter.warn calls; optionally raises to exercise the
    exception-isolation branch."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.warn_calls: list[tuple[str, str | None]] = []
        self._raises = raises

    async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
        self.warn_calls.append((message, exclude_channel))
        if self._raises is not None:
            raise self._raises
        return True


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _make_slack_adapter(tmp_path: Path) -> SlackAdapter:
    cfg = _make_config(tmp_path)
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    adapter = SlackAdapter(
        config=cfg,
        thread_participation=ThreadParticipation(state_path=state_path),
    )
    adapter._log = _RecordingLogger()
    return adapter


def _make_telegram_adapter(tmp_path: Path) -> TelegramAdapter:
    adapter = TelegramAdapter(config=_make_config(tmp_path))
    adapter._log = _RecordingLogger()
    return adapter


def _read_audit(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    for p in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _token_missing_warnings(log: _RecordingLogger) -> list[dict[str, Any]]:
    return [kw for evt, kw in log.warnings if evt == "channel.token_missing"]


# ---------------------------------------------------------------------------
# Fake slack_bolt (token-present connect path, no network)
# ---------------------------------------------------------------------------


class _FakeAsyncApp:
    def __init__(self, *, token: str) -> None:
        self.token = token
        self.client = SimpleNamespace()

    def event(self, name: str) -> Any:
        def _decorator(fn: Any) -> Any:
            return fn

        return _decorator


def _install_fake_slack_bolt(monkeypatch: pytest.MonkeyPatch, handler_cls: type) -> None:
    mod_root = types.ModuleType("slack_bolt")
    mod_adapter = types.ModuleType("slack_bolt.adapter")
    mod_socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    mod_handler = types.ModuleType("slack_bolt.adapter.socket_mode.async_handler")
    mod_app = types.ModuleType("slack_bolt.async_app")
    mod_handler.AsyncSocketModeHandler = handler_cls  # type: ignore[attr-defined]
    mod_app.AsyncApp = _FakeAsyncApp  # type: ignore[attr-defined]
    mod_root.adapter = mod_adapter  # type: ignore[attr-defined]
    mod_root.async_app = mod_app  # type: ignore[attr-defined]
    mod_adapter.socket_mode = mod_socket_mode  # type: ignore[attr-defined]
    mod_socket_mode.async_handler = mod_handler  # type: ignore[attr-defined]
    for name, mod in {
        "slack_bolt": mod_root,
        "slack_bolt.adapter": mod_adapter,
        "slack_bolt.adapter.socket_mode": mod_socket_mode,
        "slack_bolt.adapter.socket_mode.async_handler": mod_handler,
        "slack_bolt.async_app": mod_app,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


# ---------------------------------------------------------------------------
# Fake telegram.ext (token-present connect path, no network)
# ---------------------------------------------------------------------------


class _FakeFilter:
    """Supports the ``(TEXT | VOICE) & (~COMMAND)`` composition in start()."""

    def __or__(self, other: Any) -> "_FakeFilter":
        return self

    def __and__(self, other: Any) -> "_FakeFilter":
        return self

    def __invert__(self) -> "_FakeFilter":
        return self


class _FakeMessageHandler:
    def __init__(self, filt: Any, callback: Any) -> None:
        self.filter = filt
        self.callback = callback


class _FakeUpdater:
    def __init__(self) -> None:
        self.polling_started = asyncio.Event()

    async def start_polling(self) -> None:
        self.polling_started.set()
        await asyncio.Event().wait()  # park forever, like the real updater

    async def stop(self) -> None: ...


class _FakeTelegramApplication:
    def __init__(self, token: str) -> None:
        self.token = token
        self.handlers: list[Any] = []
        self.updater = _FakeUpdater()
        self.bot = SimpleNamespace()

        async def _get_me() -> Any:
            return SimpleNamespace(username="testbot")

        self.bot.get_me = _get_me

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def shutdown(self) -> None: ...


class _FakeApplicationBuilder:
    built: list[_FakeTelegramApplication] = []

    def token(self, token: str) -> "_FakeApplicationBuilder":
        self._token = token
        return self

    def build(self) -> _FakeTelegramApplication:
        app = _FakeTelegramApplication(self._token)
        type(self).built.append(app)
        return app


def _install_fake_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    mod_root = types.ModuleType("telegram")
    mod_ext = types.ModuleType("telegram.ext")
    mod_ext.ApplicationBuilder = _FakeApplicationBuilder  # type: ignore[attr-defined]
    mod_ext.MessageHandler = _FakeMessageHandler  # type: ignore[attr-defined]
    mod_ext.filters = SimpleNamespace(  # type: ignore[attr-defined]
        TEXT=_FakeFilter(), VOICE=_FakeFilter(), COMMAND=_FakeFilter()
    )
    mod_root.ext = mod_ext  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", mod_root)
    monkeypatch.setitem(sys.modules, "telegram.ext", mod_ext)
    _FakeApplicationBuilder.built = []


# ---------------------------------------------------------------------------
# Slack: token absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_missing_both_tokens_warns_alerts_and_skips_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    adapter = _make_slack_adapter(tmp_path)
    alerter = _StubAlerter()
    adapter.set_alerter(alerter)

    await adapter.start()

    # WARNING names both missing variables exactly.
    warnings = _token_missing_warnings(adapter._log)
    assert len(warnings) == 1
    assert warnings[0]["missing"] == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]
    assert "SLACK_BOT_TOKEN" in warnings[0]["note"]
    assert "SLACK_APP_TOKEN" in warnings[0]["note"]
    assert "skipping connect" in warnings[0]["note"]

    # Admin alert event written to the audit log.
    audits = _read_audit(adapter._config.audit_dir)
    events = [a for a in audits if a["event"] == "audit.transport.token_missing"]
    assert len(events) == 1
    assert events[0]["channel"] == "slack"
    assert events[0]["missing"] == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]
    assert events[0]["level"] == "WARNING"

    # AdminAlerter pinged, excluding the broken channel.
    assert len(alerter.warn_calls) == 1
    message, exclude = alerter.warn_calls[0]
    assert "SLACK_BOT_TOKEN" in message
    assert exclude == "slack"

    # No connect attempted.
    assert adapter._app is None
    assert adapter._handler is None
    assert adapter._handler_task is None


@pytest.mark.asyncio
async def test_slack_missing_app_token_names_only_that_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    adapter = _make_slack_adapter(tmp_path)

    await adapter.start()

    warnings = _token_missing_warnings(adapter._log)
    assert len(warnings) == 1
    assert warnings[0]["missing"] == ["SLACK_APP_TOKEN"]
    assert "SLACK_APP_TOKEN is not set" in warnings[0]["note"]
    assert "SLACK_BOT_TOKEN" not in warnings[0]["note"]
    assert adapter._app is None


@pytest.mark.asyncio
async def test_slack_empty_token_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    adapter = _make_slack_adapter(tmp_path)

    await adapter.start()

    warnings = _token_missing_warnings(adapter._log)
    assert len(warnings) == 1
    assert warnings[0]["missing"] == ["SLACK_BOT_TOKEN"]
    assert adapter._app is None


@pytest.mark.asyncio
async def test_slack_alerter_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    adapter = _make_slack_adapter(tmp_path)
    adapter.set_alerter(_StubAlerter(raises=RuntimeError("alerter down")))

    await adapter.start()  # must not raise

    assert _token_missing_warnings(adapter._log)
    assert any(
        evt == "channel.token_missing_alert_failed" for evt, _ in adapter._log.errors
    )


@pytest.mark.asyncio
async def test_slack_missing_token_without_alerter_still_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    adapter = _make_slack_adapter(tmp_path)  # no set_alerter call

    await adapter.start()  # must not raise

    assert _token_missing_warnings(adapter._log)
    assert adapter._app is None


# ---------------------------------------------------------------------------
# Slack: token present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_tokens_present_no_warning_and_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

    started = asyncio.Event()

    class _FakeSocketModeHandler:
        def __init__(self, app: Any, app_token: str) -> None:
            self.app = app
            self.app_token = app_token

        async def start_async(self) -> None:
            started.set()
            await asyncio.Event().wait()  # park forever, like the real handler

        async def close_async(self) -> None: ...

    _install_fake_slack_bolt(monkeypatch, _FakeSocketModeHandler)
    adapter = _make_slack_adapter(tmp_path)
    alerter = _StubAlerter()
    adapter.set_alerter(alerter)

    await adapter.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)

        assert _token_missing_warnings(adapter._log) == []
        assert alerter.warn_calls == []
        assert adapter._app is not None
        assert adapter._app.token == "xoxb-test"
        assert adapter._handler is not None
        assert adapter._handler.app_token == "xapp-test"
        audits = _read_audit(adapter._config.audit_dir)
        assert not any(
            a["event"] == "audit.transport.token_missing" for a in audits
        )
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# Telegram: token absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_missing_token_warns_alerts_and_skips_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    adapter = _make_telegram_adapter(tmp_path)
    alerter = _StubAlerter()
    adapter.set_alerter(alerter)

    await adapter.start()

    # WARNING names the missing variable exactly.
    warnings = _token_missing_warnings(adapter._log)
    assert len(warnings) == 1
    assert warnings[0]["missing"] == ["TELEGRAM_BOT_TOKEN"]
    assert "TELEGRAM_BOT_TOKEN is not set" in warnings[0]["note"]
    assert "skipping connect" in warnings[0]["note"]

    # Admin alert event written to the audit log.
    audits = _read_audit(adapter._config.audit_dir)
    events = [a for a in audits if a["event"] == "audit.transport.token_missing"]
    assert len(events) == 1
    assert events[0]["channel"] == "telegram"
    assert events[0]["missing"] == ["TELEGRAM_BOT_TOKEN"]
    assert events[0]["level"] == "WARNING"

    # AdminAlerter pinged, excluding the broken channel.
    assert len(alerter.warn_calls) == 1
    message, exclude = alerter.warn_calls[0]
    assert "TELEGRAM_BOT_TOKEN" in message
    assert exclude == "telegram"

    # No connect attempted.
    assert adapter._application is None
    assert adapter._updater_task is None


@pytest.mark.asyncio
async def test_telegram_empty_token_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    adapter = _make_telegram_adapter(tmp_path)

    await adapter.start()

    assert len(_token_missing_warnings(adapter._log)) == 1
    assert adapter._application is None


@pytest.mark.asyncio
async def test_telegram_missing_token_without_alerter_still_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    adapter = _make_telegram_adapter(tmp_path)  # no set_alerter call

    await adapter.start()  # must not raise

    assert _token_missing_warnings(adapter._log)
    assert adapter._application is None


# ---------------------------------------------------------------------------
# Telegram: token present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_token_present_no_warning_and_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:test-token")
    _install_fake_telegram(monkeypatch)
    adapter = _make_telegram_adapter(tmp_path)
    alerter = _StubAlerter()
    adapter.set_alerter(alerter)

    await adapter.start()
    try:
        assert _token_missing_warnings(adapter._log) == []
        assert alerter.warn_calls == []
        assert adapter._application is not None
        assert adapter._application.token == "12345:test-token"
        await asyncio.wait_for(
            adapter._application.updater.polling_started.wait(), timeout=1.0
        )
        audits = _read_audit(adapter._config.audit_dir)
        assert not any(
            a["event"] == "audit.transport.token_missing" for a in audits
        )
    finally:
        await adapter.stop()
