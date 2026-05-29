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
