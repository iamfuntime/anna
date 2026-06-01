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
from anna.tools.google_server import GOOGLE_TOOL_NAMES, GoogleTools, build_google_server
from anna.tools.self_edit_server import SELF_EDIT_TOOL_NAMES, SelfEditTools, build_self_edit_server
from anna.tools.vault_tools import VaultTools
from anna.tools.web_server import WEB_TOOL_NAMES, build_web_server
from anna.tools.web_tools import WebTools
from anna.transports.base import InboundEvent, OutboundMessage
from anna.vault.checkpoint import list_recent_checkpoints, write_checkpoint

if TYPE_CHECKING:
    from anna.runtime.schedule_store import ScheduleStore
    from anna.runtime.supervisor import Supervisor
    from anna.tools.google_clients import GoogleClients


# Default file-system tools we hand to ANNA so she can read and write her
# vault. Listed by their canonical SDK names. The MCP self-edit tools are
# prefixed with ``mcp__anna_self_edit__`` per the SDK convention.
_DEFAULT_FS_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")
_SELF_EDIT_PREFIX = "mcp__anna_self_edit__"
_GOOGLE_PREFIX = "mcp__anna_google__"
_WEB_PREFIX = "mcp__anna_web__"


def _allowed_tool_names(*, include_google: bool, include_web: bool) -> list[str]:
    names = list(_DEFAULT_FS_TOOLS) + [
        f"{_SELF_EDIT_PREFIX}{name}" for name in SELF_EDIT_TOOL_NAMES
    ]
    if include_google:
        names.extend(f"{_GOOGLE_PREFIX}{name}" for name in GOOGLE_TOOL_NAMES)
    if include_web:
        names.extend(f"{_WEB_PREFIX}{name}" for name in WEB_TOOL_NAMES)
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
    ) -> None:
        self.conversation_key = conversation_key
        self.transport = transport
        self._config = config
        self._supervisor = supervisor
        self._send = send
        self._on_idle_close = on_idle_close
        self._schedule_store = schedule_store
        self._google_clients = google_clients
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
        """Idle threshold for this worker, picking dm vs thread gap."""
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
            # and (when wired) the google and web MCP tools.
            allowed_tools=_allowed_tool_names(
                include_google=include_google,
                include_web=include_web,
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

    async def _closeout(self) -> None:
        """Per v3 §6: write a checkpoint, then run eviction on every core file.

        Called from :meth:`stop` before the SDK client is closed. The
        ``_closed_out`` flag guarantees this only runs once even if stop()
        is invoked twice (e.g. the idle watcher and the router shutdown
        both fire).
        """
        self._log.info("worker.closeout.start", conv_key=self.conversation_key)

        # ----- 1. Checkpoint summary --------------------------------------
        summary = await self._ask_checkpoint_summary()

        try:
            ckpt_path = write_checkpoint(
                vault_root=self._config.vault.resolved_path,
                transport=self.transport,
                conversation_key=self.conversation_key,
                summary=summary,
                operator_short_name=self._operator_short_name,
            )
            audit_event(
                "audit.checkpoint.written",
                audit_dir=self._config.audit_dir,
                actor="anna",
                conv_key=self.conversation_key,
                fsync_on_write=self._config.logging.audit.fsync_on_write,
                checkpoint_file=str(ckpt_path),
                summary_chars=len(summary),
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
            )

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

        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        except ImportError:
            AssistantMessage = ResultMessage = TextBlock = None  # type: ignore[assignment,misc]

        # Send the user message into the SDK.
        try:
            await self._client.query(event.text)  # type: ignore[attr-defined]
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

        # Collect text blocks until ResultMessage.
        reply_chunks: list[str] = []
        try:
            async for msg in self._client.receive_response():  # type: ignore[attr-defined]
                if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if TextBlock is not None and isinstance(block, TextBlock):
                            reply_chunks.append(block.text)
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

        reply_text = "\n".join(c for c in reply_chunks if c).strip()
        if not reply_text:
            reply_text = "(no response)"

        # Scheduler-driven (or any future caller-driven) dispatch short-circuits
        # the normal send path. The caller awaits the future and routes the
        # output itself. Transport-originated events have completion_future
        # unset and use the standard send-back path.
        if event.completion_future is not None and not event.completion_future.done():
            event.completion_future.set_result(reply_text)
            return

        await self._send(OutboundMessage(
            conversation_key=event.conversation_key,
            text=reply_text,
        ))
