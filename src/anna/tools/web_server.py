"""Phase 2 §2 web + vault tool MCP server.

Mounted alongside ``anna_self_edit`` and ``anna_google`` by each
conversation worker. Three tools:

* ``web_search`` — Brave Search REST.
* ``web_fetch`` — httpx GET + HTML-to-Markdown (Playwright fallback
  hook in place but inert; future slice).
* ``vault_download`` — URL → ``~/Obsidian/ANNA/Inbox`` (configurable).

Gated by ``config.tools.enabled``. Returns ``None`` from
:func:`build_web_server` when disabled so the worker can mount
conditionally with a falsy check.

shell_exec is intentionally not in this server — see the slim §2 plan
at Inbox/2026-06-01-ANNA-Phase-2-Tool-Surface-Slim-Plan.md for the
sequencing rationale.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from anna.config import AnnaConfig
from anna.tools.vault_tools import VaultTools
from anna.tools.web_tools import WebTools


WEB_TOOL_NAMES: tuple[str, ...] = (
    "web_search",
    "web_fetch",
    "vault_download",
)


def build_web_server(
    *,
    config: AnnaConfig,
    web_tools: WebTools,
    vault_tools: VaultTools,
    conv_key: str,
) -> Any:
    """Construct the per-worker anna_web MCP server.

    Returns ``None`` when ``config.tools.enabled`` is false so the caller
    can use a falsy check to decide whether to mount.
    """
    if not config.tools.enabled:
        return None

    @tool(
        "web_search",
        "Search the web via Brave Search. Returns up to max_results hits with url, title, snippet, and a published_at hint when available. Pass max_results (1-50, default from config) to cap the response.",
        {
            "query": str,
            "max_results": int,
        },
    )
    async def _web_search(args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("max_results")
        return await web_tools.web_search(
            query=args["query"],
            max_results=int(raw) if raw is not None else None,
            conv_key=conv_key,
        )

    @tool(
        "web_fetch",
        "Fetch a URL with a browser-shaped User-Agent and return the body as Markdown. HTML is converted; PDF / image / binary content types return a metadata-only response (use vault_download to save the file). used_playwright is hardcoded false in this slice (JS-render fallback is deferred).",
        {
            "url": str,
        },
    )
    async def _web_fetch(args: dict[str, Any]) -> dict[str, Any]:
        return await web_tools.web_fetch(
            url=args["url"],
            conv_key=conv_key,
        )

    @tool(
        "vault_download",
        "Download a URL to ~/Obsidian/ANNA/Inbox (configurable). Filename derived from Content-Disposition, URL basename, or a URL hash; extension picked from Content-Type. Returns the absolute path written. Refuses files larger than tools.vault_download.max_size_bytes (default 50 MB).",
        {
            "url": str,
        },
    )
    async def _vault_download(args: dict[str, Any]) -> dict[str, Any]:
        return await vault_tools.vault_download(
            url=args["url"],
            conv_key=conv_key,
        )

    return create_sdk_mcp_server(
        name="anna_web",
        version="1.0.0",
        tools=[
            _web_search,
            _web_fetch,
            _vault_download,
        ],
    )


__all__ = [
    "WEB_TOOL_NAMES",
    "build_web_server",
]
