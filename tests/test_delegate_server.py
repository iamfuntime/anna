"""Build-time and call-time tests for the anna_delegate MCP server.

Validates the schema registration, the runner kwarg threading on the
happy path, and the failure-path text response. The runner is faked so
the tests stay in-process and never spin up a real ClaudeSDKClient.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from anna.config import AnnaConfig
from anna.runtime.subagent import DelegateResult, SubAgentError
from anna.tools.delegate_server import DELEGATE_TOOL_NAMES, build_delegate_server


CONV_KEY = "slack:dm:UTEST"


@dataclass
class _FakeRunner:
    """Captures the kwargs passed to delegate; returns / raises on demand."""

    return_result: DelegateResult | None = None
    raise_error: SubAgentError | None = None

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.background_calls: list[dict[str, Any]] = []

    async def delegate(self, **kwargs: Any) -> DelegateResult:
        self.calls.append(kwargs)
        if self.raise_error is not None:
            raise self.raise_error
        assert self.return_result is not None, "test forgot to set return_result"
        return self.return_result

    def start_background(self, **kwargs: Any) -> str:
        self.background_calls.append(kwargs)
        return "job-deadbeef"


def _make_config(tmp_path: Path, *, subagents_enabled: bool = True) -> AnnaConfig:
    cfg = AnnaConfig.model_validate({"subagents": {"enabled": subagents_enabled}})
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    return cfg


# ---------------------------------------------------------------------------
# Build-time
# ---------------------------------------------------------------------------


def test_delegate_tool_names_exposes_single_delegate() -> None:
    assert DELEGATE_TOOL_NAMES == ("delegate",)


def test_build_delegate_server_returns_none_when_disabled(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, subagents_enabled=False)
    runner = _FakeRunner()
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    assert server is None


def test_build_delegate_server_returns_server_when_enabled(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = _FakeRunner()
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    assert server is not None
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"
    assert server.get("name") == "anna_delegate"


def _list_tools(server: dict[str, Any]) -> list[Any]:
    instance = server["instance"]
    handler = instance.request_handlers[ListToolsRequest]
    request = ListToolsRequest(method="tools/list")
    result = asyncio.run(handler(request))
    return list(result.root.tools)


def test_delegate_tool_schema_has_all_fields(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = _FakeRunner()
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    tools = _list_tools(server)
    assert len(tools) == 1
    tool_def = tools[0]
    assert tool_def.name == "delegate"
    props = tool_def.inputSchema["properties"]
    assert set(props.keys()) == {
        "agent_slug",
        "task",
        "context_json",
        "timeout_seconds",
        "background",
    }
    assert props["agent_slug"]["type"] == "string"
    assert props["task"]["type"] == "string"
    assert props["context_json"]["type"] == "string"
    assert props["timeout_seconds"]["type"] == "integer"
    assert props["background"]["type"] == "boolean"


# ---------------------------------------------------------------------------
# Call-time
# ---------------------------------------------------------------------------


def _call_tool(server: dict[str, Any], arguments: dict[str, Any]) -> Any:
    instance = server["instance"]
    handler = instance.request_handlers[CallToolRequest]
    # The MCP SDK marks every declared schema field required, so callers
    # must always supply ``background``. Default it to False here so the
    # existing sync-path tests stay terse; tests that exercise the
    # background path pass it explicitly.
    args = {"background": False, **arguments}
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="delegate", arguments=args),
    )
    return asyncio.run(handler(request))


def _result_text(call_result: Any) -> str:
    """Pull the concatenated text out of the SDK's tool result envelope."""
    # The handler wraps our dict in a ServerResult; the content list is on
    # the inner CallToolResult.
    inner = call_result.root if hasattr(call_result, "root") else call_result
    chunks: list[str] = []
    for block in inner.content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _ok_result(tmp_path: Path) -> DelegateResult:
    return DelegateResult(
        text="threat brief body",
        transcript_path=tmp_path / "transcripts" / "subagent" / "tr" / "today.jsonl",
        tool_calls=["mcp__anna_web__web_search", "Read"],
        cost_usd=0.0023,
        duration_ms=12400,
        status="ok",
    )


def test_delegate_call_threads_kwargs_into_runner(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(return_result=_ok_result(tmp_path))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    _call_tool(
        server,
        {
            "agent_slug": "threat-researcher",
            "task": "dig into CVE-2026-0001",
            "context_json": '{"cve_id": "CVE-2026-0001"}',
            "timeout_seconds": 90,
        },
    )
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["agent_slug"] == "threat-researcher"
    assert call["task"] == "dig into CVE-2026-0001"
    assert call["parent_conv_key"] == CONV_KEY
    assert call["context"] == {"cve_id": "CVE-2026-0001"}
    assert call["timeout_seconds"] == 90


def test_delegate_call_empty_context_json_passes_none(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(return_result=_ok_result(tmp_path))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    _call_tool(
        server,
        {
            "agent_slug": "slug",
            "task": "t",
            "context_json": "",
            "timeout_seconds": 0,
        },
    )
    assert runner.calls[0]["context"] is None
    # timeout_seconds=0 sentinel → None so the runner uses its default.
    assert runner.calls[0]["timeout_seconds"] is None


def test_delegate_call_invalid_context_json_returns_text_error(tmp_path: Path) -> None:
    """Bad JSON does NOT raise; it surfaces as a text response."""
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(return_result=_ok_result(tmp_path))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    call_result = _call_tool(
        server,
        {
            "agent_slug": "slug",
            "task": "t",
            "context_json": "{not valid json",
            "timeout_seconds": 0,
        },
    )
    body = _result_text(call_result)
    assert "delegation failed" in body
    assert "invalid context_json" in body
    # Runner was not called.
    assert runner.calls == []


def test_delegate_call_non_object_context_json_returns_text_error(
    tmp_path: Path,
) -> None:
    """A JSON array is valid JSON but not a dict; surface as a text error."""
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(return_result=_ok_result(tmp_path))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    call_result = _call_tool(
        server,
        {
            "agent_slug": "slug",
            "task": "t",
            "context_json": "[1, 2, 3]",
            "timeout_seconds": 0,
        },
    )
    body = _result_text(call_result)
    assert "delegation failed" in body
    assert "must decode to a JSON object" in body
    assert runner.calls == []


def test_delegate_call_happy_path_returns_text_plus_yaml_trailer(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    result = _ok_result(tmp_path)
    runner = _FakeRunner(return_result=result)
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    call_result = _call_tool(
        server,
        {
            "agent_slug": "tr",
            "task": "t",
            "context_json": "",
            "timeout_seconds": 0,
        },
    )
    body = _result_text(call_result)
    # Sub-agent's reply text first.
    assert body.startswith("threat brief body")
    # YAML trailer.
    assert "\n---\n" in body
    assert "delegation:" in body
    assert "duration_ms: 12400" in body
    assert "status: ok" in body
    assert "cost_usd: 0.0023" in body
    assert "mcp__anna_web__web_search" in body
    assert "Read" in body
    assert str(result.transcript_path) in body


def test_delegate_call_subagent_error_returns_text_response(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(
        raise_error=SubAgentError("timeout", reason="exceeded 300s"),
    )
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    call_result = _call_tool(
        server,
        {
            "agent_slug": "tr",
            "task": "t",
            "context_json": "",
            "timeout_seconds": 0,
        },
    )
    body = _result_text(call_result)
    assert "delegation failed" in body
    assert "timeout" in body
    assert "exceeded 300s" in body


def test_delegate_call_not_found_error_returns_text_response(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(raise_error=SubAgentError("not_found"))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    call_result = _call_tool(
        server,
        {
            "agent_slug": "ghost",
            "task": "t",
            "context_json": "",
            "timeout_seconds": 0,
        },
    )
    body = _result_text(call_result)
    assert "delegation failed" in body
    assert "not_found" in body


def test_delegate_background_returns_job_id_without_blocking(
    tmp_path: Path,
) -> None:
    """background=True returns a job id immediately and never awaits delegate."""
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(return_result=_ok_result(tmp_path))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
        conv_transport="telegram",
    )
    call_result = _call_tool(
        server,
        {
            "agent_slug": "threat-researcher",
            "task": "dig into CVE-2026-0001",
            "context_json": '{"cve_id": "CVE-2026-0001"}',
            "timeout_seconds": 90,
            "background": True,
        },
    )
    body = _result_text(call_result)
    # The synchronous delegate path was NOT taken.
    assert runner.calls == []
    # start_background was invoked with the threaded kwargs.
    assert len(runner.background_calls) == 1
    bg = runner.background_calls[0]
    assert bg["agent_slug"] == "threat-researcher"
    assert bg["task"] == "dig into CVE-2026-0001"
    assert bg["parent_conv_key"] == CONV_KEY
    assert bg["parent_transport"] == "telegram"
    assert bg["context"] == {"cve_id": "CVE-2026-0001"}
    assert bg["timeout_seconds"] == 90
    # The job id is surfaced to ANNA right away.
    assert "job-deadbeef" in body
    assert "Background delegation started" in body


def test_delegate_call_empty_dict_context_json_normalizes_to_none(
    tmp_path: Path,
) -> None:
    """``{}`` is a valid object but carries no fields; treat as no context."""
    cfg = _make_config(tmp_path)
    runner = _FakeRunner(return_result=_ok_result(tmp_path))
    server = build_delegate_server(
        runner=runner,  # type: ignore[arg-type]
        conv_key=CONV_KEY,
        config=cfg,
    )
    _call_tool(
        server,
        {
            "agent_slug": "slug",
            "task": "t",
            "context_json": "{}",
            "timeout_seconds": 0,
        },
    )
    assert runner.calls[0]["context"] is None
