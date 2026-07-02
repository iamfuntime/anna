"""Per-conversation worker.

Per v3 section 6. One async worker per active conversation_key, owning one
:class:`claude_agent_sdk.ClaudeSDKClient`. The worker reads events from an
``asyncio.Queue``, dispatches them through the SDK, and writes a vault
checkpoint when it idles out.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from anna.agents.registry import SubAgentRegistry
from anna.config import AnnaConfig
from anna.core.eviction import evict_if_over_cap
from anna.core.identity import CORE_FILES, CoreFile, read_core_file
from anna.log import audit_event, get_logger
from anna.skills.registry import SkillRegistry
from anna.tools.delegate_server import DELEGATE_TOOL_NAMES, build_delegate_server
from anna.tools.google_server import GOOGLE_TOOL_NAMES, GoogleTools, build_google_server
from anna.tools.self_edit_server import SELF_EDIT_TOOL_NAMES, SelfEditTools, build_self_edit_server
from anna.tools.slack_alerts_server import (
    SLACK_ALERTS_TOOL_NAMES,
    SlackAlertTools,
    build_slack_alerts_server,
)
from anna.tools.vault_tools import VaultTools
from anna.tools.web_server import WEB_TOOL_NAMES, build_web_server
from anna.tools.web_tools import WebTools
from anna.runtime.visibility import NULL_VISIBILITY, VisibilityCallbacks
from anna.transports.base import (
    ChannelAdapter,
    ImageAttachment,
    InboundEvent,
    OutboundMessage,
    SignalHandle,
)
from anna.vault.checkpoint import list_recent_checkpoints, write_checkpoint
from anna.vault.transcript_resume import (
    latest_checkpoint_mtime,
    render_tail_block,
    transcript_tail_since,
)

if TYPE_CHECKING:
    from anna.runtime.alerter import AdminAlerter
    from anna.runtime.schedule_store import ScheduleStore
    from anna.runtime.subagent import SubAgentRunner
    from anna.runtime.supervisor import Supervisor
    from anna.tools.google_clients import GoogleClients


# Default file-system tools we hand to ANNA so she can read and write her
# vault. Listed by their canonical SDK names. The MCP self-edit tools are
# prefixed with ``mcp__anna_self_edit__`` per the SDK convention.
_DEFAULT_FS_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")
_SELF_EDIT_PREFIX = "mcp__anna_self_edit__"
_SLACK_ALERTS_PREFIX = "mcp__anna_slack_alerts__"
_GOOGLE_PREFIX = "mcp__anna_google__"
_WEB_PREFIX = "mcp__anna_web__"
_DELEGATE_PREFIX = "mcp__anna_delegate__"


def _tool_belongs_to_servers(tool_name: str, server_names: set[str]) -> bool:
    """True if ``tool_name`` is namespaced to one of ``server_names``.

    Registry tool names follow the SDK ``mcp__<server>__<tool>`` (or the
    server-namespace wildcard ``mcp__<server>__*``) convention. We parse the
    server segment so tool-name additions for a server we skipped (e.g. a
    builtin-colliding registry entry) are not silently allowlisted.
    """
    if not tool_name.startswith("mcp__"):
        return False
    rest = tool_name[len("mcp__") :]
    server, _, _ = rest.partition("__")
    return server in server_names


def _allowed_tool_names(
    *,
    include_google: bool,
    include_web: bool,
    include_delegate: bool = False,
) -> list[str]:
    names = (
        list(_DEFAULT_FS_TOOLS)
        + [f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES]
        + [f"{_SLACK_ALERTS_PREFIX}{name}" for name in SLACK_ALERTS_TOOL_NAMES]
    )
    if include_google:
        names.extend(f"{_GOOGLE_PREFIX}{name}" for name in GOOGLE_TOOL_NAMES)
    if include_web:
        names.extend(f"{_WEB_PREFIX}{name}" for name in WEB_TOOL_NAMES)
    if include_delegate:
        names.extend(f"{_DELEGATE_PREFIX}{name}" for name in DELEGATE_TOOL_NAMES)
    return names


# Runtime guard against leaked, unparsed tool-call markup reaching a
# transport (incident: a degraded model turn posted literal
# ``<invoke name="Bash">…</invoke>`` XML and a bare
# ``mcp__anna_google__gmail_list_unread`` line to Slack). The patterns are
# deliberately structural so prose that merely mentions ``<invoke>`` in
# backticks (no ``name=`` attribute) or an inline ``mcp__…`` tool name
# mid-sentence (not on its own line) does NOT match — keeping false
# positives near zero. Each pattern below is one strong, self-sufficient
# marker of leaked function-call syntax.
# The lone OPENING-invoke tag is the one "weak" marker: by itself (no closing
# tag, no parameter tag, no second distinct marker) it is the fragment that
# legitimately shows up when ANNA quotes/explains markup mid-prose, or trails a
# turn that DID make a real structured tool call. Every other marker below is
# independently "strong" (a closing tag, a parameter tag, the function_calls
# wrappers, or a bare whole-line mcp tool name), and any two distinct markers
# together are strong regardless.
_OPEN_INVOKE_PATTERN: re.Pattern[str] = re.compile(
    r"<(?:antml:)?invoke\b[^>]*\bname\s*=", re.IGNORECASE
)
_TOOLCALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    _OPEN_INVOKE_PATTERN,
    re.compile(r"</(?:antml:)?invoke>", re.IGNORECASE),
    re.compile(r"<(?:antml:)?parameter\b[^>]*\bname\s*=", re.IGNORECASE),
    re.compile(r"</?(?:antml:)?function_calls>", re.IGNORECASE),
    re.compile(r"(?m)^\s*mcp__[a-z0-9_]+__[a-z0-9_]+\s*$", re.IGNORECASE),
)

# Code-span strippers, applied BEFORE the markup patterns. A genuine leaked
# tool call is always BARE — the degraded turn is trying to *invoke*, so the
# markup arrives unquoted (incident text: ``court\n<invoke name="Bash">``).
# Legitimate prose that discusses the syntax (e.g. a reply explaining why a
# message was suppressed) always wraps it in backticks. Stripping fenced and
# inline code spans first therefore kills the false-positive cascade — where
# an explanation that quotes ``<invoke name=…>`` got suppressed, fired an
# admin alert, prompted another question, and looped — without weakening the
# bare-leak catch. Fenced blocks are stripped before inline spans so a ```
# fence is never half-consumed by the single-backtick pass.
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _strip_code_spans(text: str) -> str:
    """Remove fenced and inline code spans so quoted markup is not scanned."""
    return _INLINE_CODE.sub(" ", _FENCED_CODE.sub(" ", text))


def _contains_unparsed_toolcall_markup(text: str) -> bool:
    """True if ``text`` contains any leaked, unparsed tool-call marker.

    Returns ``False`` for empty/``None`` text. Code spans (backtick-wrapped)
    are stripped first — see ``_strip_code_spans`` and ``_TOOLCALL_PATTERNS``
    for the rationale behind the structural (low-false-positive) markers.
    """
    if not text:
        return False
    scannable = _strip_code_spans(text)
    return any(pattern.search(scannable) for pattern in _TOOLCALL_PATTERNS)


def _matched_markers(text: str) -> list[str]:
    """Return the source-pattern strings that matched ``text`` (for audit)."""
    if not text:
        return []
    scannable = _strip_code_spans(text)
    return [pattern.pattern for pattern in _TOOLCALL_PATTERNS if pattern.search(scannable)]


def _markup_is_strong(text: str) -> bool:
    """True if the leaked markup in ``text`` is a STRONG signal of a tool call
    emitted as prose (rather than a stray/partial fragment).

    Strong means either (a) two or more DISTINCT markers matched (e.g. an
    opening invoke tag AND a parameter tag, or a paired open/close), or (b) any
    single marker OTHER than the lone opening-invoke tag matched (a closing
    tag, a parameter tag, the function_calls wrappers, or a bare whole-line mcp
    tool name — each self-sufficient evidence of a full leak). The single
    lone opening-invoke fragment is the only WEAK case.
    """
    if not text:
        return False
    scannable = _strip_code_spans(text)
    matched = [p for p in _TOOLCALL_PATTERNS if p.search(scannable)]
    if not matched:
        return False
    if len(matched) >= 2:
        return True
    # Exactly one distinct marker: strong unless it is the lone opening-invoke.
    return matched[0] is not _OPEN_INVOKE_PATTERN


def _should_suppress_markup(text: str, *, tool_used: bool = False) -> bool:
    """Suppression decision for ``text`` given whether a real tool call ran.

    Suppress when the markup is STRONG, OR when it is weak (a lone partial
    opening-invoke fragment) AND no real structured tool call executed this
    turn. Equivalently: a weak/partial fragment on a turn that DID execute a
    real tool call is delivered. ``tool_used`` defaults to ``False`` so callers
    with no notion of tool execution (e.g. the scheduler defense-in-depth pass)
    keep the strict, fail-closed behavior.
    """
    if not text:
        return False
    if not _contains_unparsed_toolcall_markup(text):
        return False
    if _markup_is_strong(text):
        return True
    # Weak fragment: deliver only when a real tool call actually executed.
    return not tool_used


SendCallback = Callable[[OutboundMessage], Awaitable[None]]
IdleCloseCallback = Callable[[str], Awaitable[None]]


@dataclass
class _FlushBuffer:
    """Per-turn pending-narration holder shared by the consumer loop and the
    periodic-flush timer task (Inbox/2026-06-04 plan, Architecture section).

    ``pending`` accumulates the text blocks emitted since the last flush
    boundary (tool-use OR timed drip). Both the consumer ``async for`` loop
    and the background timer task mutate ``pending`` IN PLACE (``extend`` /
    ``clear``) under ``lock`` — never rebind it — so both see the same list.
    ``last_flush`` is a ``loop.time()`` monotonic stamp written by whichever
    path last sent a message, so the timer measures its interval since the
    last message of ANY kind (decision B).

    Scoped to a single ``_handle`` invocation; never long-lived instance
    state. ``last_flush`` is seeded to the turn-start ``loop.time()`` so the
    first drip cannot fire before a full interval has elapsed.
    """

    last_flush: float
    pending: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationWorker:
    """An async worker that owns one Claude SDK session for one conversation."""

    def __init__(
        self,
        *,
        conversation_key: str,
        transport: str,
        config: AnnaConfig,
        supervisor: "Supervisor",
        send: SendCallback,
        on_idle_close: IdleCloseCallback | None = None,
        adapters: dict[str, ChannelAdapter] | None = None,
        schedule_store: "ScheduleStore | None" = None,
        google_clients: "GoogleClients | None" = None,
        subagent_runner: "SubAgentRunner | None" = None,
        ephemeral: bool = False,
        visibility: VisibilityCallbacks = NULL_VISIBILITY,
        alerter: "AdminAlerter | None" = None,
    ) -> None:
        self.conversation_key = conversation_key
        self.transport = transport
        self._config = config
        self._supervisor = supervisor
        self._send = send
        self._on_idle_close = on_idle_close
        # Live transport adapters (same dict the router/alerter hold). Used by
        # the anna_slack_alerts MCP server to post through ANNA's own Slack
        # adapter. Defaults to an empty dict for standalone unit tests; the
        # slack_post tool returns an error string when "slack" is absent.
        self._adapters: dict[str, ChannelAdapter] = adapters or {}
        self._schedule_store = schedule_store
        self._google_clients = google_clients
        self._subagent_runner = subagent_runner
        # Out-of-band operator alerter (same instance the router/watchdog
        # hold). Used by the runtime tool-call-markup guard to fire a
        # best-effort admin alert when a degraded turn leaks unparsed
        # function-call syntax. ``None`` in standalone unit tests; the guard
        # then audits + logs but skips the alert.
        self._alerter = alerter
        # Cadence-Visibility Hooks plan (Inbox/2026-06-02) subtask 5.
        # Default ``NULL_VISIBILITY`` means: no reminder prepend, no
        # thinking-signal start/clear, no lint pass. Existing unit tests
        # and the sub-agent path are unchanged.
        self._visibility = visibility
        # Worker-level periodic-flush interval (Inbox/2026-06-04 plan).
        # Cached at construction to honor the no-hot-reload contract: an
        # anna.yaml edit takes effect on the next restart, not mid-process.
        # ``0`` (or negative, already rejected at load) disables the timed
        # drip — the timer task is simply never started for any turn.
        self._flush_interval: int = config.runtime.visibility.periodic_flush_seconds
        # Phase 2 §5 subtask 7: when true the worker skips the checkpoint
        # write and the per-core-file eviction sweep at closeout. Set by
        # the router from the first event's ``ephemeral`` flag when the
        # CLI adapter spawns a one-shot (``cli:oneshot:<uuid>``) worker;
        # all other transports leave it false and keep the existing
        # checkpoint-on-close behavior.
        self._ephemeral = ephemeral
        self._log = get_logger("anna.worker").bind(conv_key=conversation_key, channel=transport)

        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue(maxsize=128)
        self._task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._client: object | None = None
        self._closed_out = False
        self._operator_short_name: str | None = None
        # Set true once the idle watcher has fired its close callback so we
        # do not race a second invocation against the in-flight stop().
        self._idle_close_signalled = False

        now = datetime.now(timezone.utc)
        self.last_active: datetime = now
        self.last_event_received_at: datetime | None = None
        self.last_event_processed_at: datetime | None = None

        # Periodic-checkpoint state (Fix 2). ``_created_at`` is the
        # wall-clock baseline used by the minutes trigger until the first
        # checkpoint is written. ``_turns_since_checkpoint`` and ``_dirty``
        # are advanced after each SUCCESSFUL turn in ``_run``; all three
        # reset on any checkpoint write (periodic or closeout).
        self._created_at: datetime = now
        self._turns_since_checkpoint: int = 0
        self._last_checkpoint_at: datetime | None = None
        self._dirty: bool = False
        self.is_dm: bool = conversation_key.split(":")[1].startswith("dm") if ":" in conversation_key else False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"worker.{self.conversation_key}")
        # The idle watcher runs only if the router gave us a close callback.
        # In standalone unit tests (no router) we skip it.
        if self._on_idle_close is not None:
            self._idle_task = asyncio.create_task(
                self._idle_watch(),
                name=f"worker.idle.{self.conversation_key}",
            )
        self._log.info("worker.spawn")

    async def stop(self) -> None:
        self._stopping = True
        # Cancel the idle watcher first so it cannot fire a redundant close
        # callback while stop() is mid-flight.
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):
                pass
            self._idle_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        # _closeout writes the checkpoint and runs eviction. It MUST run
        # before the SDK client is closed (eviction needs the client to
        # propose evictions). The flag guards against double-run if stop()
        # is called twice (e.g. idle-watcher and router shutdown race).
        if not self._closed_out and self._client is not None:
            try:
                await self._closeout()
            except Exception as exc:
                self._log.error("worker.closeout_failed", error=str(exc))
            finally:
                self._closed_out = True
        await self._close_client()
        self._log.info("worker.complete")

    async def restart(self) -> None:
        await self.stop()
        self._stopping = False
        await self.start()

    async def submit(self, event: InboundEvent) -> None:
        self.last_event_received_at = datetime.now(timezone.utc)
        await self._queue.put(event)

    # ------------------------------------------------------------------
    # Inner loop
    # ------------------------------------------------------------------

    def _idle_gap_seconds(self) -> float:
        """Idle threshold for this worker, picking dm vs thread gap.

        CLI transports take precedence over the dm/thread split: the CLI
        conv_key shapes (``cli:local:<user>`` and the aliased
        ``user:<canonical>``) do not match the ``dm`` substring used by
        ``is_dm``, so without this branch they would default to the
        thread gap (1h). Phase 2 §5 wants ~30m on the CLI.
        """
        if self.transport == "cli":
            return self._config.transports.cli.idle_gap_minutes * 60.0
        cfg = self._config.sessions
        return (cfg.dm_gap_hours if self.is_dm else cfg.thread_gap_hours) * 3600.0

    async def _idle_watch(self) -> None:
        """Continuously check idle time and fire the close callback when due.

        Per the v3 spec, the watcher samples at quarter-gap granularity so
        a noon-silent DM does not wait until the 03:17 housekeeping sweep
        to close out. The watcher exits as soon as it triggers (the router
        will call stop(), which cancels this task).
        """
        gap = self._idle_gap_seconds()
        # Quarter-gap polling. Clamp to a sane floor so unit tests with a
        # 1-second gap still get a watcher that wakes promptly.
        poll = max(gap / 4.0, 0.05)
        try:
            while not self._stopping:
                await asyncio.sleep(poll)
                if self._stopping or self._idle_close_signalled:
                    return
                idle = (datetime.now(timezone.utc) - self.last_active).total_seconds()
                if idle > gap and self._on_idle_close is not None:
                    self._idle_close_signalled = True
                    self._log.info(
                        "worker.idle_close.trigger",
                        idle_seconds=idle,
                        gap_seconds=gap,
                    )
                    try:
                        await self._on_idle_close(self.conversation_key)
                    except Exception as exc:
                        self._log.error("worker.idle_close.callback_failed", error=str(exc))
                    return
        except asyncio.CancelledError:
            raise

    async def _run(self) -> None:
        try:
            await self._ensure_client()
            while not self._stopping:
                event = await self._queue.get()
                try:
                    await self._handle(event)
                finally:
                    self.last_event_processed_at = datetime.now(timezone.utc)
                    self.last_active = self.last_event_processed_at
                    # NOTE: the periodic-checkpoint bookkeeping
                    # (``_turns_since_checkpoint`` / ``_dirty``) is advanced
                    # inside ``_handle`` immediately after the SDK query is
                    # accepted — NOT here. That scopes it to turns that
                    # actually ran the query path: the ``_client is None``
                    # early return in ``_handle`` is a no-op that must not
                    # arm the periodic checkpoint. ``_handle`` swallows SDK
                    # receive/query errors internally (it still marked the
                    # turn dirty once the query was accepted), so those
                    # genuine turns are counted; only an exception that
                    # escapes ``_handle`` skips the bookkeeping, and that
                    # propagates and crashes the run loop anyway.
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error("worker.crashed", error=str(exc))
            raise

    def _format_rule(self) -> str:
        channel = self.transport
        if channel == "slack":
            return (
                "You are replying via Slack. Use plain text or Slack mrkdwn "
                "(*bold*, _italic_, `code`). Do not use GitHub-flavored "
                "Markdown tables, headings, or fenced code blocks with "
                "language hints; Slack will render them as literal characters."
            )
        if channel == "telegram":
            return (
                "You are replying via Telegram. The reply is sent as plain "
                "text (no parse_mode), so do not use any Markdown formatting. "
                "Plain prose, line breaks, and dashes only."
            )
        return "Reply in plain text."

    def _build_self_edit_tools(self) -> SelfEditTools:
        cfg = self._config
        agents_registry = SubAgentRegistry(
            supervisor=self._supervisor,
            agents_dir=cfg.anna_home / "agents",
            audit_dir=cfg.audit_dir,
            fsync_on_write=cfg.logging.audit.fsync_on_write,
        )
        skills_registry = SkillRegistry(
            supervisor=self._supervisor,
            skills_dir=cfg.anna_home / "skills",
            audit_dir=cfg.audit_dir,
            fsync_on_write=cfg.logging.audit.fsync_on_write,
        )
        return SelfEditTools(
            config=cfg,
            supervisor=self._supervisor,
            agents_registry=agents_registry,
            skills_registry=skills_registry,
            schedule_store=self._schedule_store,
        )

    def _build_slack_alert_tools(self) -> SlackAlertTools:
        return SlackAlertTools(self._config, self._adapters)

    def _build_claude_env(self) -> dict[str, str]:
        """Env overrides for the spawned bundled-CLI subprocess.

        ``CLAUDE_CONFIG_DIR`` relocates host CLAUDE.md / skills / plugins /
        local-MCP discovery onto ANNA's isolated runtime dir. In max mode we
        ALSO set ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` to the operator's real
        ~/.claude so credential reads and the OAuth refresh-write share the
        operator's ``.credentials.json``. In api_key mode the key comes from
        the inherited env, so the securestorage knob is left unset (mirroring
        how the old credentials symlink was max-mode-only).
        """
        env = {"CLAUDE_CONFIG_DIR": str(self._config.claude_runtime_dir)}
        if self._config.auth.mode == "max":
            env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(
                self._config.claude_securestorage_dir
            )
        return env

    def _build_options(self) -> Any:
        """Construct the ClaudeAgentOptions for this worker.

        Extracted from ``_ensure_client`` so the unit tests can introspect
        the option set (system prompt, MCP server, tools, cwd) without
        actually spawning an SDK client.
        """
        from claude_agent_sdk import ClaudeAgentOptions

        vault_root = self._config.vault.resolved_path
        anna_home = self._config.anna_home

        system_prompt = self._assemble_system_prompt(
            anna_home=anna_home,
            vault_root=vault_root,
            format_rule=self._format_rule(),
        )

        # Build the self-edit MCP server. The conv_key is captured by the
        # tool closures so each audit event is stamped with the right caller.
        self_edit_tools = self._build_self_edit_tools()
        self_edit_server = build_self_edit_server(
            tools=self_edit_tools,
            conv_key=self.conversation_key,
        )

        # Build the Google MCP server iff google integration is wired up
        # and the runtime gave us a GoogleClients handle. Workers spawned
        # in unit tests (no clients passed) and runs with google.enabled
        # false both fall through without the server.
        # FUTURE SEAM: ANNA's main session still builds its MCP surface
        # imperatively below (self_edit always, google/web/delegate
        # conditionally). The per-agent registry/pool model lives in
        # ``src/anna/runtime/grants.py`` (resolve_effective_grant +
        # build_mcp_servers) and currently drives only sub-agents. The
        # planned fast-follow migrates this main-session construction onto
        # the same registry so the operator can curate ANNA's own server
        # surface in anna.yaml. No behavior change here yet.
        # Build the Slack-alerts MCP server (slack_post). Mounted
        # unconditionally — it posts through ANNA's own Slack adapter so it
        # works in headless/scheduled runs. When the Slack transport is not
        # connected the tool returns an error string rather than failing the
        # mount, so there is no toggle.
        slack_alert_tools = self._build_slack_alert_tools()
        slack_alerts_server = build_slack_alerts_server(
            tools=slack_alert_tools,
            conv_key=self.conversation_key,
        )

        mcp_servers: dict[str, Any] = {
            "anna_self_edit": self_edit_server,
            "anna_slack_alerts": slack_alerts_server,
        }
        include_google = False
        if self._google_clients is not None and self._config.google.enabled:
            google_tools = GoogleTools(
                config=self._config,
                clients=self._google_clients,
            )
            google_server = build_google_server(
                tools=google_tools,
                conv_key=self.conversation_key,
            )
            mcp_servers["anna_google"] = google_server
            include_google = True

        # Build the Web MCP server (Brave web_search, httpx web_fetch,
        # vault_download) iff tools.enabled is true. Three pure in-process
        # tools, no external state — they slot in just like google.
        include_web = False
        if self._config.tools.enabled:
            web_tools = WebTools(config=self._config)
            vault_tools = VaultTools(config=self._config)
            web_server = build_web_server(
                config=self._config,
                web_tools=web_tools,
                vault_tools=vault_tools,
                conv_key=self.conversation_key,
            )
            if web_server is not None:
                mcp_servers["anna_web"] = web_server
                include_web = True

        # Phase 2 §3: mount anna_delegate iff subagents.enabled and the
        # runtime gave us a SubAgentRunner. The runner is process-wide;
        # each worker's closure captures only the conv_key so audit
        # events and sub-agent transcripts cite the originating
        # conversation. Sub-agents themselves never see this server —
        # the depth-protection invariant is enforced by simply not
        # mounting it on a sub-agent's options (see
        # SubAgentRunner._build_subagent_options).
        include_delegate = False
        if self._config.subagents.enabled and self._subagent_runner is not None:
            delegate_server = build_delegate_server(
                runner=self._subagent_runner,
                conv_key=self.conversation_key,
                config=self._config,
                conv_transport=self.transport,
            )
            if delegate_server is not None:
                mcp_servers["anna_delegate"] = delegate_server
                include_delegate = True

        # Resolve the operator's explicit main-loop MCP allowlist. The
        # registry (subagents.mcp_registry) is the same operator-blessed POLICY
        # pool sub-agents resolve against; subagents.anna_mcp_servers names the
        # subset ANNA herself mounts. We reuse the sub-agent conversion path
        # (build_mcp_servers) so external stdio/http specs and tool-name
        # additions are produced identically, and the forbidden-builtin guard
        # is shared — no special-casing here. Local import mirrors how
        # subagent.py pulls build_mcp_servers in to avoid an import cycle.
        from anna.runtime.grants import build_mcp_servers

        custom_specs: list[tuple[str, Any]] = []
        for name in self._config.subagents.anna_mcp_servers:
            spec = self._config.subagents.mcp_registry.get(name)
            if spec is None:
                self._log.warning("worker.mcp_registry.unknown", dropped_name=name)
                continue
            custom_specs.append((name, spec))

        # allowed_tools may need extending with the custom servers' tool names,
        # so capture the builtin list into a local first.
        allowed_tools = _allowed_tool_names(
            include_google=include_google,
            include_web=include_web,
            include_delegate=include_delegate,
        )
        if custom_specs:
            custom_servers, custom_tool_names = build_mcp_servers(
                self._config, custom_specs, self.conversation_key
            )
            # Merge without clobbering builtins: a registry entry whose key
            # collides with an already-mounted builtin (e.g. "anna_web") must
            # not silently replace it. Skip colliding names and only extend the
            # tool-name allowlist for the entries we actually add.
            added_servers: set[str] = set()
            for name, server in custom_servers.items():
                if name in mcp_servers:
                    self._log.warning(
                        "worker.mcp_registry.builtin_collision", name=name
                    )
                    continue
                mcp_servers[name] = server
                added_servers.add(name)
            # Dedupe with first-seen order so the option set is deterministic,
            # matching subagent.py _build_subagent_options. Only tool names that
            # belong to a server we actually mounted are eligible.
            for tool_name in custom_tool_names:
                if not _tool_belongs_to_servers(tool_name, added_servers):
                    continue
                if tool_name not in allowed_tools:
                    allowed_tools.append(tool_name)

        # Ensure the vault root exists before the SDK process tries to cd
        # into it; otherwise the first tool call fails with ENOENT.
        try:
            vault_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log.warning("worker.vault_mkdir_failed", error=str(exc))

        # Record the resolved main-loop model once so the operator can verify
        # which model the conversation runs on. Plain structured INFO log, not
        # a formal audit event — this is observational, not a security record.
        # None resolves to "<cli-default>" (the SDK's account default).
        self._log.info(
            "worker.model.resolved",
            model=self._config.runtime.model or "<cli-default>",
        )

        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            # setting_sources=[] disables inheriting the operator's user /
            # project / local Claude Code *settings.json* (the permission and
            # hook layer). It does NOT, on its own, stop the bundled CLI from
            # discovering host CLAUDE.md / agents / skills / plugins / local
            # MCP — that discovery is keyed off CLAUDE_CONFIG_DIR, which we
            # relocate via env below. Both together keep ANNA speaking strictly
            # from her own ~/anna/core files instead of impersonating the
            # operator's primary agent.
            setting_sources=[],
            # Relocate the bundled CLI's host discovery off the operator's
            # ~/.claude. CLAUDE_CONFIG_DIR is what the CLI walks for memory
            # (CLAUDE.md), skills, plugins, and local MCP; pointing it at the
            # isolated runtime dir stops ANNA inheriting the operator's entire
            # Claude Code environment. CLAUDE_SECURESTORAGE_CONFIG_DIR is a
            # SEPARATE knob the CLI uses to resolve the credentials dir; in
            # max mode we point it at the operator's real ~/.claude so OAuth
            # reads and the refresh-write (temp-file + rename) land directly on
            # the shared .credentials.json instead of clobbering a symlink.
            env=self._build_claude_env(),
            # ANNA runs as a headless systemd service with no operator at a
            # terminal to approve tool calls. The default permission_mode is
            # interactive prompting, which means every tool call hangs forever
            # waiting for an OK that never comes. The config default is
            # bypassPermissions; tighten in anna.yaml if needed.
            permission_mode=self._config.runtime.permission_mode,
            # Global-default Claude model for the main conversation loop. The
            # main loop does not go through resolve_effective_grant (it reads
            # runtime.* directly), so it takes the model straight off config.
            # None inherits the CLI/account default — today's behavior exactly.
            model=self._config.runtime.model,
            # Automatic fallback when the primary model is overloaded or
            # unavailable (incl. usage-cap exhaustion). None = no fallback
            # flag passed, a failed primary fails the turn (prior behavior).
            fallback_model=self._config.runtime.fallback_model,
            # In-process MCP servers. Dict keys become the MCP server
            # prefixes in the SDK's allowed_tools naming convention
            # (``mcp__<server>__<tool>``). anna_self_edit is always
            # mounted; anna_google only when google.enabled and the
            # runtime provided a GoogleClients handle; anna_web only
            # when tools.enabled.
            mcp_servers=mcp_servers,
            # Allow the default filesystem tools, the self-edit MCP tools,
            # the google/web/delegate MCP tools (when wired), and any
            # operator-allowlisted custom registry servers (resolved above).
            allowed_tools=allowed_tools,
            # Vault root is the natural cwd: vault paths become relative
            # (Conversations/foo.md instead of long absolutes).
            cwd=str(vault_root),
            # add_dirs lets the SDK see core/ as a readable workspace. ANNA
            # should still prefer the MCP tools for core writes because they
            # take the supervisor lock, but Read/Glob over core/ is fine and
            # is the only way she can quote her own files back to the
            # operator.
            # FUTURE SEAM: this hardcoded core/ mount is the main-session
            # equivalent of a resolved write-dir grant. When the main
            # session migrates onto the registry/pool model in
            # ``src/anna/runtime/grants.py``, this becomes a dir_pool entry
            # resolved through resolve_effective_grant. No behavior change
            # yet.
            add_dirs=[str(anna_home / "core")],
        )

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            from claude_agent_sdk import ClaudeSDKClient
        except ImportError as exc:
            self._log.critical("worker.sdk_import_failed", error=str(exc))
            raise

        options = self._build_options()
        # ClaudeSDKClient is an async context manager. We hold it open for the
        # life of the worker and close it in stop().
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        self._client = client

    def _assemble_system_prompt(
        self,
        *,
        anna_home: Path,
        vault_root: Path,
        format_rule: str,
    ) -> str:
        """Build the per-conversation system prompt from ANNA's five core files.

        Per v3 §6 (carrying forward v1's five Hermes-style core identity
        files), ANNA reads SOUL.md, CLAUDE.md, AGENTS.md, MEMORY.md, and
        IDENTITY.md on every conversation boot. Their contents are spliced
        into the system prompt verbatim, in a stable order, with a leading
        scope disclaimer so ANNA never confuses herself with the operator's
        primary Claude Code agent or any other agent in the operator's
        roster. If a file is missing or empty (fresh install before the
        persona interview has been run), it is rendered as "(not yet
        written)" so ANNA can tell the operator what to populate.
        """
        core_dir = anna_home / "core"

        def _section(file: CoreFile, heading: str) -> str:
            body = read_core_file(core_dir, file).strip()
            if not body:
                body = "(not yet written — operator should run `anna-setup --persona`)"
            return f"## {heading}\n{body}"

        scope = (
            "You are ANNA, an independent personal AI agent with your own "
            "identity, memory, and vault. You are NOT the operator's primary "
            "Claude Code session and NOT a member of the operator's Vanguard "
            "agent roster. Do not reference the operator's other agents, "
            "their vault, their slash commands, or their CLAUDE.md unless "
            "the operator explicitly asks about them. Your five core "
            "identity files below are the authoritative source for who you "
            "are; do not improvise persona content beyond what they say."
        )

        runtime = (
            f"Your runtime root (anna_home) is {anna_home}. core/ holds the "
            f"five identity files below; audit/, transcripts/, anna.yaml, "
            f"and .env live alongside.\n"
            f"Your markdown vault root is {vault_root}. It holds: "
            f"Conversations/ (per-conversation closeout checkpoints), "
            f"Identity/ (archives evicted from your core identity files), "
            f"and your episodic memory layer maintained nightly by the "
            f"memory-curator skill — Daily/ (factual per-day ledgers), "
            f"Episodic/ (reflective per-day distillations), and Topics/ "
            f"(append-only interlinked threads that compound across days). "
            f"Sub-agent persona files (agents/<slug>.md) and skill files "
            f"(skills/<agent>/<slug>.md) live under {anna_home}, not in the "
            f"vault. All vault writes go under {vault_root}."
        )

        identity_block = "\n\n".join(
            [
                _section(CoreFile.SOUL, "SOUL.md"),
                _section(CoreFile.CLAUDE, "CLAUDE.md"),
                _section(CoreFile.IDENTITY, "IDENTITY.md"),
                _section(CoreFile.MEMORY, "MEMORY.md"),
                _section(CoreFile.AGENTS, "AGENTS.md"),
            ]
        )

        context = (
            f"Active conversation key: {self.conversation_key}.\n"
            f"{format_rule}"
        )

        # Resume context: the two most recent checkpoint files for this
        # conversation key, oldest first so the assistant reads them
        # chronologically. Omitted entirely if no checkpoints exist (fresh
        # conversation) so the prompt stays clean on first contact.
        resume_block = self._assemble_resume_block(vault_root)

        sections: list[str] = [
            scope,
            f"# Runtime paths\n{runtime}",
        ]
        if resume_block:
            sections.append(resume_block)
        sections.append(f"# Core identity files\n{identity_block}")
        sections.append(f"# Channel context\n{context}")
        return "\n\n".join(sections)

    def _assemble_resume_block(self, vault_root: Path) -> str:
        """Read the two newest checkpoints for this conv_key and format them.

        Returns the formatted block (with leading ``# Recent checkpoints``
        heading), or an empty string when no checkpoints exist.

        When ``checkpoint.resume_from_transcript`` is enabled (and the worker
        is not ephemeral), a bounded RAW tail of the JSONL transcript newer
        than the latest checkpoint is appended after the checkpoint block.
        This covers the gap left by a hard crash / OOM-kill / ``kill -9``
        that never ran graceful closeout. The tail addition is fully
        defensive: any failure falls back to the checkpoint block alone.
        """
        checkpoint_block = self._assemble_checkpoint_block(vault_root)
        tail_block = self._assemble_transcript_tail_block(vault_root)
        if not tail_block:
            return checkpoint_block
        if not checkpoint_block:
            return tail_block
        # Non-empty tail: delimit it from the checkpoint block with a blank
        # line so the two sections read cleanly.
        return f"{checkpoint_block}\n\n{tail_block}"

    def _assemble_checkpoint_block(self, vault_root: Path) -> str:
        """Read the two newest checkpoints for this conv_key and format them.

        Returns the formatted block (with leading ``# Recent checkpoints``
        heading), or an empty string when no checkpoints exist.
        """
        try:
            paths = list_recent_checkpoints(
                vault_root=vault_root,
                conversation_key=self.conversation_key,
                limit=2,
            )
        except OSError as exc:
            self._log.warning("worker.resume.list_failed", error=str(exc))
            return ""
        if not paths:
            return ""

        # list_recent_checkpoints returns newest first; reverse so the
        # earliest checkpoint reads first.
        parts: list[str] = []
        for path in reversed(paths):
            # Filename shape: YYYY-MM-DD-HHMM.md. Strip the suffix for the
            # human-readable label.
            stamp = path.stem
            try:
                body = path.read_text(encoding="utf-8")
            except OSError as exc:
                self._log.warning(
                    "worker.resume.read_failed",
                    file=str(path),
                    error=str(exc),
                )
                continue
            parts.append(f"## {stamp}\n{body.strip()}")

        if not parts:
            return ""
        body = "\n\n".join(parts)
        return f"# Recent checkpoints (resume context)\n{body}"

    def _assemble_transcript_tail_block(self, vault_root: Path) -> str:
        """Render the unsaved transcript tail since the latest checkpoint.

        Gated on ``checkpoint.resume_from_transcript`` and ``not
        self._ephemeral``. Returns the rendered tail block, or "" when the
        feature is off, the worker is ephemeral, there is no fresh tail, or
        anything goes wrong. Never raises into prompt assembly.
        """
        ckpt_cfg = self._config.checkpoint
        if not ckpt_cfg.resume_from_transcript or self._ephemeral:
            return ""
        try:
            since_mtime = latest_checkpoint_mtime(vault_root, self.conversation_key)
            tail = transcript_tail_since(
                transcripts_dir=self._config.transcripts_dir,
                conv_key=self.conversation_key,
                since_mtime=since_mtime,
                max_turns=ckpt_cfg.tail_max_turns,
                max_tokens=ckpt_cfg.tail_max_tokens,
            )
            return render_tail_block(tail)
        except Exception as exc:  # noqa: BLE001 — never break prompt assembly
            self._log.warning("worker.resume.tail_failed", error=str(exc))
            return ""

    async def _closeout(self) -> None:
        """Per v3 §6: write a checkpoint, then run eviction on every core file.

        Called from :meth:`stop` before the SDK client is closed. The
        ``_closed_out`` flag guarantees this only runs once even if stop()
        is invoked twice (e.g. the idle watcher and the router shutdown
        both fire).

        Phase 2 §5 subtask 7: when ``self._ephemeral`` is true (set by the
        CLI adapter for one-shot ``anna ask`` sessions), the worker skips
        the checkpoint write and the per-core-file eviction sweep so each
        ad-hoc invocation does not pollute
        ``vault/Conversations/cli-oneshot-<uuid>/``. An audit line records
        the ephemeral close so the operator can still see the session
        completed; the SDK client is torn down by the caller in the
        normal way.
        """
        self._log.info("worker.closeout.start", conv_key=self.conversation_key)

        if self._ephemeral:
            self._log.info(
                "worker.closeout.skipped_ephemeral",
                conv_key=self.conversation_key,
                transport=self.transport,
            )
            audit_event(
                "audit.checkpoint.skipped_ephemeral",
                audit_dir=self._config.audit_dir,
                actor="anna",
                conv_key=self.conversation_key,
                fsync_on_write=self._config.logging.audit.fsync_on_write,
                transport=self.transport,
            )
            return

        # ----- 1. Checkpoint summary --------------------------------------
        # Closeout always writes its authoritative LLM-authored summary,
        # regardless of whether a periodic checkpoint just landed. The
        # dirty-flag gate lives only in ``_maybe_periodic_checkpoint``.
        summary = await self._ask_checkpoint_summary()
        await self._write_checkpoint_now(summary, kind="closeout")

        # ----- 2. Per-core-file eviction ---------------------------------
        for which in CORE_FILES.keys():
            spec = CORE_FILES[which]
            lock = await self._supervisor.acquire(f"core/{spec.name}")
            async with lock:
                try:
                    archive_path = await evict_if_over_cap(
                        which=which,
                        core_dir=self._config.core_dir,
                        vault_root=self._config.vault.resolved_path,
                        sdk_client=self._client,
                        session_close_conv=self.conversation_key,
                        audit_dir=self._config.audit_dir,
                        fsync_on_write=self._config.logging.audit.fsync_on_write,
                    )
                except Exception as exc:
                    # eviction.py audits its own failures; this catches any
                    # outright crash so we still try the next file.
                    self._log.error(
                        "worker.eviction_failed",
                        file=spec.name,
                        error=str(exc),
                    )
                    continue
                if archive_path is not None:
                    self._log.info(
                        "worker.eviction.applied",
                        file=spec.name,
                        archive=str(archive_path),
                    )

        self._log.info("worker.closeout.complete")

    async def _write_checkpoint_now(self, summary: str, kind: str) -> Path | None:
        """Write a checkpoint under the supervisor lock and audit it.

        Extracted from :meth:`_closeout` so both the graceful-close path
        (``kind="closeout"``) and the periodic path
        (``kind="periodic"``) share one write+audit code path. NOTE:
        this does NOT run eviction — eviction stays exclusively in
        :meth:`_closeout`. On success the worker's checkpoint bookkeeping
        is reset (``_dirty`` cleared, turn counter zeroed,
        ``_last_checkpoint_at`` stamped). Returns the written path, or
        ``None`` if the write failed (an OSError is caught and audited,
        matching the prior closeout behavior).

        The lock key ``checkpoint/<conv_key>`` is per-conversation: it
        serialises a periodic write against the closeout write for the
        same conversation without contending with eviction's
        ``core/<file>`` locks.
        """
        lock = await self._supervisor.acquire(f"checkpoint/{self.conversation_key}")
        async with lock:
            try:
                ckpt_path = write_checkpoint(
                    vault_root=self._config.vault.resolved_path,
                    transport=self.transport,
                    conversation_key=self.conversation_key,
                    summary=summary,
                    operator_short_name=self._operator_short_name,
                    kind=kind,
                )
            except OSError as exc:
                self._log.error("worker.checkpoint_write_failed", error=str(exc))
                audit_event(
                    "audit.checkpoint.write_failed",
                    audit_dir=self._config.audit_dir,
                    actor="anna",
                    conv_key=self.conversation_key,
                    fsync_on_write=self._config.logging.audit.fsync_on_write,
                    level="WARNING",
                    error=str(exc),
                    checkpoint_kind=kind,
                )
                return None

            audit_event(
                "audit.checkpoint.written",
                audit_dir=self._config.audit_dir,
                actor="anna",
                conv_key=self.conversation_key,
                fsync_on_write=self._config.logging.audit.fsync_on_write,
                checkpoint_file=str(ckpt_path),
                summary_chars=len(summary),
                checkpoint_kind=kind,
            )

        # Reset checkpoint bookkeeping now that a checkpoint covers the
        # current state. Applies to both periodic and closeout writes.
        self._dirty = False
        self._turns_since_checkpoint = 0
        self._last_checkpoint_at = datetime.now(timezone.utc)
        return ckpt_path

    async def _maybe_periodic_checkpoint(self) -> None:
        """Write a lightweight periodic checkpoint between turns, if due.

        Invoked at the TOP of :meth:`_handle`, BEFORE ``self._client.query``,
        on the single-consumer run loop. Because it runs strictly between
        turns, it can never race an in-flight streaming reply — this is the
        property that eliminates the exit-143 mid-reply-kill footgun. There
        is no background timer.

        Trigger logic (all gates must pass):

        * Skip when ``self._ephemeral`` (one-shot CLI sessions never
          checkpoint).
        * Skip when ``checkpoint.periodic_enabled`` is False.
        * Skip when not ``self._dirty`` — nothing new since the last
          checkpoint, so a write would be redundant.
        * Fire when ``_turns_since_checkpoint >= every_turns`` OR when the
          minutes elapsed since the baseline ``>= every_minutes``.

        Baseline for the minutes check: ``_last_checkpoint_at`` once any
        checkpoint has been written this session; before that, the worker
        creation time (``last_active`` is seeded to creation time in
        ``__init__``, but we keep a dedicated ``_created_at`` so a long
        first burst of turns can still arm the wall-clock trigger
        independent of activity). The dirty gate guarantees we only fire
        when there is actually a new turn to capture.

        The summary is MECHANICAL — the Fix-1 transcript tail rendered
        compactly. No SDK round-trip, so it cannot contend with the shared
        client. If the tail is empty (nothing new on disk yet) we skip the
        write but still reset the dirty flag / counters to avoid an empty
        checkpoint and repeated no-op attempts.

        The whole body is wrapped so a periodic-checkpoint failure NEVER
        breaks the turn: on any error we log + audit a warning and return,
        letting ``_handle`` proceed to the query.
        """
        if self._ephemeral:
            return
        ckpt_cfg = self._config.checkpoint
        if not ckpt_cfg.periodic_enabled:
            return
        if not self._dirty:
            return

        now = datetime.now(timezone.utc)
        baseline = self._last_checkpoint_at or self._created_at
        minutes_since = (now - baseline).total_seconds() / 60.0
        due = (
            self._turns_since_checkpoint >= ckpt_cfg.every_turns
            or minutes_since >= ckpt_cfg.every_minutes
        )
        if not due:
            return

        try:
            vault_root = self._config.vault.resolved_path
            since_mtime = latest_checkpoint_mtime(vault_root, self.conversation_key)
            tail = transcript_tail_since(
                transcripts_dir=self._config.transcripts_dir,
                conv_key=self.conversation_key,
                since_mtime=since_mtime,
                max_turns=ckpt_cfg.tail_max_turns,
                max_tokens=ckpt_cfg.tail_max_tokens,
            )
            summary = render_tail_block(tail)
            if not summary:
                # Nothing new on disk to capture. Reset the bookkeeping so
                # we do not retry every turn against an empty tail.
                self._dirty = False
                self._turns_since_checkpoint = 0
                self._last_checkpoint_at = now
                return

            # Capture the triggering count BEFORE the write — it resets
            # ``_turns_since_checkpoint`` to 0 (Fix 2), so reading the
            # field after would always log 0 in the audit event.
            triggering_turns = self._turns_since_checkpoint
            ckpt_path = await self._write_checkpoint_now(summary, kind="periodic")
            if ckpt_path is not None:
                audit_event(
                    "audit.checkpoint.periodic",
                    audit_dir=self._config.audit_dir,
                    actor="anna",
                    conv_key=self.conversation_key,
                    fsync_on_write=self._config.logging.audit.fsync_on_write,
                    checkpoint_file=str(ckpt_path),
                    turns_since_checkpoint=triggering_turns,
                )
                self._log.info(
                    "worker.checkpoint.periodic",
                    checkpoint_file=str(ckpt_path),
                )
        except Exception as exc:  # noqa: BLE001 — never break the turn
            self._log.warning("worker.checkpoint.periodic_failed", error=str(exc))
            audit_event(
                "audit.checkpoint.periodic_failed",
                audit_dir=self._config.audit_dir,
                actor="anna",
                conv_key=self.conversation_key,
                fsync_on_write=self._config.logging.audit.fsync_on_write,
                level="WARNING",
                error=str(exc),
            )

    async def _ask_checkpoint_summary(self) -> str:
        """Round-trip the SDK for a closing summary. Best-effort.

        If the SDK is unavailable or errors, falls back to a minimal
        placeholder so the checkpoint file still lands. Never raises.
        """
        if self._client is None:
            return "(no SDK client available at closeout; placeholder checkpoint)"

        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        except ImportError:
            AssistantMessage = ResultMessage = TextBlock = None  # type: ignore[assignment,misc]

        prompt = (
            "Write a brief checkpoint summarizing this conversation — topics "
            "covered, decisions, open threads, anything to remember next time "
            "we resume. Two to four short paragraphs. Plain text."
        )
        try:
            await self._client.query(prompt)  # type: ignore[attr-defined]
        except Exception as exc:
            self._log.warning("worker.closeout.query_failed", error=str(exc))
            return f"(closeout query failed: {exc})"

        chunks: list[str] = []
        try:
            async for msg in self._client.receive_response():  # type: ignore[attr-defined]
                if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if TextBlock is not None and isinstance(block, TextBlock):
                            chunks.append(block.text)
                if ResultMessage is not None and isinstance(msg, ResultMessage):
                    break
        except Exception as exc:
            self._log.warning("worker.closeout.receive_failed", error=str(exc))
            return f"(closeout receive failed: {exc})"

        text = "\n".join(c for c in chunks if c).strip()
        return text or "(empty closeout summary)"

    async def _close_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.__aexit__(None, None, None)  # type: ignore[attr-defined]
        except Exception as exc:
            self._log.warning("worker.client_close_failed", error=str(exc))
        finally:
            self._client = None

    async def _build_image_prompt(
        self, query_text: str, images: list[ImageAttachment]
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield a single stream-json user message carrying images.

        The SDK's ``query`` accepts ``str | AsyncIterable[dict]``. The
        string branch wraps text as a user message; the AsyncIterable
        branch writes each yielded dict verbatim to the CLI stdin. We
        yield exactly one dict whose content is the text block followed by
        one base64 image block per attachment, so the model receives the
        operator's caption and the dragged-in images in the same turn.
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": query_text}]
        for image in images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": base64.b64encode(image.data).decode(),
                    },
                }
            )
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    def _voice_only_for_transport(self) -> bool:
        """True when this transport would send replies as voice-only.

        Decision F: when voice-only outbound is configured for this
        transport, the turn stays consolidated to a single voice note at
        turn end (same as the scheduler path), so the timed drip is not
        started. We gate on the static config — ``voice.outbound.enabled``,
        ``voice_only``, and this transport being in the outbound allowlist —
        which is the condition under which an intermediate drip could be
        fragmented into its own voice note.
        """
        voice_out = self._config.voice.outbound
        return (
            voice_out.enabled
            and voice_out.voice_only
            and self.transport in voice_out.transports
        )

    def _periodic_flush_active(self, event: InboundEvent) -> bool:
        """Whether to start the timed-drip task for this turn.

        Active only for an interactive (non-scheduler), non-voice-only turn
        on a buffered transport with a positive interval. The scheduler path
        (``completion_future`` set) and voice-only outbound stay consolidated.
        """
        if self._flush_interval <= 0:
            return False
        if event.completion_future is not None:
            return False
        if self.transport not in ("slack", "telegram"):
            return False
        if self._voice_only_for_transport():
            return False
        return True

    async def _periodic_flush_loop(
        self, event: InboundEvent, buffer: _FlushBuffer
    ) -> None:
        """Background timer that drips ``buffer.pending`` on a wall-clock cadence.

        Started at turn begin only when :meth:`_periodic_flush_active`, and
        cancelled-and-awaited in the turn's ``finally``. Sleeps ``poll``
        seconds, then — under ``buffer.lock`` — sends one ``OutboundMessage``
        and clears the buffer iff it is non-empty AND at least ``interval``
        seconds have elapsed since the last message of any kind. Every send
        restamps ``last_flush`` so a tool-use flush is never immediately
        followed by a redundant empty drip, and a drip resets the interval
        for the next one (decision B).

        The consumer loop stays a plain ``async for`` (the spike proved the
        ``wait_for(__anext__())`` poll-wrapper finalizes the SDK generator
        stack and drops the rest of the turn); this task is the ONLY timing
        mechanism. Cancellation is the normal teardown path and is re-raised.
        """
        interval = float(self._flush_interval)
        # Poll at a 1-second floor (or the interval, if smaller) so the timer
        # task is frequently runnable and the event loop services it on its
        # own timer wheel instead of leaving it starved behind the turn's
        # receive coroutine. The actual SEND cadence stays at ``interval``:
        # the ``loop.time() - last_flush < interval`` guard below is evaluated
        # every tick, so a real flush still only happens once the full
        # interval has elapsed since the last message of any kind.
        poll = min(interval, 1.0)
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(poll)
                async with buffer.lock:
                    if not buffer.pending:
                        continue
                    if loop.time() - buffer.last_flush < interval:
                        continue
                    txt = "\n".join(c for c in buffer.pending if c).strip()
                    # Empty/whitespace: nothing to send, but clear+stamp so a
                    # buffer of only-blank chunks doesn't re-fire every tick.
                    if not txt:
                        buffer.pending.clear()
                        buffer.last_flush = loop.time()
                        continue
                    # Send BEFORE clearing so the text is loss-safe: the lock
                    # is held across the send, so ``pending`` cannot grow
                    # during it, and only a successful return clears/stamps.
                    # If a cancel (turn-end teardown) or exception lands inside
                    # ``self._send``, ``pending`` keeps the text and the final
                    # turn-end send re-emits it — no silent drop. CancelledError
                    # propagates for clean teardown; a real send failure is
                    # logged and the timer keeps ticking (buffer untouched, so
                    # the text retries on the next tick or the final send).
                    try:
                        await self._guarded_send(
                            OutboundMessage(
                                conversation_key=event.conversation_key,
                                text=txt,
                            )
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._log.warning(
                            "worker.periodic_flush.send_failed",
                            error=str(exc),
                            conv_key=event.conversation_key,
                            transport=self.transport,
                        )
                        continue
                    buffer.pending.clear()
                    buffer.last_flush = loop.time()
        except asyncio.CancelledError:
            raise

    def _suppression_in_admin_channel(self, conv_key: str) -> bool:
        """True when ``conv_key`` targets the admin alert destination for this
        transport. Used to break the alert feedback loop: a suppression that
        happened in the admin channel must not fire another admin alert there.
        """
        admin = getattr(self._config, "admin", None)
        if admin is None:
            return False
        if self.transport == "slack":
            destination = getattr(admin, "slack_channel_id", None)
        elif self.transport == "telegram":
            destination = getattr(admin, "telegram_chat_id", None)
        else:
            destination = None
        if not destination:
            return False
        return str(destination) in conv_key

    async def _emit_markup_suppressed(self, text: str, *, conv_key: str) -> None:
        """Audit + log + best-effort alert when leaked tool-call markup is
        suppressed before it can reach a transport.

        Called by :meth:`_guarded_send` (interactive path) and the
        scheduler-driven completion-future path in :meth:`_handle`. The
        admin alert is fired through the shared :class:`AdminAlerter` when
        one is wired; failures there are logged and never propagate.
        """
        markers = _matched_markers(text)
        audit_event(
            "audit.reply.toolcall_markup_suppressed",
            audit_dir=self._config.audit_dir,
            actor="anna",
            conv_key=conv_key,
            fsync_on_write=self._config.logging.audit.fsync_on_write,
            level="WARNING",
            transport=self.transport,
            char_count=len(text),
            markers=markers,
            preview=text[:280],
        )
        self._log.warning(
            "worker.reply.toolcall_markup_suppressed",
            conv_key=conv_key,
            transport=self.transport,
            char_count=len(text),
            markers=markers,
        )
        # Feedback-loop break: when the suppressed reply was itself posted in
        # the admin channel, do NOT alert that same channel about it. The
        # operator is already reading there; an alert-per-suppression in the
        # admin channel turns one mis-formatted explanation into a runaway
        # cascade (suppress -> alert -> operator asks -> suppress -> ...).
        # The audit row + log line above still record every suppression.
        if self._alerter is not None and not self._suppression_in_admin_channel(conv_key):
            alerter = self._alerter
            message = (
                "Suppressed a reply containing leaked tool-call markup "
                f"on {self.transport} (conv {conv_key}); markers="
                f"{markers}."
            )

            async def _alert() -> None:
                try:
                    await alerter.warn(message, exclude_channel=None)
                except Exception as exc:
                    self._log.warning(
                        "worker.reply.markup_alert_failed",
                        error=str(exc),
                        conv_key=conv_key,
                        transport=self.transport,
                    )

            # AdminAlerter.warn is best-effort; do not block the turn on it.
            # The inner closure owns its own try/except so a raising warn can
            # never surface as an unretrieved-task exception.
            asyncio.create_task(
                _alert(),
                name=f"worker.markup_alert.{conv_key}",
            )

    async def _regenerate_scheduled_reply(
        self, event: InboundEvent, correction_prompt: str
    ) -> str | None:
        """Re-issue a degraded scheduled turn into the SAME SDK session and
        return the freshly-generated ``reply_text`` (or ``None`` on error).

        Scoped to the scheduler-driven completion-future path only. The
        original turn leaked tool-call markup as TEXT, which means NO tool
        actually ran (the model only narrated the calls) — so the failed
        generation has zero side effects and re-running carries no
        double-execution risk. We feed a short correction prompt back into
        the live session and drain a fresh response.

        This is a deliberately stripped-down mirror of the main drain loop in
        :meth:`_handle`: the scheduled path never mid-flushes (no buffer, no
        timed drip, no stream subscriber, no tool-use boundary sends), so a
        plain accumulate-text-blocks loop is the whole story here. On any
        query/receive exception we log ``worker.regenerate_failed`` and return
        ``None`` so the caller can fall back to today's suppress behavior;
        we never raise out of here and never leave the caller's future unset.
        """
        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        except Exception:  # pragma: no cover - SDK always present in prod
            AssistantMessage = ResultMessage = TextBlock = None  # type: ignore[assignment,misc]

        reply_chunks: list[str] = []
        try:
            await self._client.query(correction_prompt)  # type: ignore[attr-defined]
            async for msg in self._client.receive_response():  # type: ignore[attr-defined]
                if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if TextBlock is not None and isinstance(block, TextBlock):
                            reply_chunks.append(block.text)
                if ResultMessage is not None and isinstance(msg, ResultMessage):
                    break
        except Exception as exc:
            self._log.error(
                "worker.regenerate_failed",
                error=str(exc),
                conv_key=event.conversation_key,
                transport=self.transport,
            )
            return None

        reply_text = "\n".join(c for c in reply_chunks if c).strip()
        if not reply_text:
            reply_text = "(no response)"
        return reply_text

    async def _guarded_send(
        self, msg: OutboundMessage, *, tool_used: bool = False
    ) -> None:
        """Send ``msg`` unless its text carries leaked tool-call markup.

        Interactive send sites route through here so a degraded turn can
        never post literal function-call syntax to a transport. On
        detection the send is dropped after auditing/alerting. SDK-error
        strings (our own text) bypass this and use ``self._send`` directly.

        ``tool_used`` reports whether a real structured tool call executed
        this turn. A weak/partial markup fragment (a lone opening invoke tag)
        on such a turn is delivered rather than suppressed — it is almost
        always ANNA quoting a fragment in otherwise-good prose. Strong markup
        (two-plus distinct markers, or any self-sufficient marker) is always
        suppressed, as is weak markup when no tool ran (the genuine
        prose-instead-of-tool-call failure). ``tool_used`` defaults to
        ``False`` so callers with no notion of tool execution keep the strict,
        fail-closed behavior.
        """
        if _should_suppress_markup(msg.text, tool_used=tool_used):
            await self._emit_markup_suppressed(
                msg.text, conv_key=msg.conversation_key
            )
            return
        await self._send(msg)

    async def _handle(self, event: InboundEvent) -> None:
        if self._client is None:
            if event.completion_future is not None and not event.completion_future.done():
                event.completion_future.set_exception(
                    RuntimeError("worker has no SDK client; cannot dispatch")
                )
            return

        # Periodic checkpoint (Fix 2). Runs BETWEEN turns: this is the top
        # of the turn handler, BEFORE ``self._client.query(...)`` below, on
        # the single-consumer run loop. Because no reply is in flight at
        # this point, the checkpoint can never race a streaming response —
        # this ordering is load-bearing and must stay before the query.
        # The call is fully self-contained and exception-isolated, so a
        # checkpoint failure never blocks the turn.
        await self._maybe_periodic_checkpoint()

        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
        except ImportError:
            AssistantMessage = ResultMessage = TextBlock = ToolUseBlock = None  # type: ignore[assignment,misc]

        # Cadence-Visibility Hooks plan (Inbox/2026-06-02) subtask 5:
        # for buffered transports (Slack, Telegram) prepend the
        # ``<system-reminder>`` cadence block sourced from
        # ``core/CADENCE.md`` via the loader on the bundle. CLI sees
        # deltas live so the reminder is not needed there. The loader
        # call is per-event (no caching) so the operator can edit
        # CADENCE.md without restarting ANNA. An empty / missing file
        # degrades gracefully to the unmodified event text.
        query_text = event.text
        if (
            self.transport in ("slack", "telegram")
            and self._visibility.cadence_reminder_loader is not None
        ):
            reminder = ""
            try:
                reminder = self._visibility.cadence_reminder_loader()
            except Exception as exc:
                self._log.warning(
                    "worker.cadence_reminder.load_failed",
                    error=str(exc),
                )
                reminder = ""
            if reminder:
                query_text = (
                    f"<system-reminder>\n{reminder}\n</system-reminder>\n\n"
                    f"{event.text}"
                )

        # Thinking-signal start. Captured handle (possibly None) is
        # cleared in the outer ``finally`` below so the cleanup path
        # runs on success, exception, and cancellation alike. A start
        # failure must not abort the SDK call — the operator simply
        # misses the visibility cue for this turn.
        handle: SignalHandle | None = None
        try:
            handle = await self._visibility.start(event)
        except Exception as exc:
            self._log.warning(
                "worker.thinking_signal.start_failed",
                error=str(exc),
            )
            handle = None

        # Collect text blocks until ResultMessage. ``reply_chunks`` is
        # defined outside the try/finally so the lint and send paths
        # downstream can read it. The ``finally`` runs on every exit
        # path (including the early ``return``s inside the SDK error
        # handlers), so the thinking signal is cleared even when the
        # SDK call fails or the run-loop is cancelled.
        #
        # ``buffer.pending`` accumulates text since the last flush boundary
        # so buffered transports (Slack, Telegram) receive narration as a
        # sequence of messages keyed off the model's natural tool-use
        # cadence — and, when the timed-drip is active, off a wall-clock
        # cadence — instead of one consolidated end-of-turn blob. The
        # scheduler-driven path (event.completion_future set) keeps the
        # old behavior — scheduled jobs want one return value, not a
        # stream. ``reply_chunks`` accumulates EVERY text block for the
        # whole turn and is never cleared by any flush, so the cadence
        # linter still sees the full reply regardless of drip count.
        #
        # ``buffer.lock`` serializes every append/flush of ``pending``
        # between this consumer loop and the background timer task, so on
        # the single-threaded event loop ordering is preserved and there is
        # no concurrent-mutation race. ``pending`` is always mutated in
        # place (extend/clear) — never rebound — so the timer sees writes.
        reply_chunks: list[str] = []
        # Tracks whether ANY real tool actually executed this turn (a
        # ToolUseBlock was observed). Load-bearing for the scheduled-turn
        # regeneration guard below: regeneration is only safe when zero tools
        # ran (a degraded turn that NARRATED its calls as markup has no side
        # effects). If a tool truly executed AND markup also leaked, re-running
        # would double-execute that tool — so we must NOT regenerate. Set on
        # EVERY path (interactive and scheduled) because the ToolUseBlock is
        # visible in the drain loop regardless of the flush sub-branch.
        tool_used = False
        loop = asyncio.get_running_loop()
        buffer = _FlushBuffer(last_flush=loop.time())
        # Timed-drip timer (Inbox/2026-06-04 plan). Started ONLY for an
        # interactive, non-voice-only turn on a buffered transport with a
        # positive interval; cancelled-and-awaited in the ``finally`` below.
        flush_task: asyncio.Task[None] | None = None
        if self._periodic_flush_active(event):
            flush_task = asyncio.create_task(
                self._periodic_flush_loop(event, buffer),
                name=f"worker.flush.{self.conversation_key}",
            )
        try:
            # Send the user message into the SDK. NOTE: ``query_text``
            # (not ``event.text``) carries the cadence reminder when
            # one was loaded.
            try:
                # Image inbound (Slack drag-and-drop): hand the SDK an
                # AsyncIterable yielding one stream-json user message with
                # base64 image blocks. All text and voice turns keep the
                # byte-for-byte string path.
                prompt = (
                    self._build_image_prompt(query_text, event.images)
                    if event.images
                    else query_text
                )
                await self._client.query(prompt)  # type: ignore[attr-defined]
            except Exception as exc:
                self._log.error("worker.sdk_query_failed", error=str(exc))
                if event.completion_future is not None and not event.completion_future.done():
                    event.completion_future.set_exception(exc)
                    return
                await self._send(OutboundMessage(
                    conversation_key=event.conversation_key,
                    text=f"I hit an error talking to the model: {exc}",
                ))
                return

            # A real turn has now run: the SDK accepted the query. Advance
            # the periodic-checkpoint bookkeeping here so it is scoped to
            # turns that reached the query path. The ``_client is None``
            # early return above never gets here, so a no-client no-op no
            # longer arms the periodic checkpoint (Fix 1). A downstream
            # receive error still counts — the query ran and produced
            # transcript activity worth checkpointing.
            self._turns_since_checkpoint += 1
            self._dirty = True

            try:
                async for msg in self._client.receive_response():  # type: ignore[attr-defined]
                    if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if TextBlock is not None and isinstance(block, TextBlock):
                                reply_chunks.append(block.text)
                                # Append the narration to the shared flush
                                # buffer under the lock so the timer task
                                # never reads a half-written ``pending``.
                                async with buffer.lock:
                                    buffer.pending.append(block.text)
                                # Phase 2 §5: emit streaming deltas to the
                                # per-event subscriber (set by the CLI adapter)
                                # before the buffered finalize lands. Exception
                                # isolation is mandatory: a misbehaving
                                # subscriber must NOT abort the buffered send
                                # that Slack and Telegram depend on.
                                if event.stream_subscriber is not None:
                                    try:
                                        await event.stream_subscriber(block.text)
                                    except Exception as exc:
                                        self._log.warning(
                                            "worker.stream_subscriber_failed",
                                            error=str(exc),
                                            conv_key=event.conversation_key,
                                        )
                            elif ToolUseBlock is not None and isinstance(block, ToolUseBlock):
                                # A real tool executed this turn. Record it
                                # OUTSIDE the flush guard below so it reflects
                                # BOTH the interactive and scheduled paths (the
                                # scheduled path skips the flush sub-branch but
                                # still sees the ToolUseBlock here). The
                                # scheduled-turn regeneration guard reads this
                                # to refuse a re-run that would double-execute.
                                tool_used = True
                                # Tool-use boundary: the model has stopped
                                # narrating to invoke a tool. Flush the
                                # pending narration as its own outbound
                                # message so Slack/Telegram receive
                                # cadence-aligned messages instead of one
                                # end-of-turn blob.
                                #
                                # Scheduler-driven dispatch (completion_future
                                # set) is excluded — scheduled jobs want one
                                # consolidated return value, not a stream.
                                # Empty/whitespace pending buffers are
                                # skipped (no point sending blank messages).
                                #
                                # Taken under the lock and ``pending`` is
                                # cleared in place so the timer task can't
                                # race a concurrent drip; ``last_flush`` is
                                # stamped on every flush (even an empty one)
                                # so the timer measures its interval from the
                                # last message of ANY kind (decision B) and
                                # never re-fires on an already-emptied buffer.
                                #
                                # Send BEFORE clearing (mirrors the drip loop)
                                # so the text is loss-safe: the lock is held
                                # across the send, so ``pending`` cannot grow
                                # during it, and only a successful return
                                # clears/stamps. A cancel/exception inside the
                                # send leaves the text in ``pending`` for the
                                # final turn-end send.
                                if event.completion_future is None:
                                    async with buffer.lock:
                                        if buffer.pending:
                                            txt = "\n".join(
                                                c for c in buffer.pending if c
                                            ).strip()
                                            if txt:
                                                # ``tool_used`` is True here (set
                                                # just above on this ToolUseBlock),
                                                # so a weak trailing fragment in the
                                                # narration is delivered, not eaten.
                                                await self._guarded_send(
                                                    OutboundMessage(
                                                        conversation_key=event.conversation_key,
                                                        text=txt,
                                                    ),
                                                    tool_used=tool_used,
                                                )
                                            buffer.pending.clear()
                                            buffer.last_flush = loop.time()
                    if ResultMessage is not None and isinstance(msg, ResultMessage):
                        break
            except Exception as exc:
                self._log.error("worker.sdk_receive_failed", error=str(exc))
                if event.completion_future is not None and not event.completion_future.done():
                    event.completion_future.set_exception(exc)
                    return
                await self._send(OutboundMessage(
                    conversation_key=event.conversation_key,
                    text=f"I hit an error reading the model response: {exc}",
                ))
                return
        finally:
            # Tear down the timed-drip timer BEFORE the final send so no
            # concurrent drip can race the residual-buffer send below.
            # Cancel-and-await is the normal teardown path; the task only
            # ever sends a message and holds no resource. Runs on every exit
            # path (success, exception, cancellation, early return).
            #
            # Only the flush task's OWN cancellation (from our explicit
            # ``flush_task.cancel()``) is suppressed. If the outer/current
            # task is itself being cancelled (worker stop/restart), that
            # cancellation must propagate — re-raise it rather than swallow.
            if flush_task is not None:
                flush_task.cancel()
                try:
                    await flush_task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling() > 0:
                        raise
                except Exception as exc:
                    self._log.warning(
                        "worker.periodic_flush.teardown_failed",
                        error=str(exc),
                        conv_key=event.conversation_key,
                        transport=self.transport,
                    )
            # ALWAYS clear, even on exception, cancellation, or early
            # return inside the try-block above. The clear callable is
            # itself exception-isolated — defense-in-depth keeps a
            # misbehaving clear from leaking out of the finally.
            if handle is not None:
                try:
                    await self._visibility.clear(handle)
                except Exception as exc:
                    self._log.warning(
                        "worker.thinking_signal.clear_failed",
                        error=str(exc),
                    )

        reply_text = "\n".join(c for c in reply_chunks if c).strip()
        if not reply_text:
            reply_text = "(no response)"

        # Cadence-Visibility Hooks subtask 5: telemetry-only lint of
        # the final ``reply_text`` before dispatch. ``CadenceLinter.lint``
        # swallows its own exceptions; the outer try/except is
        # defense-in-depth so a misbehaving custom lint callable cannot
        # block delivery.
        if self._visibility.lint is not None:
            try:
                self._visibility.lint.lint(
                    reply_text,
                    transport=self.transport,
                    conv_key=event.conversation_key,
                )
            except Exception as exc:
                self._log.warning(
                    "worker.cadence_lint.call_failed",
                    error=str(exc),
                )

        # Scheduler-driven (or any future caller-driven) dispatch short-circuits
        # the normal send path. The caller awaits the future and routes the
        # output itself. Transport-originated events have completion_future
        # unset and use the standard send-back path.
        if event.completion_future is not None and not event.completion_future.done():
            # Scheduled-turn guard: a degraded turn must not resolve the
            # future with leaked tool-call markup, which the scheduler would
            # otherwise route to the destination transport.
            #
            # Before falling back to the QUIET_SENTINEL suppression (which
            # silently loses the whole tick), make ONE bounded regeneration
            # attempt. The leak means the model NARRATED its tool calls as
            # text instead of executing them, so zero tools ran and the failed
            # generation has no side effects — re-running the heartbeat into
            # the same SDK session is safe with no double-execution risk. The
            # failure is intermittent and probabilistic per-generation
            # (~1-2 of 26 daily ticks), so a single fresh generation almost
            # always succeeds. AT MOST one retry — never recurse — so a
            # persistently degraded model degrades to exactly today's behavior.
            # ``_should_suppress_markup`` softens the gate: a WEAK/partial
            # fragment (a lone opening invoke tag) on a turn that DID execute a
            # real tool call is delivered (falls through to ``set_result``
            # below), since it is almost always ANNA quoting a fragment in
            # otherwise-good prose. STRONG markup, or any markup on a turn with
            # no real tool call, still enters this block and is regenerated or
            # suppressed exactly as before.
            if _should_suppress_markup(reply_text, tool_used=tool_used):
                from anna.runtime.scheduler import QUIET_SENTINEL

                # Exception-safe fallback used by every "give up and go quiet"
                # branch below. ``_emit_markup_suppressed`` audits/alerts
                # WITHOUT internal isolation around its own ``audit_event``, so
                # if that raises the future would be left unresolved — the
                # scheduler then rescues it only via its 1500s timeout AND
                # counts the tick as a FAILURE (three auto-disable the
                # heartbeat). The ``finally`` guarantees the future is ALWAYS
                # resolved exactly once (the ``done()`` guard prevents a
                # double set_result if the future was somehow already settled).
                async def _suppress_and_resolve(text: str) -> None:
                    try:
                        await self._emit_markup_suppressed(
                            text, conv_key=event.conversation_key
                        )
                    except Exception as exc:
                        # ``_emit_markup_suppressed`` calls ``audit_event``
                        # without isolation, so a disk/audit failure could
                        # raise here. The ``_run`` loop wraps ``_handle`` in
                        # try/FINALLY with no ``except`` — an escaping
                        # exception would kill the worker. Swallow it (log
                        # only) and fall through to the guaranteed resolution
                        # so the tick goes quiet instead of crashing.
                        self._log.warning(
                            "worker.markup_suppress_failed",
                            error=str(exc),
                            conv_key=event.conversation_key,
                            transport=self.transport,
                        )
                    finally:
                        if not event.completion_future.done():
                            event.completion_future.set_result(QUIET_SENTINEL)

                # Double-execution guard: regeneration is safe ONLY when zero
                # tools ran this turn — a degraded turn that narrated its calls
                # as markup has no side effects, so re-running it is harmless.
                # Reaching this branch with ``tool_used`` set means a real tool
                # executed AND the leaked markup was STRONG (a weak/partial
                # fragment plus a tool call was already delivered above by
                # ``_should_suppress_markup``). Re-running the heartbeat would
                # invoke that tool a SECOND time, so skip regeneration entirely
                # and fall straight through to today's suppress + sentinel
                # behavior.
                if tool_used:
                    await _suppress_and_resolve(reply_text)
                    return

                # Step 1: record (exception-isolated, mirroring the alert
                # code) that we are attempting a regeneration. WARNING level
                # so the intermittent failure stays visible in the audit log
                # even when the retry rescues the tick.
                try:
                    audit_event(
                        "audit.reply.toolcall_markup_regenerating",
                        audit_dir=self._config.audit_dir,
                        actor="anna",
                        conv_key=event.conversation_key,
                        fsync_on_write=self._config.logging.audit.fsync_on_write,
                        level="WARNING",
                        transport=self.transport,
                        char_count=len(reply_text),
                        markers=_matched_markers(reply_text),
                        preview=reply_text[:280],
                    )
                except Exception as exc:
                    self._log.warning(
                        "worker.reply.markup_regenerating_audit_failed",
                        error=str(exc),
                        conv_key=event.conversation_key,
                        transport=self.transport,
                    )

                # Step 2: re-issue the turn with a short, direct correction
                # prompt. ``_regenerate_scheduled_reply`` returns the fresh
                # reply_text, or None if the re-query/re-drain itself errored.
                correction_prompt = (
                    "Your previous reply emitted tool-call markup (like "
                    "<invoke name=...>) as literal text instead of actually "
                    "executing the tools, so nothing ran. Re-run the heartbeat "
                    "now from the start, ACTUALLY invoking each tool, and "
                    "return ONLY the contract-legal final reply (the "
                    "[[ANNA_NO_OUTPUT]] sentinel if nothing is new, otherwise "
                    "the compact check-in). Do not describe or print tool "
                    "calls as text."
                )
                regenerated = await self._regenerate_scheduled_reply(
                    event, correction_prompt
                )

                # Step 3a: regeneration produced CLEAN text — record the
                # rescue and resolve the future with it. The scheduler already
                # treats a bare QUIET_SENTINEL string as a quiet run, so if the
                # model returned the sentinel we just pass it straight through.
                if regenerated is not None and not _contains_unparsed_toolcall_markup(
                    regenerated
                ):
                    try:
                        audit_event(
                            "audit.reply.toolcall_markup_regenerated",
                            audit_dir=self._config.audit_dir,
                            actor="anna",
                            conv_key=event.conversation_key,
                            fsync_on_write=self._config.logging.audit.fsync_on_write,
                            level="WARNING",
                            transport=self.transport,
                            char_count=len(regenerated),
                        )
                    except Exception as exc:
                        self._log.warning(
                            "worker.reply.markup_regenerated_audit_failed",
                            error=str(exc),
                            conv_key=event.conversation_key,
                            transport=self.transport,
                        )
                    event.completion_future.set_result(regenerated)
                    return

                # Step 3b: regeneration errored (None) or the retry ALSO
                # leaked markup — fall back to EXACTLY today's behavior:
                # suppress the ORIGINAL reply (audit + log + best-effort
                # alert) and resolve with the sentinel so the run is recorded
                # quiet rather than erroring. Exception-safe so a raising audit
                # can never strand the future.
                await _suppress_and_resolve(reply_text)
                return
            event.completion_future.set_result(reply_text)
            return

        # Interactive path: send the trailing pending buffer (text after the
        # last flush boundary — tool-use OR timed drip — or the full reply if
        # nothing flushed). Earlier tool-use and timed-drip flushes already
        # dispatched their slices of narration as separate OutboundMessages,
        # each clearing ``buffer.pending`` in place, so sending the join of
        # ``reply_chunks`` here would duplicate everything. The timer task is
        # cancelled in the ``finally`` above, so this read needs no lock.
        final_text = "\n".join(c for c in buffer.pending if c).strip()
        if final_text:
            await self._guarded_send(
                OutboundMessage(
                    conversation_key=event.conversation_key,
                    text=final_text,
                ),
                tool_used=tool_used,
            )
        elif not reply_chunks:
            # Edge case: the SDK returned no text at all and no tools were
            # called — preserve the "(no response)" fallback so the
            # operator sees SOMETHING. If reply_chunks is non-empty but
            # final_text is empty, that means every text block was already
            # flushed at a tool-use boundary; nothing more to send.
            await self._guarded_send(OutboundMessage(
                conversation_key=event.conversation_key,
                text="(no response)",
            ))
