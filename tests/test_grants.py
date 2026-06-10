"""Tests for the grant resolver + MCP resolver (subtasks 5 and 6)."""

from __future__ import annotations

import pytest

from anna.config import AgentGrants, AnnaConfig, McpServerSpec
from anna.runtime.grants import (
    ResolvedGrant,
    build_mcp_servers,
    resolve_effective_grant,
)


def _cfg(**subagents_overrides) -> AnnaConfig:
    """An AnnaConfig with the given subagents overrides merged in."""
    base = {"auth": {"mode": "max"}}
    if subagents_overrides:
        base["subagents"] = subagents_overrides
    return AnnaConfig.model_validate(base)


# ---------------------------------------------------------------------------
# resolve_effective_grant — layer 1 fallback
# ---------------------------------------------------------------------------


def test_empty_config_falls_back_to_today_values() -> None:
    """No grants anywhere → fallback equals today's behavior."""
    cfg = _cfg()
    rg = resolve_effective_grant(cfg, "anyone", None)
    assert isinstance(rg, ResolvedGrant)
    # allowed_tools mirrors subagents.allowed_tools.
    assert rg.allowed_tools == list(cfg.subagents.allowed_tools)
    assert rg.permission_mode == "acceptEdits"
    # tools.enabled defaults true → the anna_web builtin is reachable.
    assert [name for name, _ in rg.mcp_specs] == ["anna_web"]
    assert rg.mcp_specs[0][1].kind == "builtin"
    assert rg.mcp_specs[0][1].builtin_name == "anna_web"
    # No extra_dirs configured → no write dirs.
    assert rg.write_dirs == []


def test_fallback_no_web_when_tools_disabled() -> None:
    """tools.enabled false → fallback mounts no MCP server."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}, "tools": {"enabled": False}})
    rg = resolve_effective_grant(cfg, "x", None)
    assert rg.mcp_specs == []


# ---------------------------------------------------------------------------
# name resolution — unknown names dropped + logged
# ---------------------------------------------------------------------------


def test_unknown_dir_pool_name_dropped_and_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write_dirs name not in dir_pool is dropped with a WARNING."""
    cfg = _cfg(
        dir_pool={"reports": "/tmp/reports"},
        agents={"r": {"write_dirs": ["reports", "ghost"]}},
    )
    rg = resolve_effective_grant(cfg, "r", None)
    assert rg.write_dirs == ["/tmp/reports"]
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "grants.dir_pool.unknown" in out
    assert "ghost" in out


def test_unknown_registry_name_dropped_and_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An mcp_servers name not in the registry is dropped with a WARNING."""
    cfg = _cfg(
        mcp_registry={"pw": {"kind": "stdio", "command": "npx"}},
        agents={"r": {"mcp_servers": ["pw", "nope"]}},
    )
    rg = resolve_effective_grant(cfg, "r", None)
    assert [n for n, _ in rg.mcp_specs] == ["pw"]
    out = capsys.readouterr().out
    assert "grants.mcp_registry.unknown" in out
    assert "nope" in out


# ---------------------------------------------------------------------------
# grant resolution from anna.yaml and frontmatter
# ---------------------------------------------------------------------------


def test_yaml_agent_grant_resolves() -> None:
    """A subagents.agents.<slug> grant resolves its names."""
    cfg = _cfg(
        dir_pool={"reports": "/tmp/reports"},
        mcp_registry={"pw": {"kind": "stdio", "command": "npx"}},
        agents={
            "r": {
                "write_dirs": ["reports"],
                "mcp_servers": ["pw"],
                "permission_mode": "bypassPermissions",
                "allowed_tools": ["Read"],
            }
        },
    )
    rg = resolve_effective_grant(cfg, "r", None)
    assert rg.write_dirs == ["/tmp/reports"]
    assert [n for n, _ in rg.mcp_specs] == ["pw"]
    assert rg.permission_mode == "bypassPermissions"
    assert rg.allowed_tools == ["Read"]


def test_frontmatter_grant_resolves() -> None:
    """A frontmatter grant (no yaml agent) resolves against the pools."""
    cfg = _cfg(
        dir_pool={"reports": "/tmp/reports"},
        mcp_registry={"pw": {"kind": "stdio", "command": "npx"}},
    )
    fm = AgentGrants(write_dirs=["reports"], mcp_servers=["pw"])
    rg = resolve_effective_grant(cfg, "r", fm)
    assert rg.write_dirs == ["/tmp/reports"]
    assert [n for n, _ in rg.mcp_specs] == ["pw"]


# ---------------------------------------------------------------------------
# precedence — frontmatter REPLACES yaml REPLACES fallback
# ---------------------------------------------------------------------------


def test_precedence_frontmatter_replaces_yaml_replaces_fallback() -> None:
    """Per-field REPLACE across the three layers, including list-replace."""
    cfg = _cfg(
        extra_dirs=["~/fallback-dir"],
        dir_pool={"a": "/tmp/a", "b": "/tmp/b"},
        mcp_registry={
            "x": {"kind": "stdio", "command": "x"},
            "y": {"kind": "stdio", "command": "y"},
        },
        agents={
            "r": {
                "mcp_servers": ["x"],
                "permission_mode": "default",
            }
        },
    )
    # Fallback supplies allowed_tools + anna_web; yaml replaces mcp + mode;
    # frontmatter replaces write_dirs + mcp again (list-replace).
    fm = AgentGrants(write_dirs=["b"], mcp_servers=["y"])
    rg = resolve_effective_grant(cfg, "r", fm)
    # write_dirs: fallback extra_dirs were overridden by frontmatter ["b"].
    assert rg.write_dirs == ["/tmp/b"]
    # mcp_servers: frontmatter ["y"] replaces yaml ["x"] replaces fallback.
    assert [n for n, _ in rg.mcp_specs] == ["y"]
    # permission_mode: yaml set "default"; frontmatter left it absent → "default".
    assert rg.permission_mode == "default"
    # allowed_tools: only fallback specified it → passes through.
    assert rg.allowed_tools == list(cfg.subagents.allowed_tools)


def test_empty_grant_lists_read_as_passthrough() -> None:
    """An agent grant with empty lists does not clobber the fallback."""
    cfg = _cfg(agents={"r": {"allowed_tools": ["Read"]}})
    rg = resolve_effective_grant(cfg, "r", None)
    # write_dirs/mcp_servers were [] on the grant → fallback anna_web survives.
    assert [n for n, _ in rg.mcp_specs] == ["anna_web"]
    # allowed_tools explicitly set → replaced.
    assert rg.allowed_tools == ["Read"]


# ---------------------------------------------------------------------------
# permission_mode clamp — untrusted frontmatter cannot escalate to bypass
# ---------------------------------------------------------------------------


def test_frontmatter_bypass_permissions_denied_and_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Untrusted frontmatter permission_mode=bypassPermissions is clamped."""
    cfg = _cfg()
    fm = AgentGrants(permission_mode="bypassPermissions")
    rg = resolve_effective_grant(cfg, "r", fm)
    # Clamp → falls back to the layer-1 fallback value (acceptEdits).
    assert rg.permission_mode == "acceptEdits"
    out = capsys.readouterr().out
    assert "grants.permission_mode.frontmatter_escalation_denied" in out
    assert "bypassPermissions" in out


def test_frontmatter_bypass_clamp_falls_back_to_yaml_layer() -> None:
    """When clamped, frontmatter bypass yields the TRUSTED layer-2 mode."""
    cfg = _cfg(agents={"r": {"permission_mode": "plan"}})
    fm = AgentGrants(permission_mode="bypassPermissions")
    rg = resolve_effective_grant(cfg, "r", fm)
    # Frontmatter bypass is dropped → layer-2 yaml "plan" wins, not bypass.
    assert rg.permission_mode == "plan"


def test_frontmatter_does_not_mutate_caller_grant() -> None:
    """The clamp must not mutate the caller's frontmatter_grants object."""
    cfg = _cfg()
    fm = AgentGrants(permission_mode="bypassPermissions")
    resolve_effective_grant(cfg, "r", fm)
    # Caller's object is untouched.
    assert fm.permission_mode == "bypassPermissions"


def test_yaml_agent_bypass_permissions_is_honored() -> None:
    """TRUSTED layer-2 anna.yaml agents.<slug> may set bypassPermissions."""
    cfg = _cfg(agents={"r": {"permission_mode": "bypassPermissions"}})
    rg = resolve_effective_grant(cfg, "r", None)
    assert rg.permission_mode == "bypassPermissions"


def test_frontmatter_plan_passes_through() -> None:
    """A non-bypass frontmatter mode (plan) passes through unclamped."""
    cfg = _cfg()
    fm = AgentGrants(permission_mode="plan")
    rg = resolve_effective_grant(cfg, "r", fm)
    assert rg.permission_mode == "plan"


# ---------------------------------------------------------------------------
# build_mcp_servers — subtask 6
# ---------------------------------------------------------------------------


def test_build_builtin_anna_web_returns_sdk_server() -> None:
    """A resolved anna_web builtin builds an sdk-typed server + tool names."""
    cfg = _cfg()
    spec = McpServerSpec(kind="builtin", builtin_name="anna_web")
    servers, additions = build_mcp_servers(cfg, [("anna_web", spec)], "conv:1")
    assert "anna_web" in servers
    # The create_sdk_mcp_server return is a {"type": "sdk", ...} dict.
    assert servers["anna_web"]["type"] == "sdk"
    assert "mcp__anna_web__web_search" in additions
    assert "mcp__anna_web__web_fetch" in additions
    assert "mcp__anna_web__vault_download" in additions


def test_build_stdio_emits_literal_dict() -> None:
    """A stdio spec emits the exact {'type':'stdio','command':...} dict."""
    cfg = _cfg()
    spec = McpServerSpec(
        kind="stdio", command="npx", args=["-y", "pw"], env={"K": "V"}
    )
    servers, additions = build_mcp_servers(cfg, [("pw", spec)], "conv:1")
    assert servers["pw"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "pw"],
        "env": {"K": "V"},
    }
    # No explicit tool_names → server-namespace wildcard.
    assert additions == ["mcp__pw__*"]


def test_build_stdio_explicit_tool_names_expand() -> None:
    """An stdio spec with tool_names expands to named mcp__<name>__<tool>."""
    cfg = _cfg()
    spec = McpServerSpec(kind="stdio", command="npx", tool_names=["go", "click"])
    _servers, additions = build_mcp_servers(cfg, [("pw", spec)], "conv:1")
    assert additions == ["mcp__pw__go", "mcp__pw__click"]


def test_build_http_emits_literal_dict() -> None:
    """An http spec emits the exact {'type':'http','url':...} dict."""
    cfg = _cfg()
    spec = McpServerSpec(
        kind="http", url="https://srv/mcp", headers={"Authorization": "x"}
    )
    servers, additions = build_mcp_servers(cfg, [("remote", spec)], "conv:1")
    assert servers["remote"] == {
        "type": "http",
        "url": "https://srv/mcp",
        "headers": {"Authorization": "x"},
    }
    assert additions == ["mcp__remote__*"]


def test_forbidden_builtin_in_registry_is_dropped_and_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A registry entry naming a forbidden builtin is dropped at build time."""
    cfg = _cfg()
    spec = McpServerSpec(kind="builtin", builtin_name="anna_self_edit")
    servers, additions = build_mcp_servers(cfg, [("evil", spec)], "conv:1")
    assert servers == {}
    assert additions == []
    out = capsys.readouterr().out
    assert "grants.builtin.forbidden" in out
    assert "anna_self_edit" in out


@pytest.mark.parametrize("forbidden", ["anna_google", "anna_delegate"])
def test_each_forbidden_builtin_is_structurally_unreachable(
    forbidden: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """anna_google and anna_delegate are also dropped."""
    cfg = _cfg()
    spec = McpServerSpec(kind="builtin", builtin_name=forbidden)
    servers, _additions = build_mcp_servers(cfg, [("x", spec)], "conv:1")
    assert servers == {}
    assert "grants.builtin.forbidden" in capsys.readouterr().out


def test_builtin_dropped_when_tools_disabled() -> None:
    """anna_web builtin yields nothing when tools.enabled is false."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}, "tools": {"enabled": False}})
    spec = McpServerSpec(kind="builtin", builtin_name="anna_web")
    servers, additions = build_mcp_servers(cfg, [("anna_web", spec)], "conv:1")
    assert servers == {}
    assert additions == []


# ---------------------------------------------------------------------------
# model resolution — global default vs per-agent vs frontmatter (most wins)
# ---------------------------------------------------------------------------


def test_model_defaults_to_none_when_unset_everywhere() -> None:
    """No model anywhere → resolved model is None (inherit CLI default)."""
    cfg = _cfg()
    rg = resolve_effective_grant(cfg, "r", None)
    assert rg.model is None


def test_model_global_default_inherited_by_overrideless_agent() -> None:
    """runtime.model is the fallback layer → every override-less agent gets it."""
    cfg = AnnaConfig.model_validate({"runtime": {"model": "opus"}})
    rg = resolve_effective_grant(cfg, "anyone", None)
    assert rg.model == "opus"


def test_model_per_agent_overrides_global_default() -> None:
    """subagents.agents.<slug>.model replaces runtime.model for that slug."""
    cfg = AnnaConfig.model_validate(
        {
            "runtime": {"model": "opus"},
            "subagents": {"agents": {"r": {"model": "sonnet"}}},
        }
    )
    rg = resolve_effective_grant(cfg, "r", None)
    assert rg.model == "sonnet"
    # A different slug with no override still inherits the global default.
    assert resolve_effective_grant(cfg, "other", None).model == "opus"


def test_model_frontmatter_overrides_per_agent() -> None:
    """Frontmatter grants.model is most-specific and wins over the rest."""
    cfg = AnnaConfig.model_validate(
        {
            "runtime": {"model": "opus"},
            "subagents": {"agents": {"r": {"model": "sonnet"}}},
        }
    )
    fm = AgentGrants(model="haiku")
    rg = resolve_effective_grant(cfg, "r", fm)
    assert rg.model == "haiku"


def test_model_frontmatter_absent_passes_per_agent_through() -> None:
    """Frontmatter with no model leaves the per-agent override in place."""
    cfg = AnnaConfig.model_validate(
        {"subagents": {"agents": {"r": {"model": "sonnet"}}}}
    )
    fm = AgentGrants(write_dirs=[])  # no model field
    rg = resolve_effective_grant(cfg, "r", fm)
    assert rg.model == "sonnet"


def test_model_is_not_clamped_for_untrusted_frontmatter() -> None:
    """Unlike permission_mode, a frontmatter model is free-form (no clamp)."""
    cfg = _cfg()
    fm = AgentGrants(model="opus")
    rg = resolve_effective_grant(cfg, "r", fm)
    assert rg.model == "opus"
