"""Validate that the shipped anna.yaml.example loads via pydantic."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from anna.config import (
    AnnaConfig,
    CheckpointConfig,
    IdentityAliasEntry,
    RuntimeVisibilityConfig,
    VoiceConfig,
    VoiceInboundConfig,
    VoiceOutboundConfig,
    WebDashboardConfig,
    load_config,
)


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


def test_reports_defaults_when_block_omitted() -> None:
    """A config with no reports: block uses ReportsConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.reports.slack_channel_id == ""


def test_reports_slack_channel_persists() -> None:
    raw = {"reports": {"slack_channel_id": "C0REPORTS"}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.reports.slack_channel_id == "C0REPORTS"


def test_example_yaml_includes_reports_block() -> None:
    """Confirms anna.yaml.example's reports: block round-trips through the model."""
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.reports.slack_channel_id == ""


def test_reports_env_override_applies_when_unset() -> None:
    from anna.config import _apply_env_overrides

    import os

    prev = os.environ.get("ANNA_REPORTS_SLACK_CHANNEL_ID")
    os.environ["ANNA_REPORTS_SLACK_CHANNEL_ID"] = "C_FROM_ENV"
    try:
        data = _apply_env_overrides({})
        assert data["reports"]["slack_channel_id"] == "C_FROM_ENV"
    finally:
        if prev is None:
            del os.environ["ANNA_REPORTS_SLACK_CHANNEL_ID"]
        else:
            os.environ["ANNA_REPORTS_SLACK_CHANNEL_ID"] = prev


def test_reports_env_override_does_not_clobber_yaml_value() -> None:
    from anna.config import _apply_env_overrides

    import os

    prev = os.environ.get("ANNA_REPORTS_SLACK_CHANNEL_ID")
    os.environ["ANNA_REPORTS_SLACK_CHANNEL_ID"] = "C_FROM_ENV"
    try:
        data = _apply_env_overrides({"reports": {"slack_channel_id": "C_FROM_YAML"}})
        assert data["reports"]["slack_channel_id"] == "C_FROM_YAML"
    finally:
        if prev is None:
            del os.environ["ANNA_REPORTS_SLACK_CHANNEL_ID"]
        else:
            os.environ["ANNA_REPORTS_SLACK_CHANNEL_ID"] = prev


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


def test_example_yaml_parses_with_subagents_grant_blocks_uncommented() -> None:
    """The documented dir_pool / mcp_registry / agents examples round-trip.

    The anna.yaml.example ships these commented (so the default config stays
    minimal), but the operator copies + uncomments them. This test keeps the
    documented shapes honest by validating them through the real config
    loader exactly as written in the example's `subagents:` comment block.
    """
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    raw["subagents"]["dir_pool"] = {"git": "~/git", "brain": "~/Obsidian/Brain"}
    raw["subagents"]["mcp_registry"] = {
        "web": {"kind": "builtin", "builtin_name": "anna_web"},
        "playwright": {
            "kind": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
        },
        "detections": {
            "kind": "http",
            "url": "https://detections.internal/mcp",
            "headers": {"Authorization": "Bearer x"},
            "tool_names": ["search_rules", "get_rule"],
        },
    }
    raw["subagents"]["agents"] = {
        "code-writer": {"write_dirs": ["git"], "mcp_servers": ["web"]},
        "brain-writer": {
            "write_dirs": ["brain"],
            "mcp_servers": ["web", "detections"],
            "permission_mode": "acceptEdits",
        },
    }
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.subagents.dir_pool["git"] == "~/git"
    assert cfg.subagents.mcp_registry["playwright"].kind == "stdio"
    assert cfg.subagents.mcp_registry["detections"].tool_names == [
        "search_rules",
        "get_rule",
    ]
    assert cfg.subagents.agents["code-writer"].write_dirs == ["git"]
    assert cfg.subagents.agents["brain-writer"].permission_mode == "acceptEdits"


def test_cli_transport_defaults_when_block_omitted() -> None:
    """A config with no transports.cli block uses CLITransportConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.transports.cli.enabled is True
    assert cfg.transports.cli.socket_path == "~/anna/anna.sock"
    assert cfg.transports.cli.idle_gap_minutes == 30
    assert cfg.transports.cli.framing == "ndjson"


def test_cli_transport_resolved_socket_path_expands_tilde() -> None:
    raw = {"transports": {"cli": {"socket_path": "~/custom/anna.sock"}}}
    cfg = AnnaConfig.model_validate(raw)
    resolved = str(cfg.transports.cli.resolved_socket_path)
    assert resolved.endswith("/custom/anna.sock")
    assert "~" not in resolved


def test_cli_idle_gap_minutes_rejects_non_positive() -> None:
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"transports": {"cli": {"idle_gap_minutes": 0}}})


def test_cli_idle_gap_minutes_rejects_over_one_day() -> None:
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"transports": {"cli": {"idle_gap_minutes": 1500}}})


def test_identity_alias_entry_rejects_hyphen() -> None:
    with pytest.raises(Exception):
        IdentityAliasEntry(canonical="seth-1")


def test_identity_alias_entry_accepts_alnum_underscore() -> None:
    a = IdentityAliasEntry(canonical="seth")
    assert a.canonical == "seth"
    b = IdentityAliasEntry(canonical="seth_work")
    assert b.canonical == "seth_work"


def test_anna_config_rejects_duplicate_canonical() -> None:
    raw = {
        "identities": [
            {"canonical": "seth", "slack_user_id": "USP2QLB41"},
            {"canonical": "seth", "telegram_chat_id": "993947726"},
        ]
    }
    with pytest.raises(Exception) as excinfo:
        AnnaConfig.model_validate(raw)
    # The error message mentions the offending canonical name (each
    # duplicated value is named in the validator's ValueError).
    assert "seth" in str(excinfo.value)


def test_runtime_visibility_defaults() -> None:
    """RuntimeVisibilityConfig defaults parse and lint_patterns has 5 seeds."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}})
    vis = cfg.runtime.visibility
    assert isinstance(vis, RuntimeVisibilityConfig)
    assert vis.reaction_signal is True
    assert vis.cadence_reminder is True
    assert vis.response_lint is True
    assert vis.slack_emoji == "thinking_face"
    assert vis.telegram_typing_max_seconds == 180
    assert len(vis.lint_patterns) == 5


def test_runtime_visibility_rejects_bad_regex() -> None:
    """A malformed lint_patterns entry raises at validation, not at first match."""
    with pytest.raises(ValueError) as excinfo:
        RuntimeVisibilityConfig(lint_patterns=["(unclosed"])
    assert "lint_patterns" in str(excinfo.value)


def test_web_dashboard_defaults_when_block_omitted() -> None:
    """A config with no web: block uses WebDashboardConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert isinstance(cfg.web, WebDashboardConfig)
    assert cfg.web.enabled is True
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8765
    assert cfg.web.target_unit == "anna.service"


def test_web_dashboard_direct_instantiation_defaults() -> None:
    """WebDashboardConfig() with no args matches the documented defaults."""
    wd = WebDashboardConfig()
    assert wd.enabled is True
    assert wd.host == "127.0.0.1"
    assert wd.port == 8765
    assert wd.target_unit == "anna.service"


def test_web_dashboard_disabled_persists() -> None:
    raw = {"web": {"enabled": False}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.web.enabled is False


def test_web_dashboard_port_validation() -> None:
    for bad in (0, -1, 65536, 100000):
        with pytest.raises(Exception):
            AnnaConfig.model_validate({"web": {"port": bad}})


def test_web_dashboard_custom_values_persist() -> None:
    raw = {
        "web": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 9000,
            "target_unit": "anna-test.service",
        }
    }
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 9000
    assert cfg.web.target_unit == "anna-test.service"


def test_example_yaml_includes_web_block() -> None:
    """Confirms anna.yaml.example's web: block round-trips through the model."""
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.web.enabled is True
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8765
    assert cfg.web.target_unit == "anna.service"


def test_voice_block_parses() -> None:
    """A full voice: block round-trips through the model."""
    raw = {
        "voice": {
            "inbound": {
                "enabled": True,
                "provider": "whisper-openai",
                "api_key_env": "OPENAI_API_KEY",
                "model": "whisper-1",
                "keep_audio_files": True,
                "max_duration_seconds": 600,
                "max_audio_size_bytes": 26_214_400,
                "hint_language": "en",
                "timeout_seconds": 60,
                "retry_attempts": 2,
            },
            "outbound": {
                "enabled": True,
                "transports": ["slack", "telegram"],
                "provider": "openai-tts",
                "api_key_env": "OPENAI_API_KEY",
                "model": "tts-1",
                "voice_id": "alloy",
                "voice_only": True,
                "recent_voice_window_seconds": 600,
                "max_synthesis_chars": 4000,
                "timeout_seconds": 30,
            },
        }
    }
    cfg = AnnaConfig.model_validate(raw)
    assert isinstance(cfg.voice, VoiceConfig)
    assert isinstance(cfg.voice.inbound, VoiceInboundConfig)
    assert isinstance(cfg.voice.outbound, VoiceOutboundConfig)
    assert cfg.voice.inbound.provider == "whisper-openai"
    assert cfg.voice.inbound.model == "whisper-1"
    assert cfg.voice.inbound.max_audio_size_bytes == 26_214_400
    assert cfg.voice.inbound.hint_language == "en"
    assert cfg.voice.outbound.transports == ["slack", "telegram"]
    assert cfg.voice.outbound.voice_id == "alloy"
    assert cfg.voice.outbound.voice_only is True


def test_voice_defaults_when_block_omitted() -> None:
    """A config with no voice: block uses VoiceConfig defaults."""
    raw = {"auth": {"mode": "max"}}
    cfg = AnnaConfig.model_validate(raw)
    assert isinstance(cfg.voice, VoiceConfig)
    # Inbound defaults.
    assert cfg.voice.inbound.enabled is True
    assert cfg.voice.inbound.provider == "whisper-openai"
    assert cfg.voice.inbound.api_key_env == "OPENAI_API_KEY"
    assert cfg.voice.inbound.model == "whisper-1"
    assert cfg.voice.inbound.keep_audio_files is True
    assert cfg.voice.inbound.max_duration_seconds == 600
    assert cfg.voice.inbound.max_audio_size_bytes == 26_214_400
    assert cfg.voice.inbound.hint_language == "en"
    assert cfg.voice.inbound.timeout_seconds == 60
    assert cfg.voice.inbound.retry_attempts == 2
    # Outbound defaults.
    assert cfg.voice.outbound.enabled is True
    assert cfg.voice.outbound.transports == ["slack", "telegram"]
    assert cfg.voice.outbound.provider == "openai-tts"
    assert cfg.voice.outbound.model == "tts-1"
    assert cfg.voice.outbound.voice_id == "alloy"
    assert cfg.voice.outbound.voice_only is True
    assert cfg.voice.outbound.recent_voice_window_seconds == 600
    assert cfg.voice.outbound.max_synthesis_chars == 4000
    assert cfg.voice.outbound.timeout_seconds == 30


def test_voice_direct_instantiation_defaults() -> None:
    """VoiceConfig() with no args matches the documented defaults."""
    vc = VoiceConfig()
    assert vc.inbound.enabled is True
    assert vc.outbound.enabled is True
    assert vc.inbound.hint_language == "en"


def test_voice_hint_language_accepts_null() -> None:
    """hint_language: null lets the provider auto-detect."""
    raw = {"voice": {"inbound": {"hint_language": None}}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.voice.inbound.hint_language is None


def test_voice_inbound_disabled_persists() -> None:
    raw = {"voice": {"inbound": {"enabled": False}}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.voice.inbound.enabled is False
    # Outbound still defaults on.
    assert cfg.voice.outbound.enabled is True


def test_voice_inbound_provider_validation() -> None:
    """An unknown inbound provider is rejected by the Literal."""
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"voice": {"inbound": {"provider": "bogus"}}})


def test_voice_outbound_provider_validation() -> None:
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"voice": {"outbound": {"provider": "bogus"}}})


def test_voice_outbound_transport_validation() -> None:
    """The transports allowlist rejects unknown adapters (e.g. cli)."""
    with pytest.raises(Exception):
        AnnaConfig.model_validate({"voice": {"outbound": {"transports": ["cli"]}}})


def test_voice_inbound_numeric_field_validation() -> None:
    for field, bad in (
        ("max_duration_seconds", 0),
        ("max_duration_seconds", -1),
        ("max_audio_size_bytes", 0),
        ("max_audio_size_bytes", -5),
        ("retry_attempts", -1),
    ):
        with pytest.raises(Exception):
            AnnaConfig.model_validate({"voice": {"inbound": {field: bad}}})


def test_voice_outbound_numeric_field_validation() -> None:
    for field, bad in (
        ("recent_voice_window_seconds", 0),
        ("recent_voice_window_seconds", -1),
        ("max_synthesis_chars", 0),
        ("max_synthesis_chars", -10),
    ):
        with pytest.raises(Exception):
            AnnaConfig.model_validate({"voice": {"outbound": {field: bad}}})


def test_voice_dir_derived() -> None:
    """The derived voice_dir joins anna_home + transcripts + voice."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}})
    assert cfg.voice_dir == cfg.transcripts_dir / "voice"


def test_example_yaml_includes_voice_block() -> None:
    """Confirms anna.yaml.example's voice: block round-trips through the model."""
    raw = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.voice.inbound.enabled is True
    assert cfg.voice.inbound.provider == "whisper-openai"
    assert cfg.voice.inbound.model == "whisper-1"
    assert cfg.voice.outbound.enabled is True
    assert cfg.voice.outbound.provider == "openai-tts"
    assert cfg.voice.outbound.voice_id == "alloy"
    assert cfg.voice.outbound.transports == ["slack", "telegram"]


def test_anna_config_identities_round_trips_through_model_dump() -> None:
    raw = {
        "identities": [
            {
                "canonical": "seth",
                "slack_user_id": "USP2QLB41",
                "telegram_chat_id": "993947726",
                "cli_username": "funtime",
            }
        ]
    }
    cfg = AnnaConfig.model_validate(raw)
    assert len(cfg.identities) == 1
    assert cfg.identities[0].canonical == "seth"
    assert cfg.identities[0].slack_user_id == "USP2QLB41"
    assert cfg.identities[0].telegram_chat_id == "993947726"
    assert cfg.identities[0].cli_username == "funtime"

    dumped = cfg.model_dump()
    assert dumped["identities"] == [
        {
            "canonical": "seth",
            "slack_user_id": "USP2QLB41",
            "telegram_chat_id": "993947726",
            "cli_username": "funtime",
        }
    ]
    # Re-validating the dumped form produces an equivalent config.
    cfg2 = AnnaConfig.model_validate(dumped)
    assert cfg2.identities == cfg.identities


def test_checkpoint_defaults_when_block_omitted() -> None:
    """A config with no checkpoint: block uses CheckpointConfig defaults."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}})
    assert cfg.checkpoint.periodic_enabled is True
    assert cfg.checkpoint.every_turns == 6
    assert cfg.checkpoint.every_minutes == 10
    assert cfg.checkpoint.resume_from_transcript is True
    assert cfg.checkpoint.tail_max_turns == 8
    assert cfg.checkpoint.tail_max_tokens == 1500


def test_checkpoint_parses_from_nested_block() -> None:
    raw = {"checkpoint": {"every_turns": 3, "periodic_enabled": False}}
    cfg = AnnaConfig.model_validate(raw)
    assert cfg.checkpoint.every_turns == 3
    assert cfg.checkpoint.periodic_enabled is False
    # Unset fields fall back to defaults.
    assert cfg.checkpoint.every_minutes == 10


@pytest.mark.parametrize(
    "field",
    ["every_turns", "every_minutes", "tail_max_turns", "tail_max_tokens"],
)
def test_checkpoint_rejects_non_positive_int(field: str) -> None:
    with pytest.raises(ValueError):
        CheckpointConfig(**{field: 0})
    with pytest.raises(ValueError):
        CheckpointConfig(**{field: -1})
