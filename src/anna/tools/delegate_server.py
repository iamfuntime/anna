"""Phase 2 §3 ``anna_delegate`` MCP server.

Single-tool MCP server that exposes :meth:`SubAgentRunner.delegate` to
ANNA's primary worker. Mounted by ``ConversationWorker._build_options``
alongside ``anna_self_edit``, ``anna_google``, and ``anna_web``.

The runtime-level "one level of delegation" invariant is enforced by
*only* mounting this server on a primary worker's options. Sub-agents
are constructed with options that omit ``anna_delegate`` entirely, so
they cannot call ``delegate`` even if their persona tries.

Gated by ``config.subagents.enabled``. Returns ``None`` from
:func:`build_delegate_server` when disabled so the worker can mount
conditionally with a falsy check, mirroring
:func:`anna.tools.web_server.build_web_server`.

The tool wraps the structured :class:`DelegateResult` into a text
response with a YAML trailer so ANNA can cite the run (transcript path,
cost, duration, tool calls used) when reporting back to the operator.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from anna.config import AnnaConfig
from anna.runtime.subagent import SubAgentError

if TYPE_CHECKING:
    from anna.runtime.subagent import SubAgentRunner


DELEGATE_TOOL_NAMES: tuple[str, ...] = ("delegate",)


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _trailer_yaml(trailer: dict[str, Any]) -> str:
    dumped: str = yaml.safe_dump(
        trailer,
        default_flow_style=False,
        sort_keys=False,
    )
    return dumped.rstrip()


def _format_success(result: Any) -> str:
    """Render a DelegateResult as ``<body>\\n\\n---\\n<yaml trailer>``."""
    trailer = {
        "delegation": {
            "duration_ms": result.duration_ms,
            "status": result.status,
            "cost_usd": result.cost_usd,
            "tool_calls": list(result.tool_calls),
            "transcript": str(result.transcript_path),
        }
    }
    return f"{result.text}\n\n---\n{_trailer_yaml(trailer)}"


def format_background_success(
    *,
    job_id: str,
    agent_slug: str,
    result: Any,
) -> str:
    """Render a finished background delegation for delivery as a new turn.

    Same YAML trailer shape as :func:`_format_success` (so ANNA reads a
    consistent surface whether the run was sync or async), with the
    ``job_id`` and ``agent_slug`` folded into the trailer and a one-line
    header naming the background job so ANNA can tell this inbound turn is
    a delegation completion rather than an operator message.
    """
    trailer = {
        "delegation": {
            "job_id": job_id,
            "agent_slug": agent_slug,
            "duration_ms": result.duration_ms,
            "status": result.status,
            "cost_usd": result.cost_usd,
            "tool_calls": list(result.tool_calls),
            "transcript": str(result.transcript_path),
        }
    }
    header = (
        f"Background delegation {job_id} ({agent_slug}) finished. "
        "Here is the sub-agent's reply:"
    )
    return f"{header}\n\n{result.text}\n\n---\n{_trailer_yaml(trailer)}"


def format_background_failure(
    *,
    job_id: str,
    agent_slug: str,
    kind: str,
    detail: str,
) -> str:
    """Render a failed background delegation for delivery as a new turn."""
    trailer = {
        "delegation": {
            "job_id": job_id,
            "agent_slug": agent_slug,
            "status": kind,
        }
    }
    header = (
        f"Background delegation {job_id} ({agent_slug}) failed: {detail}"
    )
    return f"{header}\n\n---\n{_trailer_yaml(trailer)}"


def build_delegate_server(
    *,
    runner: "SubAgentRunner",
    conv_key: str,
    config: AnnaConfig,
    conv_transport: str = "slack",
) -> Any | None:
    """Construct the per-worker ``anna_delegate`` MCP server.

    Returns ``None`` when ``config.subagents.enabled`` is false so the
    caller can mount conditionally with a falsy check (same idiom as
    :func:`build_web_server`).

    Args:
        runner: Shared :class:`SubAgentRunner` instance constructed at
            ``__main__`` boot. The closure captures it so every
            ``delegate`` call routes through one process-wide semaphore.
        conv_key: The mounting worker's conv_key — captured by the
            closure and threaded into ``runner.delegate`` as
            ``parent_conv_key`` so audit events and transcript lines
            cite the originating conversation.
        config: The active :class:`AnnaConfig`; consulted for the
            enabled flag.
        conv_transport: The mounting worker's transport. Captured by the
            closure and threaded into ``runner.start_background`` so a
            background delegation's completion turn resolves the right
            send adapter even if the originating worker has idled out by
            the time the job finishes.

    Returns:
        An SDK MCP server, or ``None`` when sub-agents are disabled.
    """
    if not config.subagents.enabled:
        return None

    @tool(
        "delegate",
        "Spawn a one-shot sub-agent to handle a focused task. agent_slug "
        "selects the persona at ~/anna/agents/<slug>.md. task is the "
        "free-text instruction the sub-agent will work on. context_json "
        "is an optional JSON-encoded dict of structured context (pass "
        "the empty string when no context). timeout_seconds is the "
        "wall-clock cap (0 to use the default). Set background to true to "
        "fire the sub-agent detached: the tool returns a job id "
        "immediately without blocking, and when the sub-agent finishes "
        "its reply is delivered back to you as a new message. Leave "
        "background false (the default) to wait synchronously for the "
        "reply. Returns the sub-agent's reply (sync) or a job id "
        "(background), with a YAML trailer citing transcript path, "
        "duration, cost, and tool calls.",
        {
            "agent_slug": str,
            "task": str,
            "context_json": str,
            "timeout_seconds": int,
            "background": bool,
        },
    )
    async def _delegate(args: dict[str, Any]) -> dict[str, Any]:
        agent_slug = args["agent_slug"]
        task = args["task"]
        raw_context = args.get("context_json") or ""
        raw_timeout = args.get("timeout_seconds")
        background = bool(args.get("background") or False)

        # Parse the JSON context blob. Empty string → None (no context
        # section in the sub-agent's prompt). Invalid JSON → text error
        # response; we never raise into the SDK because the parent
        # worker must stay alive.
        context: dict[str, Any] | None
        if raw_context.strip():
            try:
                parsed = json.loads(raw_context)
            except json.JSONDecodeError as exc:
                return _text_response(
                    f"delegation failed: invalid context_json: {exc}"
                )
            if not isinstance(parsed, dict):
                return _text_response(
                    "delegation failed: context_json must decode to a JSON object"
                )
            context = parsed if parsed else None
        else:
            context = None

        # 0 (and missing) sentinel → None so the runner falls back to
        # config.subagents.default_timeout_seconds.
        timeout_seconds: int | None
        if raw_timeout in (None, 0):
            timeout_seconds = None
        else:
            timeout_seconds = int(raw_timeout)

        # Background path: fire the delegation detached and return a job id
        # immediately. The runner schedules the run as a tracked task and
        # delivers the completion back into this conversation as a new
        # inbound turn when the sub-agent finishes. ANNA stays responsive
        # in the meantime instead of blocking on the full sub-agent run.
        if background:
            job_id = runner.start_background(
                agent_slug=agent_slug,
                task=task,
                parent_conv_key=conv_key,
                parent_transport=conv_transport,
                context=context,
                timeout_seconds=timeout_seconds,
            )
            return _text_response(
                f"Background delegation started for '{agent_slug}'. "
                f"Job id: {job_id}. The sub-agent's reply will arrive as a "
                "new message when it finishes; you do not need to wait."
            )

        try:
            result = await runner.delegate(
                agent_slug=agent_slug,
                task=task,
                parent_conv_key=conv_key,
                context=context,
                timeout_seconds=timeout_seconds,
            )
        except SubAgentError as exc:
            # Spawn-time and mid-run failures both surface as
            # SubAgentError. Render them as a text response so the
            # parent worker keeps serving its other turns.
            return _text_response(f"delegation failed: {exc.kind}: {exc}")

        return _text_response(_format_success(result))

    return create_sdk_mcp_server(
        name="anna_delegate",
        version="1.0.0",
        tools=[_delegate],
    )


__all__ = [
    "DELEGATE_TOOL_NAMES",
    "build_delegate_server",
    "format_background_failure",
    "format_background_success",
]
