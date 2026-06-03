"""Validate the slack-alerts MCP server's slack_post tool.

The tests bypass the MCP transport layer and call ``SlackAlertTools.slack_post``
directly (the build-path is smoke-tested at the bottom). A stub adapter
captures the OutboundMessage so we can assert on the conversation_key shape
and text without a live Slack connection. The stub mirrors the one in
test_alerter.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.tools.slack_alerts_server import (
    SLACK_ALERTS_TOOL_NAMES,
    SlackAlertTools,
    build_slack_alerts_server,
)
from anna.transports.base import ChannelAdapter, OutboundMessage


CONV_KEY = "slack:dm:UTEST"


class _StubAdapter(ChannelAdapter):
    def __init__(
        self, name: str, *, healthy: bool = True, raise_on_send: bool = False
    ) -> None:
        self.name = name
        self.healthy = healthy
        self.raise_on_send = raise_on_send
        self.sent: list[OutboundMessage] = []

    async def start(self): ...
    async def stop(self): ...
    async def send(self, message: OutboundMessage) -> None:
        if self.raise_on_send:
            # Mirror SlackAdapter.send re-raising on a Slack API failure.
            raise RuntimeError("slack api: channel_not_found")
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
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _read_audit_records(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@pytest.mark.asyncio
async def test_slack_post_explicit_channel_sends_message(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})

    result = await tools.slack_post(
        text="hello world",
        channel_id="C123EXPLICIT",
        conv_key=CONV_KEY,
    )
    assert "C123EXPLICIT" in result["content"][0]["text"]

    assert len(slack.sent) == 1
    msg = slack.sent[0]
    assert msg.conversation_key == "slack:dm:C123EXPLICIT"
    assert msg.text == "hello world"


@pytest.mark.asyncio
async def test_slack_post_falls_back_to_reports_channel(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.reports.slack_channel_id = "C_REPORTS"
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})

    result = await tools.slack_post(text="digest card", channel_id="", conv_key=CONV_KEY)
    assert "C_REPORTS" in result["content"][0]["text"]

    assert len(slack.sent) == 1
    assert slack.sent[0].conversation_key == "slack:dm:C_REPORTS"
    assert slack.sent[0].text == "digest card"


@pytest.mark.asyncio
async def test_slack_post_no_channel_anywhere_errors_and_sends_nothing(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    # reports.slack_channel_id defaults to "".
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})

    result = await tools.slack_post(text="nowhere to go", channel_id="", conv_key=CONV_KEY)
    text = result["content"][0]["text"]
    assert "failed" in text.lower()
    assert "channel" in text.lower()
    assert slack.sent == []

    # No dispatch audit event when nothing was sent.
    audits = _read_audit_records(cfg.audit_dir)
    assert not any(a.get("event") == "audit.slack_post.dispatched" for a in audits)


@pytest.mark.asyncio
async def test_slack_post_missing_slack_adapter_errors(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.reports.slack_channel_id = "C_REPORTS"
    # No "slack" key in the adapters map (e.g. only telegram connected).
    tg = _StubAdapter("telegram")
    tools = SlackAlertTools(cfg, {"telegram": tg})

    result = await tools.slack_post(text="ping", channel_id="C123", conv_key=CONV_KEY)
    text = result["content"][0]["text"]
    assert "failed" in text.lower()
    assert "not connected" in text.lower()
    assert tg.sent == []


@pytest.mark.asyncio
async def test_slack_post_emits_audit_event(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})

    await tools.slack_post(text="audit me", channel_id="C_AUD", conv_key=CONV_KEY)

    audits = _read_audit_records(cfg.audit_dir)
    dispatched = [a for a in audits if a.get("event") == "audit.slack_post.dispatched"]
    assert dispatched
    assert dispatched[0]["channel"] == "C_AUD"
    assert dispatched[0]["text_len"] == len("audit me")
    assert dispatched[0]["conv_key"] == CONV_KEY


@pytest.mark.asyncio
async def test_slack_post_send_failure_returns_error_and_audits(
    tmp_path: Path,
) -> None:
    """A re-raising adapter must not crash the turn: slack_post returns an
    error text response and records an ``audit.slack_post.failed`` event."""
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack", raise_on_send=True)
    tools = SlackAlertTools(cfg, {"slack": slack})

    # Does not raise out of the handler.
    result = await tools.slack_post(
        text="will fail", channel_id="C_FAIL", conv_key=CONV_KEY
    )
    text = result["content"][0]["text"]
    assert "failed" in text.lower()
    assert "C_FAIL" in text

    audits = _read_audit_records(cfg.audit_dir)
    failed = [a for a in audits if a.get("event") == "audit.slack_post.failed"]
    assert failed
    assert failed[0]["channel"] == "C_FAIL"
    assert failed[0]["error"]
    assert failed[0]["conv_key"] == CONV_KEY
    # The failure path must NOT also emit a dispatched event.
    assert not any(a.get("event") == "audit.slack_post.dispatched" for a in audits)


@pytest.mark.asyncio
async def test_slack_post_empty_text_errors_and_sends_nothing(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    cfg.reports.slack_channel_id = "C_REPORTS"
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})

    result = await tools.slack_post(
        text="   \n\t ", channel_id="C123", conv_key=CONV_KEY
    )
    text = result["content"][0]["text"]
    assert "failed" in text.lower()
    assert "empty" in text.lower()
    assert slack.sent == []

    audits = _read_audit_records(cfg.audit_dir)
    assert not any(a.get("event") == "audit.slack_post.dispatched" for a in audits)


@pytest.mark.asyncio
async def test_slack_post_admin_channel_refused_and_sends_nothing(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    cfg.admin.slack_channel_id = "C_ADMIN"
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})

    result = await tools.slack_post(
        text="sneaky alert", channel_id="C_ADMIN", conv_key=CONV_KEY
    )
    text = result["content"][0]["text"]
    assert "failed" in text.lower()
    assert "admin" in text.lower()
    assert slack.sent == []

    audits = _read_audit_records(cfg.audit_dir)
    assert not any(a.get("event") == "audit.slack_post.dispatched" for a in audits)


# ---------------------------------------------------------------------------
# Server build path (smoke)
# ---------------------------------------------------------------------------


def test_build_slack_alerts_server_exposes_tool(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    slack = _StubAdapter("slack")
    tools = SlackAlertTools(cfg, {"slack": slack})
    server = build_slack_alerts_server(tools=tools, conv_key=CONV_KEY)

    assert isinstance(server, dict) and server.get("type") == "sdk"
    assert server.get("name") == "anna_slack_alerts"
    instance = server["instance"]

    import asyncio
    import mcp.types as mcp_types

    async def _list() -> list[str]:
        list_handler = instance.request_handlers.get(mcp_types.ListToolsRequest)
        assert list_handler is not None
        req = mcp_types.ListToolsRequest(method="tools/list")
        result = await list_handler(req)
        tools_payload = result.root if hasattr(result, "root") else result
        return [t.name for t in tools_payload.tools]

    names = asyncio.run(_list())
    for name in SLACK_ALERTS_TOOL_NAMES:
        assert name in names, f"server missing tool {name}; got {names}"
