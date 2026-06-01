"""Build-time tests for the anna_web MCP server.

Tool invocation paths are covered in test_web_tools.py and
test_vault_tools.py; this file exercises build_web_server's gating,
schema registration, and the WEB_TOOL_NAMES registry.
"""

from __future__ import annotations

from pathlib import Path

from anna.config import AnnaConfig
from anna.tools.vault_tools import VaultTools
from anna.tools.web_server import WEB_TOOL_NAMES, build_web_server
from anna.tools.web_tools import WebTools


CONV_KEY = "slack:dm:UTEST"


def _make_bundle(tmp_path: Path) -> tuple[AnnaConfig, WebTools, VaultTools]:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    (tmp_path / "anna_home" / "audit").mkdir(parents=True, exist_ok=True)
    return cfg, WebTools(config=cfg), VaultTools(config=cfg)


def test_web_tool_names_match_implementation() -> None:
    assert WEB_TOOL_NAMES == ("web_search", "web_fetch", "vault_download")


def test_build_web_server_returns_server_when_enabled(tmp_path: Path) -> None:
    cfg, web_tools, vault_tools = _make_bundle(tmp_path)
    server = build_web_server(
        config=cfg,
        web_tools=web_tools,
        vault_tools=vault_tools,
        conv_key=CONV_KEY,
    )
    assert server is not None
    # SDK in-process servers are returned as dicts with type='sdk'.
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"


def test_build_web_server_returns_none_when_disabled(tmp_path: Path) -> None:
    cfg, web_tools, vault_tools = _make_bundle(tmp_path)
    cfg.tools.enabled = False
    server = build_web_server(
        config=cfg,
        web_tools=web_tools,
        vault_tools=vault_tools,
        conv_key=CONV_KEY,
    )
    assert server is None
