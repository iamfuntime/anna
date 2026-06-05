"""Validate ConversationWorker._build_options() wires up the expected tools.

The worker spawns a real ClaudeSDKClient only when ``start()`` runs. The
``_build_options`` method is the seam: it builds the ClaudeAgentOptions
object without side effects, so the unit test can inspect every field the
worker requests from the SDK.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anna.agents.registry import SubAgentRegistry
from anna.config import AnnaConfig, GoogleAccountConfig, McpServerSpec
from anna.runtime.subagent import SubAgentRunner
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import (
    _DEFAULT_FS_TOOLS,
    _DELEGATE_PREFIX,
    _GOOGLE_PREFIX,
    _SELF_EDIT_PREFIX,
    _SLACK_ALERTS_PREFIX,
    _WEB_PREFIX,
    ConversationWorker,
)
from anna.skills.registry import SkillRegistry
from anna.tools.delegate_server import DELEGATE_TOOL_NAMES
from anna.tools.google_server import GOOGLE_TOOL_NAMES
from anna.tools.self_edit_server import SELF_EDIT_TOOL_NAMES
from anna.tools.slack_alerts_server import SLACK_ALERTS_TOOL_NAMES
from anna.tools.web_server import WEB_TOOL_NAMES


CONV_KEY = "slack:dm:UTEST"


def _make_worker(
    tmp_path: Path,
    *,
    with_google: bool = False,
    with_subagent_runner: bool = False,
    subagents_enabled: bool = True,
    adapters: dict | None = None,
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.subagents.enabled = subagents_enabled
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

    subagent_runner = None
    if with_subagent_runner:
        agents_registry = SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=cfg.anna_home / "agents",
            audit_dir=cfg.audit_dir,
            fsync_on_write=False,
        )
        skills_registry = SkillRegistry(
            supervisor=supervisor,
            skills_dir=cfg.anna_home / "skills",
            audit_dir=cfg.audit_dir,
            fsync_on_write=False,
        )
        subagent_runner = SubAgentRunner(
            config=cfg,
            supervisor=supervisor,
            agents_registry=agents_registry,
            skills_registry=skills_registry,
        )

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
        adapters=adapters,
        google_clients=google_clients,
        subagent_runner=subagent_runner,
    )


def test_build_options_includes_default_fs_self_edit_and_web_tools(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()

    # tools.enabled defaults to true so anna_web mounts by default; google
    # stays off because no GoogleClients was passed.
    expected = (
        list(_DEFAULT_FS_TOOLS)
        + [f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES]
        + [f"{_SLACK_ALERTS_PREFIX}{name}" for name in SLACK_ALERTS_TOOL_NAMES]
        + [f"{_WEB_PREFIX}{name}" for name in WEB_TOOL_NAMES]
    )
    assert sorted(options.allowed_tools) == sorted(expected)
    assert "anna_google" not in options.mcp_servers
    assert "anna_web" in options.mcp_servers


def test_build_options_mounts_google_server_when_enabled(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path, with_google=True)
    options = worker._build_options()
    expected = (
        list(_DEFAULT_FS_TOOLS)
        + [f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES]
        + [f"{_SLACK_ALERTS_PREFIX}{name}" for name in SLACK_ALERTS_TOOL_NAMES]
        + [f"{_GOOGLE_PREFIX}{name}" for name in GOOGLE_TOOL_NAMES]
        + [f"{_WEB_PREFIX}{name}" for name in WEB_TOOL_NAMES]
    )
    assert sorted(options.allowed_tools) == sorted(expected)
    assert "anna_google" in options.mcp_servers
    google_server = options.mcp_servers["anna_google"]
    assert isinstance(google_server, dict)
    assert google_server.get("type") == "sdk"


def test_build_options_skips_web_server_when_tools_disabled(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._config.tools.enabled = False
    options = worker._build_options()
    assert "anna_web" not in options.mcp_servers
    web_named = [t for t in options.allowed_tools if t.startswith(_WEB_PREFIX)]
    assert web_named == []


def test_build_options_web_server_is_sdk_shape(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()
    assert "anna_web" in options.mcp_servers
    web_server = options.mcp_servers["anna_web"]
    assert isinstance(web_server, dict)
    assert web_server.get("type") == "sdk"


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


def test_build_options_mounts_slack_alerts_mcp_server(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    options = worker._build_options()
    assert "anna_slack_alerts" in options.mcp_servers
    server = options.mcp_servers["anna_slack_alerts"]
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"
    # The slack_post tool is always allowlisted.
    assert "mcp__anna_slack_alerts__slack_post" in options.allowed_tools


def test_build_slack_alert_tools_reaches_live_adapter(tmp_path: Path) -> None:
    """The router→worker→tool handoff: SlackAlertTools built by the worker must
    see the same live adapter the worker was constructed with, so slack_post
    posts through ANNA's own Slack connection rather than an empty map."""

    class _StubSlackAdapter:
        async def start(self): ...
        async def stop(self): ...
        async def send(self, message): ...
        def subscribe(self, handler): ...
        async def health_check(self) -> bool:
            return True

        @classmethod
        def conversation_key_for(cls, event):
            return ""

    stub = _StubSlackAdapter()
    worker = _make_worker(tmp_path, adapters={"slack": stub})

    tools = worker._build_slack_alert_tools()
    # The tool bundle holds the worker's live adapter map, and "slack" resolves
    # to the exact adapter instance — not a copy or an empty fallback.
    assert tools.adapters is worker._adapters
    assert tools.adapters.get("slack") is stub


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


# ---------------------------------------------------------------------------
# Phase 2 §3 — anna_delegate wiring (subtask 10)
# ---------------------------------------------------------------------------


def test_build_options_mounts_delegate_when_enabled_and_runner_provided(
    tmp_path: Path,
) -> None:
    """anna_delegate mounts when subagents.enabled=True AND a runner is passed."""
    worker = _make_worker(tmp_path, with_subagent_runner=True)
    options = worker._build_options()
    assert "anna_delegate" in options.mcp_servers
    server = options.mcp_servers["anna_delegate"]
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"


def test_build_options_skips_delegate_when_subagents_disabled(tmp_path: Path) -> None:
    """anna_delegate stays unmounted when subagents.enabled=False."""
    worker = _make_worker(
        tmp_path,
        with_subagent_runner=True,
        subagents_enabled=False,
    )
    options = worker._build_options()
    assert "anna_delegate" not in options.mcp_servers
    delegate_named = [t for t in options.allowed_tools if t.startswith(_DELEGATE_PREFIX)]
    assert delegate_named == []


def test_build_options_skips_delegate_when_no_runner_provided(tmp_path: Path) -> None:
    """anna_delegate stays unmounted when no runner is supplied to the worker."""
    worker = _make_worker(tmp_path)  # no with_subagent_runner=True
    options = worker._build_options()
    assert "anna_delegate" not in options.mcp_servers
    delegate_named = [t for t in options.allowed_tools if t.startswith(_DELEGATE_PREFIX)]
    assert delegate_named == []


def test_build_options_allowed_tools_includes_delegate_when_enabled(
    tmp_path: Path,
) -> None:
    """allowed_tools picks up mcp__anna_delegate__delegate when wired."""
    worker = _make_worker(tmp_path, with_subagent_runner=True)
    options = worker._build_options()
    expected = [f"{_DELEGATE_PREFIX}{name}" for name in DELEGATE_TOOL_NAMES]
    for name in expected:
        assert name in options.allowed_tools, name
    # Belt-and-suspenders: the canonical name is present.
    assert "mcp__anna_delegate__delegate" in options.allowed_tools


# ---------------------------------------------------------------------------
# CLAUDE_CONFIG_DIR isolation + custom MCP registry allowlist
# ---------------------------------------------------------------------------


def test_build_options_sets_claude_config_dir_env_and_setting_sources(
    tmp_path: Path,
) -> None:
    """env points the spawned CLI at the isolated runtime dir; settings stay off."""
    worker = _make_worker(tmp_path)
    options = worker._build_options()
    assert options.env["CLAUDE_CONFIG_DIR"] == str(
        worker._config.claude_runtime_dir
    )
    assert options.setting_sources == []


def test_build_options_mounts_anna_mcp_servers_from_registry(tmp_path: Path) -> None:
    """An allowlisted stdio registry server mounts on ANNA's main loop."""
    worker = _make_worker(tmp_path)
    worker._config.subagents.mcp_registry["security-detections"] = McpServerSpec(
        kind="stdio",
        command="npx",
        args=["-y", "security-detections-mcp"],
    )
    worker._config.subagents.anna_mcp_servers = ["security-detections"]
    options = worker._build_options()

    assert "security-detections" in options.mcp_servers
    server = options.mcp_servers["security-detections"]
    assert server == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "security-detections-mcp"],
    }
    # The server-namespace wildcard lands in allowed_tools.
    assert "mcp__security-detections__*" in options.allowed_tools


def test_build_options_drops_unknown_anna_mcp_server(tmp_path: Path) -> None:
    """An unknown anna_mcp_servers name is dropped without crashing."""
    worker = _make_worker(tmp_path)
    worker._config.subagents.anna_mcp_servers = ["does-not-exist"]
    options = worker._build_options()
    assert "does-not-exist" not in options.mcp_servers
    # Builtins still mounted; the unknown name contributed nothing.
    assert "anna_self_edit" in options.mcp_servers
    assert not any(
        t.startswith("mcp__does-not-exist__") for t in options.allowed_tools
    )


def test_build_options_custom_server_tool_names_are_deduped(tmp_path: Path) -> None:
    """Listing the same registry server twice yields no duplicate tool names."""
    worker = _make_worker(tmp_path)
    worker._config.subagents.mcp_registry["security-detections"] = McpServerSpec(
        kind="stdio",
        command="npx",
        args=["-y", "security-detections-mcp"],
    )
    # Same name twice: the spec resolves twice, so build_mcp_servers emits the
    # server-namespace wildcard twice. The merge must first-seen-dedupe it.
    worker._config.subagents.anna_mcp_servers = [
        "security-detections",
        "security-detections",
    ]
    options = worker._build_options()

    wildcard = "mcp__security-detections__*"
    assert options.allowed_tools.count(wildcard) == 1
    # The server itself is mounted exactly once (dict keys collapse).
    assert "security-detections" in options.mcp_servers


def test_build_options_custom_server_does_not_clobber_builtin(tmp_path: Path) -> None:
    """A registry entry colliding with a builtin name must not overwrite it."""
    worker = _make_worker(tmp_path)
    # Register a stdio server under a builtin's key.
    worker._config.subagents.mcp_registry["anna_web"] = McpServerSpec(
        kind="stdio",
        command="npx",
        args=["-y", "rogue-server"],
    )
    worker._config.subagents.anna_mcp_servers = ["anna_web"]
    options = worker._build_options()

    # The builtin anna_web (sdk shape) wins; the rogue stdio spec is dropped.
    server = options.mcp_servers["anna_web"]
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"
    # The colliding registry server contributed no wildcard tool name.
    assert "mcp__anna_web__*" not in options.allowed_tools


def test_build_options_no_external_servers_by_default(tmp_path: Path) -> None:
    """Empty anna_mcp_servers => only builtins mount, no external servers."""
    worker = _make_worker(tmp_path)
    assert worker._config.subagents.anna_mcp_servers == []
    options = worker._build_options()
    # Builtins present; no stdio/http shapes leaked in.
    assert "anna_self_edit" in options.mcp_servers
    assert "anna_web" in options.mcp_servers
    for server in options.mcp_servers.values():
        assert server.get("type") != "stdio"
        assert server.get("type") != "http"


def test_build_options_env_isolates_config_and_securestorage_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Max mode env relocates CLAUDE_CONFIG_DIR and shares ~/.claude credentials.

    CLAUDE_CONFIG_DIR points at the isolated runtime dir (host discovery off the
    operator's tree); CLAUDE_SECURESTORAGE_CONFIG_DIR points at the operator's
    real ~/.claude so OAuth reads and the refresh-write share the operator's
    .credentials.json. No credentials symlink is involved.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worker = _make_worker(tmp_path)
    assert worker._config.auth.mode == "max"
    options = worker._build_options()

    assert options.env["CLAUDE_CONFIG_DIR"] == str(
        worker._config.claude_runtime_dir
    )
    assert options.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(
        home / ".claude"
    )
    # The securestorage dir is exactly the operator's real ~/.claude.
    assert options.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(
        worker._config.claude_securestorage_dir
    )


def test_build_options_env_omits_securestorage_in_api_key_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_key mode does not set CLAUDE_SECURESTORAGE_CONFIG_DIR.

    The key comes from the inherited env; sharing the operator's credentials
    dir is a max-mode-only behavior (mirroring the old credentials symlink).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worker = _make_worker(tmp_path)
    worker._config.auth.mode = "api_key"
    options = worker._build_options()

    assert options.env["CLAUDE_CONFIG_DIR"] == str(
        worker._config.claude_runtime_dir
    )
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in options.env
