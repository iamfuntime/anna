"""Validate ConversationWorker._build_options() wires up the expected tools.

The worker spawns a real ClaudeSDKClient only when ``start()`` runs. The
``_build_options`` method is the seam: it builds the ClaudeAgentOptions
object without side effects, so the unit test can inspect every field the
worker requests from the SDK.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import (
    _DEFAULT_FS_TOOLS,
    _GOOGLE_PREFIX,
    _SELF_EDIT_PREFIX,
    ConversationWorker,
)
from anna.tools.google_server import GOOGLE_TOOL_NAMES
from anna.tools.self_edit_server import SELF_EDIT_TOOL_NAMES


CONV_KEY = "slack:dm:UTEST"


def _make_worker(tmp_path: Path, *, with_google: bool = False) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    supervisor = Supervisor(config=cfg)

    async def _noop_send(_msg):
        return None

    google_clients = None
    if with_google:
        cfg.google.enabled = True
        cfg.google.accounts.append(
            GoogleAccountConfig(
                slug="personal_main",
                email="x@y.com",
                auth_type="oauth",
                credentials_file="state/google/oauth_client.json",
            )
        )
        # Use the real GoogleClients; nothing in _build_options touches it
        # until the SDK actually invokes a tool, so we don't need a fake.
        from anna.tools.google_clients import GoogleClients
        google_clients = GoogleClients(config=cfg)

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
        google_clients=google_clients,
    )


def test_build_options_includes_default_fs_and_self_edit_tools(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()

    expected = list(_DEFAULT_FS_TOOLS) + [
        f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES
    ]
    assert sorted(options.allowed_tools) == sorted(expected)
    # Without google_clients the google server must not be mounted.
    assert "anna_google" not in options.mcp_servers


def test_build_options_mounts_google_server_when_enabled(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path, with_google=True)
    options = worker._build_options()
    expected = (
        list(_DEFAULT_FS_TOOLS)
        + [f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES]
        + [f"{_GOOGLE_PREFIX}{name}" for name in GOOGLE_TOOL_NAMES]
    )
    assert sorted(options.allowed_tools) == sorted(expected)
    assert "anna_google" in options.mcp_servers
    google_server = options.mcp_servers["anna_google"]
    assert isinstance(google_server, dict)
    assert google_server.get("type") == "sdk"


def test_build_options_skips_google_when_enabled_false_but_clients_passed(tmp_path: Path) -> None:
    """An operator may toggle google.enabled off without scrubbing the clients."""
    worker = _make_worker(tmp_path, with_google=True)
    # Now flip the config off after construction.
    worker._config.google.enabled = False
    options = worker._build_options()
    assert "anna_google" not in options.mcp_servers
    google_named = [t for t in options.allowed_tools if t.startswith(_GOOGLE_PREFIX)]
    assert google_named == []


def test_build_options_mounts_self_edit_mcp_server(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()
    assert "anna_self_edit" in options.mcp_servers
    server = options.mcp_servers["anna_self_edit"]
    # The SDK returns a dict-shaped McpSdkServerConfig for in-process servers.
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"


def test_build_options_sets_cwd_to_vault_and_add_dirs_to_core(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()

    vault_root = worker._config.vault.resolved_path
    assert str(options.cwd) == str(vault_root)

    core_dir = worker._config.anna_home / "core"
    assert str(core_dir) in [str(p) for p in options.add_dirs]


def test_build_options_isolates_settings_and_permission_mode(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()

    # setting_sources must be the empty list (NOT None) so the SDK does not
    # inherit user/project/local settings.
    assert options.setting_sources == []
    # bypassPermissions is the project default — the runtime cannot prompt.
    assert options.permission_mode == "bypassPermissions"


def test_build_options_system_prompt_contains_scope_and_runtime(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()
    prompt = options.system_prompt
    assert isinstance(prompt, str)
    assert "You are ANNA" in prompt
    assert "Runtime paths" in prompt
    assert "Core identity files" in prompt


def test_build_options_creates_vault_root(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path
    assert not vault_root.exists()
    worker._build_options()
    assert vault_root.is_dir()
