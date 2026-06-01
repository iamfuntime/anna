"""Validate that the shipped anna.yaml.example loads via pydantic."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from anna.config import AnnaConfig, load_config


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = REPO_ROOT / "anna.yaml.example"


def test_example_yaml_parses() -> None:
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert raw is not None
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.auth.mode == "max"
    assert cfg.logging.level == "INFO"
    assert cfg.logging.format == "json"
    assert cfg.logging.audit.retention_days == 365
    assert cfg.logging.transcripts.retention_days == 30
    assert cfg.watchdog.interval_seconds == 300
    assert cfg.transports.slack.enabled is False
    assert cfg.transports.telegram.enabled is False
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.max_concurrent == 3
    assert cfg.scheduler.failure_threshold == 3


def test_scheduler_defaults_when_block_omitted() -> None:
    """A config with no scheduler: block uses SchedulerConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.state_path == "~/anna/schedules.yaml"
    assert cfg.scheduler.default_timeout_seconds == 300
    assert cfg.scheduler.max_concurrent == 3
    assert cfg.scheduler.poll_interval_seconds == 30
    assert cfg.scheduler.failure_threshold == 3


def test_scheduler_resolved_state_path_expands_tilde() -> None:
    raw = {"scheduler": {"state_path": "~/custom/schedules.yaml"}}
    cfg = AnnaConfig.model_validate(raw)
    resolved = str(cfg.scheduler.resolved_state_path)
    assert resolved.endswith("/custom/schedules.yaml")
    assert "~" not in resolved


def test_scheduler_disabled_persists() -> None:
    raw = {"scheduler": {"enabled": False}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.scheduler.enabled is False


def test_load_config_uses_explicit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "anna.yaml"
    cfg_file.write_text(
        "auth:\n  mode: api_key\n"
        "logging:\n  level: DEBUG\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANNA_CONFIG_PATH", str(cfg_file))
    cfg = load_config()
    assert cfg.auth.mode == "api_key"
    assert cfg.logging.level == "DEBUG"


def test_ship_enabled_raises_in_phase_one() -> None:
    raw = {"logging": {"ship": {"enabled": True, "destination": "udp://logs:514"}}}
    with pytest.raises(Exception):
        AnnaConfig.model_validate(raw)


def test_invalid_daily_sweep_time_rejected() -> None:
    raw = {"housekeeping": {"daily_sweep_time": "25:99"}}
    with pytest.raises(Exception):
        AnnaConfig.model_validate(raw)


def test_tools_defaults_when_block_omitted() -> None:
    """A config with no tools: block uses ToolsConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.tools.enabled is True
    assert cfg.tools.web_search.provider == "brave"
    assert cfg.tools.web_search.api_key_env == "BRAVE_SEARCH_API_KEY"
    assert cfg.tools.web_search.max_results == 10
    assert cfg.tools.web_search.timeout_seconds == 15
    assert cfg.tools.web_fetch.timeout_seconds == 30
    assert "Chrome/" in cfg.tools.web_fetch.user_agent
    assert cfg.tools.web_fetch.playwright_fallback is False
    assert cfg.tools.vault_download.destination == "~/Obsidian/ANNA/Inbox"
    assert cfg.tools.vault_download.max_size_bytes == 52_428_800


def test_tools_disabled_persists() -> None:
    raw = {"tools": {"enabled": False}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.tools.enabled is False


def test_tools_web_search_max_results_validation() -> None:
    for bad in (0, -1, 51, 1000):
        with pytest.raises(Exception):
            AnnaConfig.model_validate({"tools": {"web_search": {"max_results": bad}}})


def test_tools_web_fetch_timeout_validation() -> None:
    for bad in (0, -5, 301, 9999):
        with pytest.raises(Exception):
            AnnaConfig.model_validate({"tools": {"web_fetch": {"timeout_seconds": bad}}})


def test_tools_vault_download_max_size_validation() -> None:
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"tools": {"vault_download": {"max_size_bytes": 0}}})
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"tools": {"vault_download": {"max_size_bytes": -1}}})


def test_tools_vault_download_destination_expands_tilde() -> None:
    raw = {"tools": {"vault_download": {"destination": "~/custom/inbox"}}}
    cfg = AnnaConfig.model_validate(raw)
    resolved = str(cfg.tools.vault_download.resolved_destination)
    assert resolved.endswith("/custom/inbox")
    assert "~" not in resolved


def test_example_yaml_includes_tools_block() -> None:
    """Confirms anna.yaml.example's tools: block round-trips through the model."""
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.tools.enabled is True
    assert cfg.tools.web_search.provider == "brave"
    assert cfg.tools.web_fetch.playwright_fallback is False


def test_subagents_defaults_when_block_omitted() -> None:
    """A config with no subagents: block uses SubagentsConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.subagents.enabled is True
    assert cfg.subagents.max_concurrent == 3
    assert cfg.subagents.default_timeout_seconds == 300
    assert cfg.subagents.concurrency_acquire_timeout_seconds == 60
    assert cfg.subagents.transcript_subdir == "subagent"
    # Default allowed_tools includes the file ops and the three anna_web
    # tools, and explicitly excludes anna_self_edit / anna_google /
    # anna_delegate prefixes.
    tools = cfg.subagents.allowed_tools
    assert "Read" in tools
    assert "Write" in tools
    assert "Edit" in tools
    assert "Glob" in tools
    assert "Grep" in tools
    assert "mcp__anna_web__web_search" in tools
    assert "mcp__anna_web__web_fetch" in tools
    assert "mcp__anna_web__vault_download" in tools
    assert not any(t.startswith("mcp__anna_self_edit__") for t in tools)
    assert not any(t.startswith("mcp__anna_google__") for t in tools)
    assert not any(t.startswith("mcp__anna_delegate__") for t in tools)


def test_subagents_disabled_persists() -> None:
    raw = {"subagents": {"enabled": False}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.subagents.enabled is False


def test_subagent_transcript_dir_derived() -> None:
    """The derived path joins anna_home + transcripts + transcript_subdir."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}})
    expected = cfg.transcripts_dir / "subagent"
    assert cfg.subagent_transcript_dir == expected
    # Custom transcript_subdir flows through.
    cfg2 = AnnaConfig.model_validate({"subagents": {"transcript_subdir": "agents"}})
    assert cfg2.subagent_transcript_dir == cfg2.transcripts_dir / "agents"


def test_example_yaml_includes_subagents_block() -> None:
    """Confirms anna.yaml.example's subagents: block round-trips through the model."""
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.subagents.enabled is True
    assert cfg.subagents.max_concurrent == 3
    assert cfg.subagents.default_timeout_seconds == 300
    assert cfg.subagents.concurrency_acquire_timeout_seconds == 60
    assert cfg.subagents.transcript_subdir == "subagent"
    assert "Read" in cfg.subagents.allowed_tools
    assert "mcp__anna_web__web_search" in cfg.subagents.allowed_tools
