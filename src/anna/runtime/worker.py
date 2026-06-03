"""Per-conversation worker.

Per v3 section 6. One async worker per active conversation_key, owning one
:class:`claude_agent_sdk.ClaudeSDKClient`. The worker reads events from an
``asyncio.Queue``, dispatches them through the SDK, and writes a vault
checkpoint when it idles out.
"""

from __future__ import annotations

import asyncio
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
from anna.tools.vault_tools import VaultTools
from anna.tools.web_server import WEB_TOOL_NAMES, build_web_server
from anna.tools.web_tools import WebTools
from anna.runtime.visibility import NULL_VISIBILITY, VisibilityCallbacks
from anna.transports.base import InboundEvent, OutboundMessage, SignalHandle
from anna.vault.checkpoint import list_recent_checkpoints, write_checkpoint
from anna.vault.transcript_resume import (
    latest_checkpoint_mtime,
    render_tail_block,
    transcript_tail_since,
)

if TYPE_CHECKING:
    from anna.runtime.schedule_store import ScheduleStore
    from anna.runtime.subagent import SubAgentRunner
    from anna.runtime.supervisor import Supervisor
    from anna.tools.google_clients import GoogleClients


# Default file-system tools we hand to ANNA so she can read and write her
# vault. Listed by their canonical SDK names. The MCP self-edit tools are
# prefixed with ``mcp__anna_self_edit__`` per the SDK convention.
_DEFAULT_FS_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")
_SELF_EDIT_PREFIX = "mcp__anna_self_edit__"
_GOOGLE_PREFIX = "mcp__anna_google__"
_WEB_PREFIX = "mcp__anna_web__"
_DELEGATE_PREFIX = "mcp__anna_delegate__"


def _allowed_tool_names(
    *,
    include_google: bool,
    include_web: bool,
    include_delegate: bool = False,
) -> list[str]:
    names = list(_DEFAULT_FS_TOOLS) + [
        f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES
    ]
    if include_google:
        names.extend(f"{_GOOGLE_PREFIX}{name}" for name in GOOGLE_TOOL_NAMES)
    if include_web:
        names.extend(f"{_WEB_PREFIX}{name}" for name in WEB_TOOL_NAMES)
    if include_delegate:
        names.extend(f"{_DELEGATE_PREFIX}{name}" for name in DELEGATE_TOOL_NAMES)
    return names


SendCallback = Callable[[OutboundMessage], Awaitable[None]]
IdleCloseCallback = Callable[[str], Awaitable[None]]


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
        schedule_store: "ScheduleStore | None" = None,
        google_clients: "GoogleClients | None" = None,
        subagent_runner: "SubAgentRunner | None" = None,
        ephemeral: bool = False,
        visibility: VisibilityCallbacks = NULL_VISIBILITY,
    ) -> None:
        self.conversation_key = conversation_key
        self.transport = transport
        self._config = config
        self._supervisor = supervisor
        self._send = send
        self._on_idle_close = on_idle_close
        self._schedule_store = schedule_store
        self._google_clients = google_clients
        self._subagent_runner = subagent_runner
        # Cadence-Visibility Hooks plan (Inbox/2026-06-02) subtask 5.
        # Default ``NULL_VISIBILITY`` means: no reminder prepend, no
        # thinking-signal start/clear, no lint pass. Existing unit tests
        # and the sub-agent path are unchanged.
        self._visibility = visibility
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
        mcp_servers: dict[str, Any] = {"anna_self_edit": self_edit_server}
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

        # Ensure the vault root exists before the SDK process tries to cd
        # into it; otherwise the first tool call fails with ENOENT.
        try:
            vault_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log.warning("worker.vault_mkdir_failed", error=str(exc))

        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            # setting_sources=[] disables inheriting the operator's user /
            # project / local Claude Code settings (CLAUDE.md, agents/, MCP
            # servers, skills). Without this, ANNA pulls in the entire host
            # Claude Code environment and starts responding as if she were
            # the operator's primary agent. She must speak strictly from her
            # own ~/anna/core files.
            setting_sources=[],
            # ANNA runs as a headless systemd service with no operator at a
            # terminal to approve tool calls. The default permission_mode is
            # interactive prompting, which means every tool call hangs forever
            # waiting for an OK that never comes. The config default is
            # bypassPermissions; tighten in anna.yaml if needed.
            permission_mode=self._config.runtime.permission_mode,
            # In-process MCP servers. Dict keys become the MCP server
            # prefixes in the SDK's allowed_tools naming convention
            # (``mcp__<server>__<tool>``). anna_self_edit is always
            # mounted; anna_google only when google.enabled and the
            # runtime provided a GoogleClients handle; anna_web only
            # when tools.enabled.
            mcp_servers=mcp_servers,
            # Allow the default filesystem tools, the self-edit MCP tools,
            # and (when wired) the google, web, and delegate MCP tools.
            allowed_tools=_allowed_tool_names(
                include_google=include_google,
                include_web=include_web,
                include_delegate=include_delegate,
            ),
            # Vault root is the natural cwd: vault paths become relative
            # (Conversations/foo.md instead of long absolutes).
            cwd=str(vault_root),
            # add_dirs lets the SDK see core/ as a readable workspace. ANNA
            # should still prefer the MCP tools for core writes because they
            # take the supervisor lock, but Read/Glob over core/ is fine and
            # is the only way she can quote her own files back to the
            # operator.
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
            f"Your markdown vault root is {vault_root}. Conversations/, "
            f"Identity/ archives, and SubAgents/ scratch notes live here. "
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
        # ``pending`` accumulates text since the last flush boundary so
        # buffered transports (Slack, Telegram) receive narration as a
        # sequence of messages keyed off the model's natural tool-use
        # cadence instead of one consolidated end-of-turn blob. The
        # scheduler-driven path (event.completion_future set) keeps the
        # old behavior — scheduled jobs want one return value, not a
        # stream.
        reply_chunks: list[str] = []
        pending: list[str] = []
        try:
            # Send the user message into the SDK. NOTE: ``query_text``
            # (not ``event.text``) carries the cadence reminder when
            # one was loaded.
            try:
                await self._client.query(query_text)  # type: ignore[attr-defined]
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
                                pending.append(block.text)
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
                                if event.completion_future is None and pending:
                                    txt = "\n".join(c for c in pending if c).strip()
                                    if txt:
                                        await self._send(OutboundMessage(
                                            conversation_key=event.conversation_key,
                                            text=txt,
                                        ))
                                    pending = []
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
            event.completion_future.set_result(reply_text)
            return

        # Interactive path: send the trailing pending buffer (text after the
        # last tool-use boundary, or the full reply if no tools were called).
        # Earlier tool-use flushes already dispatched their slices of
        # narration as separate OutboundMessages — sending the join of
        # ``reply_chunks`` here would duplicate everything.
        final_text = "\n".join(c for c in pending if c).strip()
        if final_text:
            await self._send(OutboundMessage(
                conversation_key=event.conversation_key,
                text=final_text,
            ))
        elif not reply_chunks:
            # Edge case: the SDK returned no text at all and no tools were
            # called — preserve the "(no response)" fallback so the
            # operator sees SOMETHING. If reply_chunks is non-empty but
            # final_text is empty, that means every text block was already
            # flushed at a tool-use boundary; nothing more to send.
            await self._send(OutboundMessage(
                conversation_key=event.conversation_key,
                text="(no response)",
            ))
