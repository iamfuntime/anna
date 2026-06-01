"""Validate AdminAlerter routes operator alerts to the surviving channel."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.alerter import AdminAlerter
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


@pytest.mark.asyncio
async def test_warn_picks_first_healthy_adapter(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=True)
    tg = _StubAdapter("telegram", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack, "telegram": tg})

    ok = await alerter.warn("hello")
    assert ok is True
    # Slack is first in candidate order; it took the message.
    assert len(slack.sent) == 1
    assert slack.sent[0].text == "hello"
    assert tg.sent == []

    audits = _read_audit(cfg.audit_dir)
    assert any(a["event"] == "audit.alerter.dispatched" for a in audits)


@pytest.mark.asyncio
async def test_exclude_channel_routes_to_other(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=True)
    tg = _StubAdapter("telegram", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack, "telegram": tg})

    ok = await alerter.warn("slack just restarted", exclude_channel="slack")
    assert ok is True
    assert slack.sent == []
    assert len(tg.sent) == 1
    assert "restarted" in tg.sent[0].text


@pytest.mark.asyncio
async def test_unhealthy_first_falls_through_to_second(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=False)
    tg = _StubAdapter("telegram", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack, "telegram": tg})

    ok = await alerter.warn("notice")
    assert ok is True
    assert slack.sent == []
    assert len(tg.sent) == 1


@pytest.mark.asyncio
async def test_critical_prefixes_message(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack})
    await alerter.critical("SDK auth gone")
    assert slack.sent[0].text.startswith("[CRITICAL] ")


@pytest.mark.asyncio
async def test_returns_false_when_no_destination_configured(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.admin.slack_channel_id = ""
    cfg.admin.telegram_chat_id = ""
    slack = _StubAdapter("slack", healthy=True)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack})
    ok = await alerter.warn("nobody home")
    assert ok is False
    assert slack.sent == []


@pytest.mark.asyncio
async def test_returns_false_when_all_channels_unhealthy(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", healthy=False)
    tg = _StubAdapter("telegram", healthy=False)
    alerter = AdminAlerter(config=cfg, adapters={"slack": slack, "telegram": tg})
    ok = await alerter.warn("ugh")
    assert ok is False
    assert slack.sent == []
    assert tg.sent == []
