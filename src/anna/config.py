"""Configuration loading.

ANNA reads two files at startup:

* ``.env`` (chmod 600) for secrets, loaded via python-dotenv.
* ``anna.yaml`` for non-secret runtime config, validated with pydantic.

The :func:`load_config` function returns an :class:`AnnaConfig` instance that
the rest of the runtime treats as immutable for the lifetime of the process.
There is no hot-reload: edits to ``anna.yaml`` take effect on the next
``systemctl --user restart anna``. A reloader was scoped out in Phase 1 as
not worth the per-consumer plumbing — see MEMORY.md (2026-06-01) for the
tradeoff.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------

# SDK tier aliases accepted verbatim by ClaudeAgentOptions.model.
_MODEL_ALIASES: frozenset[str] = frozenset({"opus", "sonnet", "haiku", "inherit"})

# Shape check for a full model ID. Deliberately loose so it does not rot as
# new model versions ship: covers Anthropic-direct ``claude-...`` IDs and
# Bedrock-style ``anthropic.claude-...`` / ``us.anthropic.claude-...`` /
# ``eu.anthropic.claude-...`` forms. We do NOT pin specific version strings.
# Requires a non-empty suffix after ``claude-`` and is end-anchored with ``\Z``
# (not ``$``) so a bare ``claude-`` or an embedded-newline value cannot slip through.
_MODEL_ID_RE = re.compile(r"^(us\.|eu\.)?(anthropic\.)?claude-[\w.:-]+\Z")


def validate_model_string(value: str | None) -> str | None:
    """Light validator for a configurable Claude model string.

    ``None``/unset is always valid and means "inherit the CLI/account
    default" (today's behavior — no ``model=`` passed to the SDK). A set
    value must be either a known SDK tier alias
    (``opus``/``sonnet``/``haiku``/``inherit``) or a well-formed full model
    ID matching :data:`_MODEL_ID_RE`. Anything else (e.g. ``"Fable 5"`` or
    ``"Opus 4.8"`` with a space) is rejected at config load with a clear
    error listing the accepted forms.

    This is a SHAPE check, not an allowlist of specific version IDs — the
    set of valid models drifts as new versions ship, so a hardcoded table
    would rot. The value is passed through to the SDK unchanged; an
    otherwise-well-formed-but-nonexistent ID still fails loudly at the SDK
    boundary.
    """
    if value is None:
        return None
    if value in _MODEL_ALIASES or _MODEL_ID_RE.match(value):
        return value
    raise ValueError(
        f"model must be a tier alias {sorted(_MODEL_ALIASES)} or a "
        f"well-formed model ID matching {_MODEL_ID_RE.pattern!r} "
        f"(e.g. 'claude-sonnet-4-5', 'us.anthropic.claude-...'); "
        f"got {value!r}"
    )


# Valid Claude Agent SDK reasoning-effort levels (ClaudeAgentOptions.effort).
# Mirrors the SDK's EffortLevel literal; when effort is None/unset the SDK
# applies its own default, which is "high".
_EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})


def validate_effort_string(value: str | None) -> str | None:
    """Light validator for a configurable reasoning-effort level.

    ``None``/unset is always valid and means "no ``effort=`` passed to the
    SDK" — the SDK then applies its own default (``"high"``). A set value
    must be one of the SDK's effort levels
    (``low``/``medium``/``high``/``xhigh``/``max``); input is
    case-insensitive and normalized to lowercase. Anything else is rejected
    at config load with a clear error listing the accepted values.
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in _EFFORT_LEVELS:
        return normalized
    raise ValueError(
        f"effort must be one of {sorted(_EFFORT_LEVELS)} "
        f"(unset = SDK default 'high'); got {value!r}"
    )


# ---------------------------------------------------------------------------
# ANNA_HOME resolution
# ---------------------------------------------------------------------------


def _resolve_anna_home() -> Path:
    """Canonical ``$ANNA_HOME`` directory — the single source of truth.

    ``ANNA_HOME`` from the environment wins (the systemd units and the
    setup wizard always write it), expanded via
    :func:`os.path.expanduser`, falling back to ``~/anna``.

    Module-level rather than a method on :class:`AnnaConfig` because
    :func:`load_config` needs the answer *before* the model is
    instantiated (the explicit ``.env`` load anchors here). Every
    ``ANNA_HOME`` read in this module funnels through this helper —
    do not add another inline ``os.environ.get("ANNA_HOME", ...)``;
    see TaskNote ANNA-Consolidate-ANNA_HOME-resolution.
    """
    return Path(os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")))


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class AuthConfig(BaseModel):
    mode: Literal["max", "api_key"] = "max"


class RuntimeVisibilityConfig(BaseModel):
    """Cadence-visibility hooks (per Inbox/2026-06-02 plan).

    Three independently-toggleable surfaces that give the operator feedback
    while ANNA is mid-turn on buffered transports:

    * ``reaction_signal`` — per-transport "thinking" signal posted before
      ``client.query()`` and cleared on ``ResultMessage`` (Slack reaction,
      Telegram typing action, CLI frame).
    * ``cadence_reminder`` — small ``<system-reminder>`` block sourced from
      ``core/CADENCE.md`` and prepended to the inbound text on Slack and
      Telegram only (CLI sees deltas live, no reminder needed).
    * ``response_lint`` — telemetry-only regex scan of the final
      ``reply_text`` for known bad-cadence phrases. Emits structured
      warnings; never blocks delivery.

    Defaults are on — the primary pain (10-90s blank pauses on buffered
    transports) is real and the operator wants the fix live. Each flag can
    be flipped independently in ``anna.yaml`` without restarting the
    others.
    """

    reaction_signal: bool = True
    cadence_reminder: bool = True
    response_lint: bool = True

    # Worker-level periodic "drip" flush (per Inbox/2026-06-04 plan). During a
    # long single-turn run (sub-agent chains, multi-tool sequences) the worker
    # flushes the pending narration buffer to buffered transports (Slack,
    # Telegram) on this wall-clock cadence, layered on top of the existing
    # tool-use-boundary flush, so the operator is not staring at dead air. The
    # scheduler/``completion_future`` path and voice-only outbound stay
    # consolidated and never drip. ``0`` disables the timer entirely (today's
    # behavior); negatives are rejected at load. Slack/Telegram interactive
    # only; no hot-reload — takes effect on restart.
    periodic_flush_seconds: int = 30

    # Slack-specific knobs. Custom emojis may not exist on every workspace;
    # if reactions.add fails the worker logs a warning and the SDK turn
    # continues uninterrupted.
    slack_emoji: str = "thinking_face"

    # Telegram refresher bound. Beyond this many seconds the refresher
    # stops and lets the typing indicator naturally expire — better to
    # show "stopped typing" than to spam send_chat_action indefinitely on
    # a runaway SDK call.
    telegram_typing_max_seconds: int = 180

    # Telemetry-only lint patterns. Regex strings compiled at config-load
    # time so a broken pattern fails fast at boot, not at first match.
    lint_patterns: list[str] = Field(
        default_factory=lambda: [
            r"kicking off .{0,40}\bin the background\b",
            r"\bon it\b\s*[—-]\s*\w+ing\b",
            r"^Synthesizing:",
            r"^Let me \w+\s+[—-]\s+",
            r"backgrounded so\b",
        ]
    )

    @field_validator("periodic_flush_seconds")
    @classmethod
    def _flush_seconds_non_negative(cls, v: int) -> int:
        """Reject a negative drip interval; ``0`` is the explicit off switch."""
        if v < 0:
            raise ValueError(
                "runtime.visibility.periodic_flush_seconds must be >= 0 "
                "(0 disables the timed drip)"
            )
        return v

    @field_validator("lint_patterns")
    @classmethod
    def _compile_patterns(cls, v: list[str]) -> list[str]:
        """Compile every pattern at config-load so a broken regex fails fast.

        We do not retain the compiled objects on the model (the linter
        recompiles at runtime with its own flags), but compiling here
        catches a malformed pattern at boot instead of at first lint call.
        """
        for pat in v:
            try:
                re.compile(pat)
            except re.error as exc:
                raise ValueError(
                    f"runtime.visibility.lint_patterns: invalid regex "
                    f"{pat!r}: {exc}"
                ) from exc
        return v


class RuntimeConfig(BaseModel):
    """SDK runtime options applied to every conversation worker.

    permission_mode is the Claude Agent SDK's tool-permission gate. ANNA runs
    as a headless service with no human at a terminal to approve tool calls,
    so the default is "bypassPermissions". The other valid values from the
    SDK are "default" (interactive prompts — never use in service), "plan"
    (read-only planning), and "acceptEdits" (auto-approve file edits, prompt
    for the rest). Override in anna.yaml if you want stricter behavior.
    """

    permission_mode: Literal[
        "default", "acceptEdits", "bypassPermissions", "plan"
    ] = "bypassPermissions"

    # Global-default Claude model for the main conversation loop and, via the
    # grant fallback layer, every sub-agent without an override. ``None``
    # (unset) means "inherit the CLI/account default" — today's behavior, no
    # ``model=`` passed to the SDK. A set value is either a tier alias
    # (``opus``/``sonnet``/``haiku``/``inherit``) or a full model ID; the light
    # validator below rejects garbage at load. Restart-gated: anna.yaml has no
    # hot-reload, so a change here takes effect on the next daemon restart.
    model: str | None = None

    # Fallback model for the main conversation loop. Passed through to the
    # SDK as ``ClaudeAgentOptions.fallback_model`` (CLI ``--fallback-model``):
    # when the primary ``model`` is overloaded or not available (which should
    # include subscription usage-cap exhaustion), the CLI transparently serves
    # the turn on this model instead of erroring. ``None`` (unset) preserves
    # today's behavior — no flag passed, a capped/overloaded primary fails the
    # turn. Same value grammar as ``model``. Restart-gated like everything
    # else in anna.yaml.
    fallback_model: str | None = None

    # Reasoning-effort level for the MAIN conversation loop, passed through
    # to the SDK as ``ClaudeAgentOptions.effort``. One of
    # low | medium | high | xhigh | max (case-insensitive, normalized to
    # lowercase). ``None`` (unset) means no ``effort=`` passed — the SDK
    # applies its own default, which is "high". Deliberately NOT inherited by
    # sub-agents (unlike ``model``): the grant fallback layer seeds
    # ``effort=None``, so an override-less sub-agent falls through to the SDK
    # default regardless of this setting. Restart-gated like the rest of
    # anna.yaml.
    effort: str | None = None

    visibility: RuntimeVisibilityConfig = Field(default_factory=RuntimeVisibilityConfig)

    @field_validator("model", "fallback_model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        return validate_model_string(v)

    @field_validator("effort")
    @classmethod
    def _validate_effort(cls, v: str | None) -> str | None:
        return validate_effort_string(v)


class SlackTransportConfig(BaseModel):
    enabled: bool = False


class TelegramTransportConfig(BaseModel):
    enabled: bool = False


class CLITransportConfig(BaseModel):
    """Phase 2 §5 CLI transport.

    A third ChannelAdapter alongside Slack and Telegram. Speaks NDJSON
    over a Unix-domain socket at ``socket_path``. Owner-only socket
    permissions (mode 0600) are the entire auth boundary — there is no
    token, no TLS, no remote case. For Docker deployments, either
    bind-mount the socket out or leave this disabled.

    See Inbox/2026-06-01-ANNA-Phase-2-CLI-Transport-Plan.md for the full
    design.
    """

    enabled: bool = True
    # Owner-only Unix-domain socket the daemon binds and `anna chat` /
    # `anna ask` connect to. Resolved against the operator's home via
    # os.path.expanduser when consumed.
    socket_path: str = "~/anna/anna.sock"
    # Per-CLI idle close, distinct from sessions.dm_gap_hours (8h) and
    # sessions.thread_gap_hours (1h). 30m lands between the two existing
    # gaps — long enough that stepping away for coffee doesn't tear the
    # conv down, short enough that a forgotten terminal doesn't pin a
    # worker overnight.
    idle_gap_minutes: int = 30
    # Wire framing. v1 ships NDJSON only; the Literal keeps the field
    # forward-compatible without exposing variant choice.
    framing: Literal["ndjson"] = "ndjson"

    @property
    def resolved_socket_path(self) -> Path:
        return Path(os.path.expanduser(self.socket_path))

    @field_validator("idle_gap_minutes")
    @classmethod
    def _idle_positive(cls, v: int) -> int:
        if v <= 0 or v > 24 * 60:
            raise ValueError("cli.idle_gap_minutes must be between 1 and 1440")
        return v


class TransportsConfig(BaseModel):
    slack: SlackTransportConfig = Field(default_factory=SlackTransportConfig)
    telegram: TelegramTransportConfig = Field(default_factory=TelegramTransportConfig)
    cli: CLITransportConfig = Field(default_factory=CLITransportConfig)


class VaultConfig(BaseModel):
    path: str = "~/anna/vault"

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


class CheckpointConfig(BaseModel):
    """Conversation checkpointing and transcript-tail resume.

    Two coordinated behaviours:

    * ``resume_from_transcript`` folds a bounded RAW tail of the JSONL
      transcript into the resume block when that tail is newer than the
      latest checkpoint — covering the gap left by a hard crash or kill
      that never ran graceful closeout. ``tail_max_turns`` /
      ``tail_max_tokens`` bound the injected excerpt.
    * ``periodic_enabled`` writes a lightweight checkpoint every
      ``every_turns`` turns or ``every_minutes`` minutes during an active
      conversation, between turns, decoupled from eviction.

    No hot-reload: edits take effect on the next ``systemctl --user
    restart anna``.
    """

    periodic_enabled: bool = True
    every_turns: int = 6
    every_minutes: int = 10
    resume_from_transcript: bool = True
    tail_max_turns: int = 8
    tail_max_tokens: int = 1500

    @field_validator(
        "every_turns",
        "every_minutes",
        "tail_max_turns",
        "tail_max_tokens",
    )
    @classmethod
    def _positive(cls, v: int, info: Any) -> int:
        if v <= 0:
            raise ValueError(f"checkpoint.{info.field_name} must be a positive integer")
        return v


class AdminConfig(BaseModel):
    """Operator-side destinations for out-of-band alerts.

    Used by :class:`anna.runtime.alerter.AdminAlerter` when a transport
    restart, SDK auth failure, or service restart needs to reach the
    operator on the surviving channel. The two destination fields are
    optional; if unset for the surviving transport, the alerter logs a
    WARNING and skips. The Slack value is a channel ID (e.g.
    ``C0123ABC``); the Telegram value is the operator's chat ID as a
    string.

    ``startup_alert`` controls the boot-time ping that fires after
    adapters connect. The message tags the restart as clean (sentinel
    found) or unclean (sentinel missing — likely crash, OOM-kill, or
    power loss).
    """

    slack_channel_id: str = ""
    telegram_chat_id: str = ""
    startup_alert: bool = True


class ReportsConfig(BaseModel):
    """Destination for ANNA's outbound report/digest cards.

    The ``slack_post`` MCP tool (``anna_slack_alerts`` server) posts through
    ANNA's own Slack adapter — the same path :class:`AdminAlerter` uses — so
    it works in headless/scheduled runs. When the tool is called without an
    explicit ``channel_id``, it falls back to ``slack_channel_id`` here. The
    feed-aggregator / threat-report skill points this at the channel the
    cards should land in. Left blank by default; an explicit ``channel_id``
    argument always wins over this fallback.

    Env-var override: ``ANNA_REPORTS_SLACK_CHANNEL_ID`` (only applied when
    the YAML value is unset).
    """

    slack_channel_id: str = ""


class GoogleAccountConfig(BaseModel):
    """A single Google account ANNA can read mail and calendar from.

    Two auth flavors are supported:

    * ``auth_type: oauth`` — a personal Google account. The
      ``credentials_file`` points at the OAuth client JSON downloaded from
      the GCP console (Desktop application type). The per-account refresh
      token is captured by ``python -m anna.setup.google_auth add <slug>``
      and persisted at ``state/google/token_<slug>.json``.
    * ``auth_type: service_account`` — a Workspace account where the
      operator controls the domain. ``credentials_file`` points at the
      service-account key JSON; ANNA uses domain-wide delegation to
      impersonate the ``email`` address. No browser flow needed; verify
      with ``python -m anna.setup.google_auth verify <slug>``.

    Paths in ``credentials_file`` are resolved relative to ``$ANNA_HOME``
    when not absolute. Slugs must be filesystem-safe (a-z, 0-9, underscore)
    because they become part of token filenames and tool-call arguments.
    """

    slug: str
    email: str
    auth_type: Literal["oauth", "service_account"]
    credentials_file: str

    @field_validator("slug")
    @classmethod
    def _slug_safe(cls, v: str) -> str:
        if not v:
            raise ValueError("google account slug cannot be empty")
        bad = [c for c in v if not (c.isalnum() or c == "_")]
        if bad:
            raise ValueError(
                f"google account slug must be a-z, 0-9, underscore only; "
                f"got disallowed chars: {''.join(sorted(set(bad)))}"
            )
        return v


class GoogleConfig(BaseModel):
    """Top-level Google integration toggle and per-account list.

    The phase-1 tool surface is read-only (Gmail message list/search/read
    and Calendar event listing). Write tools (drafts, sends, label edits,
    calendar mutations) are gated behind ``write_enabled``, which defaults
    off so a scheduled prompt cannot send mail without an explicit opt-in.

    Tokens for OAuth accounts live in ``$ANNA_HOME/state/google/``; the
    setup CLI creates the directory with 700 perms on first use.
    """

    enabled: bool = False
    accounts: list[GoogleAccountConfig] = Field(default_factory=list)
    write_enabled: bool = False

    @model_validator(mode="after")
    def _check_unique_slugs(self) -> "GoogleConfig":
        slugs = [a.slug for a in self.accounts]
        dupes = sorted({s for s in slugs if slugs.count(s) > 1})
        if dupes:
            raise ValueError(
                f"duplicate google account slugs: {', '.join(dupes)}"
            )
        return self


class WebSearchConfig(BaseModel):
    """Brave Search REST wrapper config.

    The API key is read from the env var named in ``api_key_env`` (default
    ``BRAVE_SEARCH_API_KEY``). If the variable is unset or empty at call
    time the tool surfaces a clear error rather than silently returning
    empty results.
    """

    provider: Literal["brave"] = "brave"
    api_key_env: str = "BRAVE_SEARCH_API_KEY"
    max_results: int = 10
    timeout_seconds: int = 15

    @field_validator("max_results")
    @classmethod
    def _max_results_positive(cls, v: int) -> int:
        if v <= 0 or v > 50:
            raise ValueError("web_search.max_results must be between 1 and 50")
        return v


class WebFetchConfig(BaseModel):
    """httpx-based URL fetch + HTML-to-Markdown conversion config.

    ``user_agent`` defaults to a current Chrome-on-Linux string so
    reputation-aware sites don't rate-limit ANNA the way they would an
    unknown UA. Override per-deployment if a specific situation requires
    it. ``playwright_fallback`` is a forward-compat flag — true is not
    yet wired (the dep ships in a later slice with the chromium install
    step). Leave as false.
    """

    timeout_seconds: int = 30
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    playwright_fallback: bool = False

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_positive(cls, v: int) -> int:
        if v <= 0 or v > 300:
            raise ValueError("web_fetch.timeout_seconds must be between 1 and 300")
        return v


class VaultDownloadConfig(BaseModel):
    """URL → vault-Inbox download config.

    ``destination`` resolves user-home (``~``). Files larger than
    ``max_size_bytes`` abort mid-stream and the partial file is removed.
    """

    destination: str = "~/Obsidian/ANNA/Inbox"
    max_size_bytes: int = 52_428_800  # 50 MB

    @property
    def resolved_destination(self) -> Path:
        return Path(os.path.expanduser(self.destination))

    @field_validator("max_size_bytes")
    @classmethod
    def _max_size_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("vault_download.max_size_bytes must be > 0")
        return v


class ToolsConfig(BaseModel):
    """Phase 2 §2 tool surface (slim slice).

    Three in-process tools mounted on each worker as the ``anna_web`` MCP
    server: web_search (Brave REST), web_fetch (httpx + future Playwright
    fallback), and vault_download (URL → ``~/Obsidian/ANNA/Inbox``).

    Gated by ``enabled`` so a deployment that doesn't want any of these
    can skip the mount cleanly. shell_exec is deferred to the Docker
    slice — when ANNA runs inside a container the security-posture
    question (open Bash vs allowlisted exec) gets a real answer; until
    then the worker's existing Bash tool covers the capability.
    """

    enabled: bool = True
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    web_fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)
    vault_download: VaultDownloadConfig = Field(default_factory=VaultDownloadConfig)


class McpServerSpec(BaseModel):
    """One entry in the operator-blessed ``subagents.mcp_registry``.

    The registry is POLICY (operator-only, restart-gated): it lives in
    ``anna.yaml`` and the ``anna_self_edit`` MCP cannot touch it. Untrusted
    GRANTS (per-agent config / persona frontmatter) may only *reference* a
    registry entry by name — they can never invent a server. An unknown name
    is dropped at resolution time, not at parse time.

    Three kinds:

    * ``builtin`` — dispatches to an ANNA in-process factory by
      ``builtin_name`` (e.g. ``anna_web``). The resolver's dispatch table
      structurally excludes the forbidden builtins (``anna_self_edit``,
      ``anna_google``, ``anna_delegate``); a registry entry naming one of
      those parses fine here but is dropped at resolution.
    * ``stdio`` — an external MCP server launched as a subprocess. Requires
      ``command``; ``args``/``env`` optional. Emitted to the SDK as the
      literal ``{"type": "stdio", "command": ..., ...}`` TypedDict.
    * ``http`` — an external MCP server reached over HTTP. Requires ``url``;
      ``headers`` optional.

    ``tool_names`` is the explicit tool allow-list contributed to
    ``allowed_tools``. For builtins it is ignored (the factory's own
    ``*_TOOL_NAMES`` drive the additions). For external servers it is left
    empty by default and the resolver contributes the server-namespace
    wildcard ``mcp__<name>__*`` (confirmed honored by the bundled CLI — see
    grants.py header); set it to restrict the surface to named tools.
    """

    kind: Literal["builtin", "stdio", "http"]
    builtin_name: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    tool_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "McpServerSpec":
        """Enforce required-field-per-kind and reject cross-kind combos.

        A builtin with a bogus ``builtin_name`` is *allowed* here —
        resolution-time drop handles unknown builtins. What is rejected is a
        structurally incoherent spec: a missing required field for the kind,
        or a field that belongs to a different kind (e.g. a stdio spec that
        also sets ``url``).
        """
        if self.kind == "builtin":
            if not self.builtin_name:
                raise ValueError(
                    "McpServerSpec(kind='builtin') requires builtin_name"
                )
            if self.command or self.url:
                raise ValueError(
                    "McpServerSpec(kind='builtin') must not set command/url"
                )
        elif self.kind == "stdio":
            if not self.command:
                raise ValueError(
                    "McpServerSpec(kind='stdio') requires command"
                )
            if self.url or self.headers:
                raise ValueError(
                    "McpServerSpec(kind='stdio') must not set url/headers"
                )
            if self.builtin_name:
                raise ValueError(
                    "McpServerSpec(kind='stdio') must not set builtin_name"
                )
        elif self.kind == "http":
            if not self.url:
                raise ValueError(
                    "McpServerSpec(kind='http') requires url"
                )
            if self.command or self.args or self.env:
                raise ValueError(
                    "McpServerSpec(kind='http') must not set command/args/env"
                )
            if self.builtin_name:
                raise ValueError(
                    "McpServerSpec(kind='http') must not set builtin_name"
                )
        return self


class AgentGrants(BaseModel):
    """Untrusted per-agent capability grant.

    Sourced from either ``subagents.agents.<slug>`` in anna.yaml or a
    persona file's ``grants:`` frontmatter. Every list field is a set of
    *names* that must resolve against the operator-blessed pools
    (``subagents.dir_pool`` / ``subagents.mcp_registry``); names that do not
    resolve are dropped + logged at resolution time, never invented. The
    reachable set is therefore always a subset of what the operator blessed.

    Per-field semantics in the resolver (grants.py): a field that is set
    REPLACES the lower layer's value; a field left unset (``None`` for the
    optional scalars, or simply absent) passes the lower layer through.
    Note the lists default to ``[]`` — an explicit empty list means "grant
    nothing here", which is a deliberate REPLACE, distinct from absence.
    """

    write_dirs: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] | None = None
    permission_mode: Literal[
        "default", "acceptEdits", "bypassPermissions", "plan"
    ] | None = None

    # Per-agent Claude model override. ``None``/unset passes the lower layer
    # through (inherit ``runtime.model`` / CLI default). A set value REPLACES
    # the lower layer (same scalar-override semantics as ``permission_mode``)
    # and is either a tier alias or a full model ID — the same light validator
    # as ``RuntimeConfig.model``. Unlike the name-list fields, ``model`` is
    # free-form: it is NOT resolved against an operator-blessed pool, because a
    # model choice cannot escalate capability (the grant security model is
    # about dir/server reachability, not which model executes), so there is no
    # clamp here.
    model: str | None = None

    # Per-agent reasoning-effort override, passed through to the SDK as
    # ``ClaudeAgentOptions.effort``. Same scalar REPLACE semantics as
    # ``model``: ``None``/unset passes the lower layer through, a set value
    # (low|medium|high|xhigh|max, case-insensitive) replaces it. NOTE the
    # grant fallback layer deliberately seeds ``effort=None`` — sub-agents
    # do NOT inherit ``runtime.effort`` — so an override-less agent falls
    # through to the SDK default ("high"). Like ``model``, effort is
    # free-form (not pool-resolved): it cannot escalate capability.
    effort: str | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        return validate_model_string(v)

    @field_validator("effort")
    @classmethod
    def _validate_effort(cls, v: str | None) -> str | None:
        return validate_effort_string(v)


class SubagentsConfig(BaseModel):
    """Phase 2 §3 sub-agent spawn runtime.

    ANNA spawns one-shot sub-agents via the ``anna_delegate`` MCP tool.
    Each sub-agent is a fresh ``ClaudeSDKClient`` carrying a persona file
    from ``$ANNA_HOME/agents/<slug>.md`` plus any matching skill files
    under ``$ANNA_HOME/skills/<slug>/``.

    * ``enabled`` — master kill switch for the sub-agent runtime and the
      ``anna_delegate`` MCP server. When false, the ``delegate`` tool is
      not mounted on the worker.
    * ``max_concurrent`` — process-wide cap on concurrent sub-agent runs.
      The runner holds one ``asyncio.Semaphore`` of this size.
    * ``default_timeout_seconds`` — per-delegation wall-clock cap when
      the caller does not override.
    * ``concurrency_acquire_timeout_seconds`` — how long a delegation
      waits on the semaphore before failing with a
      ``concurrency_timeout``.
    * ``transcript_subdir`` — directory name under
      ``$ANNA_HOME/transcripts/`` where sub-agent transcripts coalesce by
      slug + day.
    * ``allowed_tools`` — the canonical tool surface a sub-agent sees.
      Explicitly omits the ``mcp__anna_self_edit__``,
      ``mcp__anna_google__``, and ``mcp__anna_delegate__`` prefixes so a
      sub-agent cannot self-edit, read mail, or spawn further sub-agents.
    """

    enabled: bool = True
    max_concurrent: int = 3
    default_timeout_seconds: int = 300
    concurrency_acquire_timeout_seconds: int = 60
    transcript_subdir: str = "subagent"
    allowed_tools: list[str] = Field(
        default_factory=lambda: [
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "mcp__anna_web__web_search",
            "mcp__anna_web__web_fetch",
            "mcp__anna_web__vault_download",
        ]
    )
    extra_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Additional absolute directories mounted into every "
            "sub-agent's ``add_dirs`` so file ops (Read/Write/Edit/Glob/"
            "Grep) can reach beyond the ANNA vault. The sub-agent cwd "
            "stays the ANNA vault; these are extra reachable roots. Use "
            "to grant access to the collaborative Brain vault (detection "
            "templates, query libraries, example reports, Inbox output). "
            "Paths are ``~``-expanded at spawn time. Empty by default — "
            "keeps the depth-protection surface minimal unless a "
            "deployment opts in."
        ),
    )
    dir_pool: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "POLICY (operator-only, restart-gated): named pool of absolute "
            "write-directory paths a per-agent grant may reference. Maps a "
            "short name to an abs path (``~``-expanded at resolution). "
            "Untrusted grants (``agents.<slug>.write_dirs`` / persona "
            "frontmatter) may only name a pool entry; an unknown name is "
            "dropped + logged, never invented."
        ),
    )
    mcp_registry: dict[str, McpServerSpec] = Field(
        default_factory=dict,
        description=(
            "POLICY (operator-only, restart-gated): named registry of MCP "
            "server specs a per-agent grant may reference. Untrusted grants "
            "may only name a registry entry; unknown names and the forbidden "
            "builtins (anna_self_edit/anna_google/anna_delegate) are dropped "
            "+ logged at resolution, never invented."
        ),
    )
    anna_mcp_servers: list[str] = Field(
        default_factory=list,
        description=(
            "Registry names ANNA's main loop mounts for herself. Unknown "
            "names dropped + logged at resolution, never invented. List "
            "external (stdio/http) servers only; builtins mount via their "
            "own toggles."
        ),
    )
    agents: dict[str, AgentGrants] = Field(
        default_factory=dict,
        description=(
            "GRANTS (untrusted): per-slug capability grants. Each value may "
            "only reference dir_pool / mcp_registry names. Layered under "
            "persona frontmatter grants and over the global fallback "
            "(extra_dirs / allowed_tools). See grants.py for precedence."
        ),
    )

    @model_validator(mode="after")
    def _check_reserved_registry_keys(self) -> "SubagentsConfig":
        """Reject mcp_registry keys that collide with forbidden builtins.

        ``anna_self_edit`` / ``anna_google`` / ``anna_delegate`` are reserved
        for in-process servers that must never be sub-agent-mountable. An
        operator keying a stdio/http registry entry under one of those names
        would mount an external server under a reserved name; fail fast at
        config-load so the operator gets immediate feedback rather than a
        silent resolution-time drop. The forbidden set is sourced from
        grants.py (single source of truth — avoid drift).
        """
        # Imported lazily: grants.py imports from config.py, so a module-level
        # import here would be circular.
        from anna.runtime.grants import FORBIDDEN_BUILTINS

        collisions = sorted(set(self.mcp_registry) & FORBIDDEN_BUILTINS)
        if collisions:
            raise ValueError(
                f"subagents.mcp_registry keys collide with reserved builtin "
                f"names: {', '.join(collisions)}. These names are reserved "
                f"for in-process servers and must not be sub-agent-mountable."
            )
        return self


class SchedulerConfig(BaseModel):
    """Phase 2 scheduler. Fires scheduled prompts through the worker pool.

    Schedules persist to ``state_path`` as YAML. The scheduler coroutine
    polls every ``poll_interval_seconds`` for schedules whose next-fire
    time has passed and dispatches a synthetic ``InboundEvent`` through
    the conversation router. Output routes to the per-schedule
    destination (a non-admin Slack channel or Telegram chat). Three
    consecutive failures auto-disables the schedule and alerts admin.

    See Inbox/2026-06-01-ANNA-Phase-2-Scheduler-Buildout-Plan.md for the
    full design.
    """

    enabled: bool = True
    state_path: str = "~/anna/schedules.yaml"
    default_timeout_seconds: int = 300
    max_concurrent: int = 3
    poll_interval_seconds: int = 30
    failure_threshold: int = 3

    @property
    def resolved_state_path(self) -> Path:
        return Path(os.path.expanduser(self.state_path))


class WebDashboardConfig(BaseModel):
    """Phase 2.5 localhost-only FastAPI dashboard.

    A separate user systemd unit (``anna-web.service``) running a small
    FastAPI app that gives the operator form-based editors for
    ``anna.yaml``, ``.env``, and ``schedules.yaml`` plus a one-button
    restart of the main daemon. The dashboard runs out-of-process and
    never mutates the running daemon's in-memory state; edits land on
    disk and the operator presses Restart.

    The auth boundary is ``127.0.0.1`` + filesystem permissions on
    ``~/anna/.env`` — the same posture the CLI transport's
    Unix-socket adapter takes. Remote access is the operator's
    reverse-proxy problem (Caddy, Tailscale, SSH tunnel); ANNA does
    not ship a login UI in v1.

    * ``enabled`` — default-on. The wizard offers an opt-out prompt
      and ``anna-setup --disable-web`` flips this without an
      interactive session.
    * ``host`` — bind address. Default ``127.0.0.1`` keeps the port
      off the network. Operators who front the dashboard with a
      reverse proxy on the same box can leave this as-is and proxy
      to localhost.
    * ``port`` — TCP port. 8765 was picked to sit well clear of the
      common dev-server range (3000/5000/8000/8080).
    * ``target_unit`` — the systemd user unit the Restart button
      acts on. Pinned at config time, never accepted from a request
      body, so there is no path for a crafted POST to restart an
      arbitrary unit.

    See Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md for the full
    design.
    """

    enabled: bool = True
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address. The dashboard ships no authentication, so keep this "
            "at 127.0.0.1 unless it sits behind a trusted reverse proxy or VPN — "
            "a non-loopback bind exposes the editors to your LAN/network."
        ),
        # Surfaced by the web config editor: a non-loopback value renders a
        # loud inline warning next to this field (anna_web.config_field.html).
        # Reuses the same json_schema_extra channel as the textarea widget hint
        # rather than inventing a parallel metadata path.
        json_schema_extra={"warn_if_non_loopback": True},
    )
    port: int = 8765
    target_unit: str = "anna.service"

    @field_validator("port")
    @classmethod
    def _port_in_range(cls, v: int) -> int:
        if v < 1 or v > 65535:
            raise ValueError("web.port must be between 1 and 65535")
        return v


class VoiceInboundConfig(BaseModel):
    """Inbound voice (speech-to-text) config.

    Phase 2.5 voice messages. When ``enabled`` is true and a Slack or
    Telegram inbound carries an audio payload, the adapter downloads the
    file, calls the configured transcription provider, and substitutes the
    returned transcript into the ``InboundEvent.text`` prefixed by a
    ``[voice transcript]:`` marker.

    * ``provider`` — ``whisper-openai`` (cloud, default) or
      ``faster-whisper-local`` (local model behind the ``voice-local``
      extras group). Providers are explicitly named; there is no plugin
      discovery.
    * ``api_key_env`` — env var holding the provider key (ignored by the
      local provider).
    * ``model`` — provider-specific model id (``whisper-1`` for OpenAI).
    * ``keep_audio_files`` — when true, persist the downloaded audio under
      ``$ANNA_HOME/transcripts/voice/`` so the operator can re-listen or
      re-transcribe; when false the transcribe call runs against a
      tempfile that is unlinked immediately after the call returns.
    * ``max_duration_seconds`` / ``max_audio_size_bytes`` — hard caps;
      clips over either are rejected before the provider call.
    * ``hint_language`` — language hint passed to the provider; null lets
      the provider auto-detect.
    * ``timeout_seconds`` — per-transcribe ``asyncio.wait_for`` bound.
    * ``retry_attempts`` — extra attempts on transient HTTP 5xx / timeout
      only; 4xx and unsupported-codec errors do not retry.
    """

    enabled: bool = True
    provider: Literal["whisper-openai", "faster-whisper-local"] = "whisper-openai"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "whisper-1"
    keep_audio_files: bool = True
    max_duration_seconds: int = 600
    max_audio_size_bytes: int = 26_214_400  # 25 MB (OpenAI cap)
    hint_language: str | None = "en"
    timeout_seconds: int = 60
    retry_attempts: int = 2

    @field_validator("max_duration_seconds")
    @classmethod
    def _duration_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("voice.inbound.max_duration_seconds must be > 0")
        return v

    @field_validator("max_audio_size_bytes")
    @classmethod
    def _size_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("voice.inbound.max_audio_size_bytes must be > 0")
        return v

    @field_validator("retry_attempts")
    @classmethod
    def _retries_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("voice.inbound.retry_attempts must be >= 0")
        return v


def _default_voice_transports() -> list[Literal["slack", "telegram"]]:
    """Default outbound-voice allowlist: both buffered transports.

    A named helper (rather than an inline lambda) so the return type is
    the Literal list mypy expects for the field's default_factory.
    """
    return ["slack", "telegram"]


class VoiceOutboundConfig(BaseModel):
    """Outbound voice (text-to-speech) config.

    Phase 2.5 voice messages. When ``enabled`` is true and the worker's
    reply targets a conv_key whose most-recent inbound was voice (within
    ``recent_voice_window_seconds``), the adapter synthesizes the reply
    and posts it as a voice file. Off by default per-transport via the
    ``transports`` allowlist.

    * ``provider`` — ``openai-tts`` (the only provider that ships in v1).
    * ``voice_id`` — provider voice id (``alloy`` is neutral, operator
      tunable).
    * ``voice_only`` — voice-in produces voice-only-out; text-in still
      produces text-out.
    * ``recent_voice_window_seconds`` — TTL on the per-conv_key
      "last inbound was voice" cache.
    * ``max_synthesis_chars`` — replies longer than this fall through to
      text-only.
    """

    enabled: bool = True
    transports: list[Literal["slack", "telegram"]] = Field(
        default_factory=lambda: _default_voice_transports()
    )
    provider: Literal["openai-tts"] = "openai-tts"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "tts-1"
    voice_id: str = "alloy"
    voice_only: bool = True
    recent_voice_window_seconds: int = 600
    max_synthesis_chars: int = 4000
    timeout_seconds: int = 30
    # Per-transport output container. Slack renders an .mp3 upload with an
    # inline audio player; Telegram's send_voice wants Opus-in-OGG. Any
    # transport missing here falls back to opus.
    formats: dict[str, Literal["opus", "mp3"]] = Field(
        default_factory=lambda: {"slack": "mp3", "telegram": "opus"}
    )

    @field_validator("recent_voice_window_seconds")
    @classmethod
    def _window_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("voice.outbound.recent_voice_window_seconds must be > 0")
        return v

    @field_validator("max_synthesis_chars")
    @classmethod
    def _max_chars_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("voice.outbound.max_synthesis_chars must be > 0")
        return v


class VoiceConfig(BaseModel):
    """Phase 2.5 voice-messages top-level block.

    Voice is a runtime-level capability the Slack and Telegram adapters
    consume: inbound audio is transcribed upstream of the router so the
    worker sees a normal text event, and outbound replies are optionally
    synthesized back to a voice note. The CLI transport, scheduler, and
    sub-agent runtime are voice-agnostic and unaffected.

    Both sub-blocks default to a sensible enabled state so an existing
    config without a ``voice:`` section still validates with voice on;
    the operator flips ``inbound.enabled`` / ``outbound.enabled`` to opt
    out. See Inbox/2026-06-02-ANNA-Voice-Messages-Plan.md for the full
    design.
    """

    inbound: VoiceInboundConfig = Field(default_factory=VoiceInboundConfig)
    outbound: VoiceOutboundConfig = Field(default_factory=VoiceOutboundConfig)


class ImagesInboundConfig(BaseModel):
    """Inbound image-understanding config.

    A dragged-in image on Slack arrives as a ``files[]`` entry. When
    ``enabled`` the adapter downloads each accepted image and carries the
    raw bytes on the ``InboundEvent`` so the worker can hand them to the
    model as base64 image content blocks.

    * ``max_images`` — cap on images attached to a single turn; overflow
      is skipped with an operator-facing marker.
    * ``max_image_size_bytes`` — per-image hard cap (checked against the
      Slack ``size`` field before download and against the downloaded
      byte length after).
    * ``max_total_bytes`` — aggregate cap across all images on the turn.
    """

    enabled: bool = True
    max_images: int = 8
    max_image_size_bytes: int = 5_242_880  # 5 MB per image
    max_total_bytes: int = 20_971_520  # 20 MB total per turn

    @field_validator("max_images")
    @classmethod
    def _max_images_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("images.inbound.max_images must be > 0")
        return v

    @field_validator("max_image_size_bytes")
    @classmethod
    def _image_size_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("images.inbound.max_image_size_bytes must be > 0")
        return v

    @field_validator("max_total_bytes")
    @classmethod
    def _total_bytes_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("images.inbound.max_total_bytes must be > 0")
        return v


class ImagesConfig(BaseModel):
    """Top-level image-understanding block.

    Like voice, images are a runtime-level capability the Slack adapter
    consumes upstream of the router. A config without an ``images:``
    section validates with image understanding on; the operator flips
    ``inbound.enabled`` to opt out. Telegram and the CLI transport are
    image-agnostic for now and unaffected.
    """

    inbound: ImagesInboundConfig = Field(default_factory=ImagesInboundConfig)


class ObsidianIntegrationConfig(BaseModel):
    """Optional Obsidian-vault integration (default: fully off).

    First registration of the optional-integration gating pattern
    (Inbox/2026-06-10-anna-web-mission-control-plan.md, subtask 8).
    With the defaults below, anna-web renders no vault-touching UI at
    all: no Tasks nav entry, ``/tasks`` is never mounted (404), and the
    TaskNote reader reports unavailable. Flipping ``enabled`` +
    ``tasknotes_enabled`` (and restarting anna-web) surfaces the
    read-only TaskNote pipeline board.

    * ``enabled`` — master gate for the whole Obsidian integration.
    * ``vault_path`` — root of the operator's Obsidian vault.
    * ``tasknotes_enabled`` — sub-gate for the TaskNote board; the
      board needs BOTH this and ``enabled`` true.
    * ``tasknotes_path`` — directory the TaskNote markdown files live
      in (e.g. ``~/Obsidian/Brain/TaskNotes/Tasks``).

    Like the rest of anna.yaml there is no hot-reload; changes apply on
    the next service restart.
    """

    enabled: bool = False
    vault_path: Path | None = None
    tasknotes_enabled: bool = False
    tasknotes_path: Path | None = None

    @property
    def resolved_vault_path(self) -> Path | None:
        """``vault_path`` with ``~`` expanded; ``None`` stays ``None``."""
        return self.vault_path.expanduser() if self.vault_path else None

    @property
    def resolved_tasknotes_path(self) -> Path | None:
        """``tasknotes_path`` with ``~`` expanded; ``None`` stays ``None``."""
        return self.tasknotes_path.expanduser() if self.tasknotes_path else None


class IntegrationsConfig(BaseModel):
    """Optional third-party integrations, every one default-off.

    Top-level (not nested under ``web:``) so future daemon features can
    read the same gates — see mission-control plan Open Q3. The shared
    contract: a vanilla deploy with no ``integrations:`` block behaves
    exactly as if the block were absent — no extra nav, routes, or
    readers. anna-web's :mod:`anna_web.integrations` registry is the
    consumer that maps each gate onto nav entries / route mounting /
    reader availability.
    """

    obsidian: ObsidianIntegrationConfig = Field(
        default_factory=ObsidianIntegrationConfig
    )


class IdentityAliasEntry(BaseModel):
    """Phase 2 §5 identity alias.

    Maps one or more per-transport identifiers (Slack user ID, Telegram
    chat ID, CLI OS username) onto a single canonical name. When the
    router sees an inbound event whose per-transport identifier matches,
    it rewrites the conv_key from the per-transport shape (e.g.
    ``slack:dm:USP2QLB41``) to ``user:<canonical>`` so checkpoints and
    resume context land in one place across transports.

    ``canonical`` is restricted to a-z, 0-9, underscore because it
    becomes part of the conv_key and the on-disk checkpoint directory
    name. Same restriction as schedule.id and google.slug.

    See Inbox/2026-06-01-ANNA-Phase-2-CLI-Transport-Plan.md "Identity
    aliasing" for the full design and the no-auto-migration tradeoff.
    """

    canonical: str
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    cli_username: str | None = None

    @field_validator("canonical")
    @classmethod
    def _safe(cls, v: str) -> str:
        if not v:
            raise ValueError("identity.canonical cannot be empty")
        bad = [c for c in v if not (c.isalnum() or c == "_")]
        if bad:
            raise ValueError(
                f"identity.canonical must be a-z, 0-9, underscore only; "
                f"got disallowed chars: {''.join(sorted(set(bad)))}"
            )
        return v


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class AnnaConfig(BaseModel):
    auth: AuthConfig = Field(default_factory=AuthConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    transports: TransportsConfig = Field(default_factory=TransportsConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    housekeeping: HousekeepingConfig = Field(default_factory=HousekeepingConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    subagents: SubagentsConfig = Field(default_factory=SubagentsConfig)
    web: WebDashboardConfig = Field(default_factory=WebDashboardConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    identities: list[IdentityAliasEntry] = Field(default_factory=list)

    # Derived runtime paths. Not in the YAML file. ANNA_HOME from .env wins,
    # falling back to ~/anna (via _resolve_anna_home, the single source of
    # truth). The setup wizard always writes ANNA_HOME. The sub-path
    # properties below (audit_dir, transcripts_dir, ...) all derive from
    # this field, so they inherit the consolidated resolution too.
    anna_home: Path = Field(default_factory=_resolve_anna_home)

    @model_validator(mode="after")
    def _check_unique_canonical(self) -> "AnnaConfig":
        names = [i.canonical for i in self.identities]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(
                f"duplicate identities.canonical: {', '.join(dupes)}"
            )
        return self

    @property
    def audit_dir(self) -> Path:
        return self.anna_home / "audit"

    @property
    def transcripts_dir(self) -> Path:
        return self.anna_home / "transcripts"

    @property
    def subagent_transcript_dir(self) -> Path:
        """Per-sub-agent transcript root.

        Sub-agent runs coalesce under
        ``$ANNA_HOME/transcripts/<subagents.transcript_subdir>/<slug>/``
        rather than the per-conv_key tree the main transcript writer uses
        — see Inbox/2026-06-01-ANNA-Phase-2-Subagent-Runtime-Plan.md.
        """
        return self.transcripts_dir / self.subagents.transcript_subdir

    @property
    def voice_dir(self) -> Path:
        """Per-conversation voice-audio root.

        Inbound voice notes persist under
        ``$ANNA_HOME/transcripts/voice/<safe(conv_key)>/<msg_id>.<ext>``
        when ``voice.inbound.keep_audio_files`` is true. Treated as a
        transcript artifact and swept on the transcript retention
        schedule — see Inbox/2026-06-02-ANNA-Voice-Messages-Plan.md.
        """
        return self.transcripts_dir / "voice"

    @property
    def core_dir(self) -> Path:
        return self.anna_home / "core"

    @property
    def state_dir(self) -> Path:
        """Runtime state files (clean-shutdown sentinel, etc.)."""
        return self.anna_home / "state"

    @property
    def claude_runtime_dir(self) -> Path:
        """Isolated CLAUDE_CONFIG_DIR for ANNA's spawned CLI subprocesses.

        Relocates host Claude Code discovery (CLAUDE.md / skills / plugins /
        local MCP under ~/.claude) off the operator's dir. Credentials are NOT
        seeded here; they are resolved separately via
        ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` (see
        :meth:`claude_securestorage_dir`) so max-mode OAuth refresh writes land
        on the operator's shared ~/.claude/.credentials.json.
        """
        return self.anna_home / ".claude-runtime"

    @property
    def claude_securestorage_dir(self) -> Path:
        """Operator's real ``~/.claude`` dir for ``CLAUDE_SECURESTORAGE_CONFIG_DIR``.

        The bundled CLI resolves the credentials directory from this env var
        independently of ``CLAUDE_CONFIG_DIR``. Pointing it at the operator's
        real ~/.claude lets max-mode credential reads and the OAuth refresh
        (temp-file + rename) operate directly on the shared
        ``.credentials.json`` rather than a relocated symlink. Derived from the
        same source of truth as the auth layer so the value is never
        duplicated.
        """
        from anna.auth import operator_securestorage_dir

        return operator_securestorage_dir()

    @property
    def google_state_dir(self) -> Path:
        """Per-account Google credentials and refresh tokens."""
        return self.state_dir / "google"

    def resolve_google_credentials_path(self, account: "GoogleAccountConfig") -> Path:
        """Resolve a per-account credentials_file against anna_home if relative."""
        raw = Path(os.path.expanduser(account.credentials_file))
        if raw.is_absolute():
            return raw
        return self.anna_home / raw

    def google_token_path(self, account: "GoogleAccountConfig") -> Path:
        """Per-OAuth-account refresh-token cache path."""
        return self.google_state_dir / f"token_{account.slug}.json"


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
    candidate = _resolve_anna_home() / "anna.yaml"
    if candidate.is_file():
        return candidate
    return Path("anna.yaml")


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Layer env-var overrides on top of the parsed YAML.

    The ones we honor explicitly are ANNA_LOG_LEVEL, ANNA_LOG_FORMAT, and
    ANNA_RUNTIME_EFFORT, which the .env file documents as override knobs.
    Anything else in the YAML is treated as authoritative.
    """
    level = os.environ.get("ANNA_LOG_LEVEL")
    if level:
        data.setdefault("logging", {})["level"] = level

    fmt = os.environ.get("ANNA_LOG_FORMAT")
    if fmt:
        data.setdefault("logging", {})["format"] = fmt

    # Reasoning-effort override for the main loop. Same precedence as
    # ANNA_LOG_LEVEL: the env var, when set, wins over the YAML value.
    # Validation (low|medium|high|xhigh|max) happens downstream in
    # RuntimeConfig, so a garbage env value fails config load loudly.
    effort = os.environ.get("ANNA_RUNTIME_EFFORT")
    if effort:
        data.setdefault("runtime", {})["effort"] = effort

    # Admin destinations fall back to .env so the operator can keep the
    # channel IDs out of anna.yaml if they prefer.
    admin_slack = os.environ.get("ANNA_ADMIN_SLACK_CHANNEL_ID")
    admin_tg = os.environ.get("ANNA_ADMIN_TELEGRAM_CHAT_ID")
    if admin_slack or admin_tg:
        admin = data.setdefault("admin", {})
        if admin_slack and not admin.get("slack_channel_id"):
            admin["slack_channel_id"] = admin_slack
        if admin_tg and not admin.get("telegram_chat_id"):
            admin["telegram_chat_id"] = admin_tg

    # Reports destination falls back to .env so the operator can keep the
    # threat-report channel ID out of anna.yaml if they prefer.
    reports_slack = os.environ.get("ANNA_REPORTS_SLACK_CHANNEL_ID")
    if reports_slack:
        reports = data.setdefault("reports", {})
        if not reports.get("slack_channel_id"):
            reports["slack_channel_id"] = reports_slack

    return data


def load_config(path: Path | None = None) -> AnnaConfig:
    """Read .env and anna.yaml, return a validated config object."""
    # .env first, so anna.yaml's env overrides see the right values.
    # Be explicit about the dotenv path: python-dotenv's default upward
    # search walks from this source file, which no longer lives under
    # ~/anna/ after the uv-tool install migration. ANNA_HOME comes from
    # the systemd unit's Environment= line and is the canonical anchor
    # (resolved via _resolve_anna_home, the single source of truth);
    # fall back to the legacy upward search for dev/test runs that don't
    # set it — deliberately NOT _resolve_anna_home()'s ~/anna default,
    # which would pull the operator's real .env into isolated test envs.
    # override=False is load-bearing either way: a var already in the
    # process environment always wins over the file.
    if os.environ.get("ANNA_HOME"):
        load_dotenv(dotenv_path=_resolve_anna_home() / ".env", override=False)
    else:
        load_dotenv(override=False)

    config_path = path or _resolve_config_path()
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp) or {}
    else:
        raw = {}

    raw = _apply_env_overrides(raw)
    return AnnaConfig.model_validate(raw)
