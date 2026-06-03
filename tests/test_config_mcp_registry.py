"""Tests for the per-agent permission config models (subtask 2).

Covers McpServerSpec per-kind validators, AgentGrants, and the
SubagentsConfig dir_pool / mcp_registry / agents extensions.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anna.config import AgentGrants, AnnaConfig, McpServerSpec


def test_empty_config_still_validates() -> None:
    """Adding the new fields does not break AnnaConfig.model_validate({})."""
    cfg = AnnaConfig.model_validate({})
    assert cfg.subagents.dir_pool == {}
    assert cfg.subagents.mcp_registry == {}
    assert cfg.subagents.agents == {}


def test_builtin_spec_requires_builtin_name() -> None:
    """kind='builtin' without builtin_name is rejected."""
    with pytest.raises(ValidationError):
        McpServerSpec(kind="builtin")


def test_builtin_with_bogus_name_parses() -> None:
    """A builtin with an unknown builtin_name parses (drop is resolution-time)."""
    spec = McpServerSpec(kind="builtin", builtin_name="totally-bogus")
    assert spec.builtin_name == "totally-bogus"


def test_stdio_spec_requires_command() -> None:
    """kind='stdio' without command is rejected."""
    with pytest.raises(ValidationError):
        McpServerSpec(kind="stdio")


def test_http_spec_requires_url() -> None:
    """kind='http' without url is rejected."""
    with pytest.raises(ValidationError):
        McpServerSpec(kind="http")


def test_stdio_with_url_is_rejected() -> None:
    """A cross-kind combo (stdio + url) is rejected."""
    with pytest.raises(ValidationError):
        McpServerSpec(kind="stdio", command="srv", url="https://x")


def test_http_with_command_is_rejected() -> None:
    """A cross-kind combo (http + command) is rejected."""
    with pytest.raises(ValidationError):
        McpServerSpec(kind="http", url="https://x", command="srv")


def test_builtin_with_command_is_rejected() -> None:
    """A builtin must not carry external-transport fields."""
    with pytest.raises(ValidationError):
        McpServerSpec(kind="builtin", builtin_name="anna_web", command="srv")


def test_stdio_spec_round_trips_fields() -> None:
    """A full stdio spec keeps args/env/tool_names."""
    spec = McpServerSpec(
        kind="stdio",
        command="npx",
        args=["-y", "playwright-mcp"],
        env={"FOO": "bar"},
        tool_names=["navigate", "click"],
    )
    assert spec.command == "npx"
    assert spec.args == ["-y", "playwright-mcp"]
    assert spec.env == {"FOO": "bar"}
    assert spec.tool_names == ["navigate", "click"]


def test_agent_grants_defaults() -> None:
    """AgentGrants defaults: empty lists, None scalars."""
    g = AgentGrants()
    assert g.write_dirs == []
    assert g.mcp_servers == []
    assert g.allowed_tools is None
    assert g.permission_mode is None


def test_subagents_registry_parses_nested_specs() -> None:
    """A full subagents block with registry + agents validates end to end."""
    cfg = AnnaConfig.model_validate(
        {
            "subagents": {
                "dir_pool": {"reports": "~/reports"},
                "mcp_registry": {
                    "playwright": {
                        "kind": "stdio",
                        "command": "npx",
                        "args": ["-y", "pw"],
                    },
                    "anna_web": {"kind": "builtin", "builtin_name": "anna_web"},
                },
                "agents": {
                    "researcher": {
                        "write_dirs": ["reports"],
                        "mcp_servers": ["playwright"],
                        "permission_mode": "acceptEdits",
                    }
                },
            }
        }
    )
    assert cfg.subagents.dir_pool["reports"] == "~/reports"
    assert cfg.subagents.mcp_registry["playwright"].kind == "stdio"
    assert cfg.subagents.agents["researcher"].mcp_servers == ["playwright"]


@pytest.mark.parametrize(
    "reserved", ["anna_self_edit", "anna_google", "anna_delegate"]
)
def test_registry_key_colliding_with_forbidden_builtin_is_rejected(
    reserved: str,
) -> None:
    """A registry key reusing a reserved builtin name fails at config-load."""
    with pytest.raises(ValidationError, match="reserved builtin"):
        AnnaConfig.model_validate(
            {
                "subagents": {
                    "mcp_registry": {
                        reserved: {"kind": "stdio", "command": "x"},
                    }
                }
            }
        )


def test_normally_keyed_registry_still_parses() -> None:
    """A registry with non-reserved keys parses fine after the guard."""
    cfg = AnnaConfig.model_validate(
        {
            "subagents": {
                "mcp_registry": {
                    "playwright": {"kind": "stdio", "command": "npx"},
                    "anna_web": {"kind": "builtin", "builtin_name": "anna_web"},
                }
            }
        }
    )
    assert set(cfg.subagents.mcp_registry) == {"playwright", "anna_web"}
