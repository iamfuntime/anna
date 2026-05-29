"""Configuration loading.

ANNA reads two files at startup:

* ``.env`` (chmod 600) for secrets, loaded via python-dotenv.
* ``anna.yaml`` for non-secret runtime config, validated with pydantic.

The :func:`load_config` function returns an :class:`AnnaConfig` instance that
the rest of the runtime treats as immutable. A config reload (triggered by the
watchfiles observer in :mod:`anna.runtime.watchdog`) calls :func:`load_config`
again and swaps the instance, emitting ``audit.config.reloaded``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class AuthConfig(BaseModel):
    mode: Literal["max", "api_key"] = "max"


class SlackTransportConfig(BaseModel):
    enabled: bool = False
    admin_channel: str = ""


class TelegramTransportConfig(BaseModel):
    enabled: bool = False
    admin_chat_id: str = ""


class TransportsConfig(BaseModel):
    slack: SlackTransportConfig = Field(default_factory=SlackTransportConfig)
    telegram: TelegramTransportConfig = Field(default_factory=TelegramTransportConfig)


class VaultConfig(BaseModel):
    path: str = "~/Obsidian/ANNA"

    @property
    def resolved_path(self) -> Path:
        return Path(os.path.expanduser(self.path))


class WatchdogConfig(BaseModel):
    interval_seconds: int = 300
    worker_stall_seconds: int = 90
    restart_stalled_workers: bool = False


class AuditConfig(BaseModel):
    enabled: bool = True
    retention_days: int = 365
    fsync_on_write: bool = True


class TranscriptsConfig(BaseModel):
    enabled: bool = True
    retention_days: int = 30


class ShipConfig(BaseModel):
    enabled: bool = False
    destination: str = ""
    format: str = "json"

    @model_validator(mode="after")
    def _phase_one_block(self) -> "ShipConfig":
        # The ship block is scaffolded for Phase 2. Setting enabled in Phase 1
        # raises a config error pointing at the Phase 2 stub.
        if self.enabled:
            raise ValueError(
                "logging.ship.enabled is a Phase 2 feature. "
                "See the v3 buildout plan section 9 for the rationale."
            )
        return self


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "text"] = "json"
    audit: AuditConfig = Field(default_factory=AuditConfig)
    transcripts: TranscriptsConfig = Field(default_factory=TranscriptsConfig)
    ship: ShipConfig = Field(default_factory=ShipConfig)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.upper()
        return v


class HousekeepingConfig(BaseModel):
    daily_sweep_time: str = "03:17"

    @field_validator("daily_sweep_time")
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("daily_sweep_time must be HH:MM")
        hh, mm = parts
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError("daily_sweep_time must be a valid 24h time")
        return v


class SessionsConfig(BaseModel):
    dm_gap_hours: float = 8.0
    thread_gap_hours: float = 1.0


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class AnnaConfig(BaseModel):
    auth: AuthConfig = Field(default_factory=AuthConfig)
    transports: TransportsConfig = Field(default_factory=TransportsConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    housekeeping: HousekeepingConfig = Field(default_factory=HousekeepingConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)

    # Derived runtime paths. Not in the YAML file. ANNA_HOME from .env wins,
    # falling back to ~/anna. The setup wizard always writes ANNA_HOME.
    anna_home: Path = Field(default_factory=lambda: Path(os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna"))))

    @property
    def audit_dir(self) -> Path:
        return self.anna_home / "audit"

    @property
    def transcripts_dir(self) -> Path:
        return self.anna_home / "transcripts"

    @property
    def core_dir(self) -> Path:
        return self.anna_home / "core"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _resolve_config_path() -> Path:
    """Look in the expected locations for anna.yaml.

    Priority order:

    1. ``$ANNA_CONFIG_PATH`` if set.
    2. ``$ANNA_HOME/anna.yaml``.
    3. ``./anna.yaml`` (for development).
    """
    explicit = os.environ.get("ANNA_CONFIG_PATH")
    if explicit:
        return Path(os.path.expanduser(explicit))
    anna_home = Path(os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")))
    candidate = anna_home / "anna.yaml"
    if candidate.is_file():
        return candidate
    return Path("anna.yaml")


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Layer env-var overrides on top of the parsed YAML.

    The two we honor explicitly are ANNA_LOG_LEVEL and ANNA_LOG_FORMAT, which
    the .env file documents as override knobs. Anything else in the YAML is
    treated as authoritative.
    """
    level = os.environ.get("ANNA_LOG_LEVEL")
    if level:
        data.setdefault("logging", {})["level"] = level

    fmt = os.environ.get("ANNA_LOG_FORMAT")
    if fmt:
        data.setdefault("logging", {})["format"] = fmt

    return data


def load_config(path: Path | None = None) -> AnnaConfig:
    """Read .env and anna.yaml, return a validated config object."""
    # .env first, so anna.yaml's env overrides see the right values.
    load_dotenv(override=False)

    config_path = path or _resolve_config_path()
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp) or {}
    else:
        raw = {}

    raw = _apply_env_overrides(raw)
    return AnnaConfig.model_validate(raw)
