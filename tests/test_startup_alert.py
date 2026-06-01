"""Boot-time clean/unclean sentinel + startup alert dispatch."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.alerter import AdminAlerter
from anna.runtime.startup import (
    build_startup_message,
    read_and_clear_sentinel,
    write_clean_shutdown_sentinel,
)
from anna.transports.base import ChannelAdapter, OutboundMessage


class _StubAdapter(ChannelAdapter):
    def __init__(self, name: str, *, healthy: bool = True) -> None:
        self.name = name
        self.healthy = healthy
        self.sent: list[OutboundMessage] = []

    async def start(self): ...
    async def stop(self): ...
    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def subscribe(self, handler): ...
    async def health_check(self) -> bool:
        return self.healthy

    @classmethod
    def conversation_key_for(cls, event):
        return ""


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.logging.audit.fsync_on_write = False
    cfg.admin.slack_channel_id = "C_ADMIN"
    cfg.admin.telegram_chat_id = "9000"
    return cfg


def _read_audit(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    for p in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Sentinel round-trip
# ---------------------------------------------------------------------------


def test_sentinel_round_trip(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    when = datetime(2026, 6, 1, 12, 34, 56, tzinfo=timezone.utc)
    path = write_clean_shutdown_sentinel(state_dir, now=when)
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip() == "2026-06-01T12:34:56+00:00"

    read = read_and_clear_sentinel(state_dir)
    assert read == when
    # File is consumed.
    assert not path.exists()


def test_sentinel_missing_returns_none(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    assert read_and_clear_sentinel(state_dir) is None


def test_sentinel_unparseable_is_cleared(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = state_dir / "last_clean_shutdown"
    path.write_text("not a timestamp\n", encoding="utf-8")
    assert read_and_clear_sentinel(state_dir) is None
    # Even unparseable content gets cleared so the next boot does not see
    # stale state.
    assert not path.exists()


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------


def test_build_message_clean() -> None:
    boot = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    last = datetime(2026, 6, 1, 8, 59, tzinfo=timezone.utc)
    msg = build_startup_message(last_clean_shutdown=last, boot_time=boot, pid=12345)
    assert "ANNA started at 2026-06-01T09:00:00+00:00" in msg
    assert "PID 12345" in msg
    assert "Last clean shutdown: 2026-06-01T08:59:00+00:00" in msg
    assert "UNCLEAN" not in msg


def test_build_message_unclean() -> None:
    boot = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    msg = build_startup_message(last_clean_shutdown=None, boot_time=boot, pid=12345)
    assert "ANNA started at 2026-06-01T09:00:00+00:00" in msg
    assert "PID 12345" in msg
    assert "UNCLEAN" in msg


# ---------------------------------------------------------------------------
# Alerter notify_startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_startup_dispatches_and_audits(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack})

    ok = await alerter.notify_startup("ANNA started at T.")
    assert ok is True
    assert len(slack.sent) == 1
    assert slack.sent[0].text == "ANNA started at T."

    audits = _read_audit(cfg.audit_dir)
    matching = [a for a in audits if a["event"] == "audit.alerter.dispatched"]
    assert matching, "expected an audit.alerter.dispatched row"
    assert matching[0]["level"] == "STARTUP"


@pytest.mark.asyncio
async def test_notify_startup_falls_through_to_telegram(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=False)
    tg = _StubAdapter("telegram", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack, "telegram": tg})

    ok = await alerter.notify_startup("hello")
    assert ok is True
    assert slack.sent == []
    assert tg.sent[0].text == "hello"


@pytest.mark.asyncio
async def test_notify_startup_returns_false_with_no_destination(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.admin.slack_channel_id = ""
    cfg.admin.telegram_chat_id = ""
    slack = _StubAdapter("slack", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack})
    ok = await alerter.notify_startup("hello")
    assert ok is False
    assert slack.sent == []
