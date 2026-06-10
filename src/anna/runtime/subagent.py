"""Phase 2 §3 sub-agent spawn runtime.

ANNA's *hire half* (Phase 1) writes persona files at
``$ANNA_HOME/agents/<slug>.md`` via :class:`anna.agents.registry.SubAgentRegistry`.
This module is the *run half*: it takes a slug + a task and spawns a
fresh :class:`claude_agent_sdk.ClaudeSDKClient` carrying the persona
plus any matching skill files from ``$ANNA_HOME/skills/<slug>/``.

The runtime is mounted on the primary worker through a separate
``anna_delegate`` MCP server. Sub-agents do not inherit that server —
the depth-protection invariant ("one level only") is enforced at the
runtime level by simply not mounting ``anna_delegate`` on a sub-agent's
options.

This file currently provides the skeleton: the runner class wires up
dependencies and the semaphore, declares :class:`DelegateResult` and
:class:`SubAgentError`, and stubs :meth:`SubAgentRunner.delegate` until
subsequent subtasks fill in persona loading, prompt assembly, options
building, transcript writing, and the actual SDK invocation.

See ``Inbox/2026-06-01-ANNA-Phase-2-Subagent-Runtime-Plan.md`` for the
full design.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from anna.config import AgentGrants, AnnaConfig
from anna.log import audit_event, get_logger
from anna.runtime.frontmatter import split_frontmatter

if TYPE_CHECKING:
    from anna.agents.registry import SubAgentRegistry
    from anna.runtime.grants import ResolvedGrant
    from anna.runtime.supervisor import Supervisor
    from anna.skills.registry import SkillRegistry


# Callback the runner invokes when a *background* delegation completes. It
# receives the originating conversation's transport + conv_key and the
# already-formatted completion text (reply + YAML trailer) and is
# responsible for delivering that text back into the originating
# conversation as a NEW inbound turn so the harness re-invokes ANNA. The
# router supplies this at boot via :meth:`SubAgentRunner.set_delivery`.
# Signature: ``(transport, conv_key, text) -> Awaitable[None]``.
BackgroundDeliveryCallback = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True)
class DelegateResult:
    """Return value of :meth:`SubAgentRunner.delegate`.

    Exposes the fields the ``anna_delegate`` MCP tool serializes back to
    the calling worker:

    * ``text`` — the sub-agent's final reply, concatenated assistant text
      blocks.
    * ``transcript_path`` — absolute path to the JSONL transcript file
      this run appended to.
    * ``tool_calls`` — list of tool names the sub-agent invoked, in
      order. Empty if no tools fired.
    * ``cost_usd`` — total cost reported by the SDK's ``ResultMessage``.
    * ``duration_ms`` — wall-clock duration of the SDK round-trip,
      measured around ``client.query`` + ``receive_response``.
    * ``status`` — one of ``ok``, ``timeout``, ``error``, ``not_found``,
      ``depth_violation``, ``concurrency_timeout``. The non-``ok``
      variants exist so a future caller can branch without re-parsing
      :class:`SubAgentError` strings.

    The internal ``audit_id`` (uuid stamped on spawn / complete / fail
    audit events) and the ``slug`` are tracked on the runner side and
    not exposed in the return value — MCP callers do not need them, and
    leaking them into the tool response only invites the model to cite
    them as if they were operator-facing identifiers.
    """

    text: str
    transcript_path: Path
    tool_calls: list[str]
    cost_usd: float
    duration_ms: int
    status: str


@dataclass(frozen=True)
class PersonaDoc:
    """Parsed persona file: stripped body + optional frontmatter grants.

    ``_load_persona`` peels any leading ``---``-fenced YAML off the persona
    file. ``body`` is the markdown that gets spliced into the system prompt
    (the fence is never visible to the model). ``grants`` is the parsed
    ``grants:`` frontmatter block as an :class:`AgentGrants`, or ``None`` when
    the file has no frontmatter, no ``grants`` key, or a malformed grants
    block (which is logged). The grants are threaded through ``delegate`` for
    a later wiring pass (subtask 7) — they do not yet affect spawned options.
    """

    body: str
    grants: AgentGrants | None


class SubAgentError(Exception):
    """Raised by :meth:`SubAgentRunner.delegate` for any failure case.

    Both spawn-time failures (missing persona, depth violation,
    semaphore starvation) and mid-run failures (timeout, SDK exception)
    surface as :class:`SubAgentError` so callers branch on ``.kind``
    rather than on exception type.

    ``kind`` is one of:

    * ``not_found`` — persona file at ``$ANNA_HOME/agents/<slug>.md``
      did not exist.
    * ``depth_violation`` — the caller is itself a sub-agent
      (``parent_conv_key`` starts with ``subagent:``). Mostly defensive
      since the ``anna_delegate`` server is never mounted on a
      sub-agent's options.
    * ``concurrency_timeout`` — the runner could not acquire a
      semaphore slot within
      ``config.subagents.concurrency_acquire_timeout_seconds``.
    * ``timeout`` — the per-delegation wall-clock cap elapsed mid-run.
    * ``error`` — any other exception during ``query`` or
      ``receive_response``; ``reason`` carries the ``repr`` of the
      underlying exception.

    Caught and rendered as the tool's text response in
    ``build_delegate_server``; never propagates out of the worker.
    """

    def __init__(
        self,
        kind: str,
        *,
        reason: str | None = None,
    ) -> None:
        self.kind = kind
        self.reason = reason
        message = kind if reason is None else f"{kind}: {reason}"
        super().__init__(message)


class SubAgentRunner:
    """Owns the concurrency semaphore and the per-call SDK client lifecycle.

    One instance lives at the process level (instantiated in
    ``anna.__main__`` and threaded through the router into every
    worker). The runner is *not* per-conversation; the
    :meth:`delegate` method receives the caller's ``conv_key`` so the
    audit linkage and transcript routing can stamp the parent.

    Subsequent subtasks fill in:

    * ``_load_persona`` / ``_load_skills`` (subtask 3)
    * ``_build_system_prompt`` (subtask 4)
    * ``_build_subagent_options`` (subtask 5)
    * ``delegate`` happy path + audit events (subtask 6)
    * ``_write_transcript_line`` (subtask 7)
    * failure paths + structured ``status`` mapping (subtask 8)
    """

    def __init__(
        self,
        *,
        config: AnnaConfig,
        supervisor: "Supervisor",
        agents_registry: "SubAgentRegistry",
        skills_registry: "SkillRegistry",
    ) -> None:
        self._config = config
        self._supervisor = supervisor
        self._agents_registry = agents_registry
        self._skills_registry = skills_registry
        self._log = get_logger("anna.subagent")
        # Process-wide cap on simultaneously-running sub-agents. A
        # delegate call that cannot acquire within
        # ``concurrency_acquire_timeout_seconds`` fails with
        # status=concurrency_timeout (subtask 8).
        self._semaphore = asyncio.Semaphore(config.subagents.max_concurrent)
        self._audit_dir: Path = config.audit_dir
        self._fsync: bool = config.logging.audit.fsync_on_write
        # Background-delegation state. ``_delivery`` is the callback the
        # router installs at boot so a completed background job can route
        # its result back into the originating conversation as a new
        # inbound turn. ``_background_jobs`` tracks in-flight detached
        # tasks so :meth:`drain_background_jobs` (called from
        # ``ConversationRouter.shutdown``) can await them on SIGTERM
        # rather than leaving them orphaned.
        self._delivery: BackgroundDeliveryCallback | None = None
        self._background_jobs: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Background delegation (async / detached)
    # ------------------------------------------------------------------

    def set_delivery(self, delivery: BackgroundDeliveryCallback) -> None:
        """Install the completion-delivery callback for background jobs.

        Called once at boot from :meth:`ConversationRouter.__init__` after
        both the runner and the router exist (the router cannot be passed
        into the runner's constructor without an import cycle, and the
        runner is constructed before the router). The callback delivers a
        finished background delegation's formatted text back into the
        originating conversation as a new inbound turn.
        """
        self._delivery = delivery

    def start_background(
        self,
        *,
        agent_slug: str,
        task: str,
        parent_conv_key: str,
        parent_transport: str,
        context: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """Fire a delegation detached and return a job id immediately.

        Unlike :meth:`delegate`, this does not block the caller on the
        sub-agent run. It schedules :meth:`_run_background` as a tracked
        asyncio task and returns a freshly-minted ``job_id`` right away.
        When the sub-agent finishes (success OR failure), the task formats
        the outcome and hands it to the installed delivery callback, which
        injects it back into ``parent_conv_key`` as a new inbound turn so
        ANNA is re-invoked to read and act on it.

        The job still flows through :meth:`delegate`, so it respects the
        process-wide concurrency semaphore exactly as a synchronous call
        does — a background job that cannot acquire a slot waits (and may
        ultimately fail with ``concurrency_timeout``), and the delivered
        turn carries that failure.

        Args:
            agent_slug: Persona slug.
            task: Free-text task body.
            parent_conv_key: Originating conversation key; the completion
                turn is delivered here.
            parent_transport: Originating transport (``slack``,
                ``telegram``, ``cli``, ...). Threaded through so the
                synthetic delivery event resolves the right send adapter
                even if the originating worker idled out in the meantime.
            context: Optional structured context dict.
            timeout_seconds: Per-call wall-clock cap; None → config
                default.

        Returns:
            The ``job_id`` (a uuid4 hex string) the caller surfaces to
            ANNA immediately.
        """
        job_id = uuid.uuid4().hex
        self._audit(
            "audit.subagent.background_start",
            parent_conv=parent_conv_key,
            slug=agent_slug,
            job_id=job_id,
            task=task[:500],
        )
        task_obj = asyncio.create_task(
            self._run_background(
                job_id=job_id,
                agent_slug=agent_slug,
                task=task,
                parent_conv_key=parent_conv_key,
                parent_transport=parent_transport,
                context=context,
                timeout_seconds=timeout_seconds,
            ),
            name=f"subagent.background.{agent_slug}.{job_id}",
        )
        self._background_jobs.add(task_obj)
        task_obj.add_done_callback(self._background_jobs.discard)
        return job_id

    async def _run_background(
        self,
        *,
        job_id: str,
        agent_slug: str,
        task: str,
        parent_conv_key: str,
        parent_transport: str,
        context: dict[str, Any] | None,
        timeout_seconds: int | None,
    ) -> None:
        """Run a detached delegation and deliver its outcome back upstream.

        Both the success and failure branches format a self-contained
        message (the sub-agent reply or a failure line, plus the job_id /
        agent_slug header and the standard YAML trailer) and hand it to
        the delivery callback. Cancellation (SIGTERM drain) is allowed to
        propagate so the drain can complete promptly; the audit
        background_complete event is still emitted for the terminal
        success/failure paths.
        """
        # Local import avoids a module-level cycle (delegate_server imports
        # from this module).
        from anna.tools.delegate_server import (
            format_background_failure,
            format_background_success,
        )

        try:
            result = await self.delegate(
                agent_slug=agent_slug,
                task=task,
                parent_conv_key=parent_conv_key,
                context=context,
                timeout_seconds=timeout_seconds,
            )
        except SubAgentError as exc:
            text = format_background_failure(
                job_id=job_id,
                agent_slug=agent_slug,
                kind=exc.kind,
                detail=str(exc),
            )
            self._audit(
                "audit.subagent.background_complete",
                parent_conv=parent_conv_key,
                level="WARNING",
                slug=agent_slug,
                job_id=job_id,
                status=exc.kind,
            )
            await self._deliver(parent_transport, parent_conv_key, text, job_id)
            return
        except Exception as exc:  # noqa: BLE001
            # A RAW exception from any step that runs after semaphore
            # acquisition but is NOT wrapped as a SubAgentError (skill
            # load, system-prompt build, options build, spawn audit,
            # transcript write). Without this branch the task would die
            # silently: no failure turn delivered, no background_complete
            # audit. Mirror the SubAgentError branch so the operator
            # always learns the job failed, just with status="error" and
            # a message making clear it was unexpected.
            status = type(exc).__name__ or "error"
            text = format_background_failure(
                job_id=job_id,
                agent_slug=agent_slug,
                kind="error",
                detail=f"unexpected error: {exc!r}",
            )
            self._audit(
                "audit.subagent.background_complete",
                parent_conv=parent_conv_key,
                level="WARNING",
                slug=agent_slug,
                job_id=job_id,
                status=status,
            )
            await self._deliver(parent_transport, parent_conv_key, text, job_id)
            return

        text = format_background_success(
            job_id=job_id,
            agent_slug=agent_slug,
            result=result,
        )
        self._audit(
            "audit.subagent.background_complete",
            parent_conv=parent_conv_key,
            slug=agent_slug,
            job_id=job_id,
            status=result.status,
            duration_ms=result.duration_ms,
            output_length=len(result.text),
        )
        await self._deliver(parent_transport, parent_conv_key, text, job_id)

    async def _deliver(
        self,
        transport: str,
        conv_key: str,
        text: str,
        job_id: str,
    ) -> None:
        """Hand a completed job's text to the delivery callback, if set.

        Delivery failures are logged but never raised — a background job
        that cannot reach the router must not crash the runner's task or
        wedge the drain. Best-effort by design, mirroring the alerter and
        send-path error handling elsewhere in the runtime.
        """
        if self._delivery is None:
            self._log.warning(
                "subagent.background.no_delivery",
                job_id=job_id,
                conv_key=conv_key,
            )
            return
        try:
            await self._delivery(transport, conv_key, text)
        except Exception as exc:  # noqa: BLE001
            self._log.error(
                "subagent.background.delivery_failed",
                job_id=job_id,
                conv_key=conv_key,
                error=str(exc),
            )

    async def drain_background_jobs(self, timeout: float = 10.0) -> None:
        """Await in-flight background jobs so SIGTERM does not orphan them.

        Called from :meth:`ConversationRouter.shutdown` before the worker
        registry is torn down. Waits up to ``timeout`` seconds for every
        tracked job to finish delivering its result; jobs that have not
        landed by then are cancelled so the loop teardown does not hang.
        """
        jobs = list(self._background_jobs)
        if not jobs:
            return
        self._log.info("subagent.background.drain.start", inflight=len(jobs))
        try:
            await asyncio.wait_for(
                asyncio.gather(*jobs, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._log.warning(
                "subagent.background.drain.timeout",
                inflight=len(self._background_jobs),
            )
            for job in list(self._background_jobs):
                job.cancel()
            # Give cancellation a beat to settle.
            await asyncio.gather(*self._background_jobs, return_exceptions=True)
        self._background_jobs.clear()
        self._log.info("subagent.background.drain.complete")

    # ------------------------------------------------------------------
    # Persona + skills loading (subtask 3)
    # ------------------------------------------------------------------

    def _load_persona(self, slug: str) -> PersonaDoc:
        """Read ``$ANNA_HOME/agents/<slug>.md`` and split body from grants.

        Reads on every call — no caching — so the operator can edit a
        persona file without restarting ANNA. The registry is *not*
        consulted because :class:`SubAgentRegistry` exposes only
        ``list_personas()`` and a single-slug lookup would either need
        a linear scan or a new ``get(slug)`` method; the runner skips
        both by reading the well-known path directly.

        Any leading ``---``-fenced YAML frontmatter is peeled off via
        :func:`split_frontmatter`. The returned ``body`` is the markdown
        with the fence removed (so the fence never reaches the system
        prompt); ``grants`` is the parsed ``grants:`` block as an
        :class:`AgentGrants`, or ``None`` when absent or malformed.

        Returns:
            A :class:`PersonaDoc`. ``body`` may be empty if the file exists
            but is blank.

        Raises:
            :class:`SubAgentError`: when the persona file does not
                exist. The error message is the literal string
                ``"not_found"`` to match the status field on a
                ``DelegateResult`` of the same shape.
        """
        path = self._config.anna_home / "agents" / f"{slug}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SubAgentError("not_found") from exc

        body, meta = split_frontmatter(text)
        grants = self._parse_grants(slug, meta)
        return PersonaDoc(body=body, grants=grants)

    def _parse_grants(self, slug: str, meta: dict[str, Any]) -> AgentGrants | None:
        """Tolerantly parse a frontmatter ``grants:`` block into AgentGrants.

        Unknown keys inside the block are ignored (pydantic drops extras by
        default). A malformed block (wrong type, or values that fail
        validation) degrades to ``None`` + a WARNING so a bad header can
        never break a delegation.
        """
        raw = meta.get("grants")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            self._log.warning(
                "subagent.persona.grants.malformed",
                slug=slug,
                reason="grants is not a mapping",
            )
            return None
        try:
            return AgentGrants.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - tolerate any validation error
            self._log.warning(
                "subagent.persona.grants.malformed",
                slug=slug,
                reason=str(exc),
            )
            return None

    def _load_skills(self, slug: str) -> list[str]:
        """Return skill body texts for the given agent slug, alphabetical order.

        Walks ``$ANNA_HOME/skills/<slug>/`` for ``*.md`` files, reads
        each, and returns the bodies sorted alphabetically by skill
        slug (filename stem). The sort matters because the spliced
        prompt is otherwise dependent on filesystem iteration order,
        which varies by OS and makes test assertions flaky.

        Missing skills directory is not an error — most personas ship
        without skills until the operator iterates them in. Returns
        ``[]`` in that case.
        """
        skills_dir = self._config.anna_home / "skills" / slug
        if not skills_dir.is_dir():
            return []
        bodies: list[str] = []
        for path in sorted(skills_dir.glob("*.md")):
            bodies.append(path.read_text(encoding="utf-8"))
        return bodies

    # ------------------------------------------------------------------
    # System prompt assembly (subtask 4)
    # ------------------------------------------------------------------

    # Default sub-agent server-tool surface, used when no resolved grant is
    # threaded in (the empty-config / backward-compat case). Kept as a
    # constant so the empty-case prompt stays byte-identical to the pre-
    # chunk-A wording while a real grant can override it (subtask 8).
    _DEFAULT_SERVER_TOOLS = "web_search, web_fetch, and vault_download"

    @staticmethod
    def _build_system_prompt(
        persona: str,
        skills: list[str],
        task: str,
        context: dict[str, Any] | None,
        vault_root: Path,
        extra_dirs: list[str] | None = None,
        server_tools: str | None = None,
    ) -> str:
        """Splice the persona, skills, delegation framing, task, and optional context.

        Pure function — no I/O, no logger, no clock. The shape is:

        ```
        <persona verbatim>

        # Skills
        <skill_1_text>

        <skill_2_text>

        # Delegation context
        <fixed framing block naming the available tools and vault root>

        # Task
        <task>

        # Context
        <yaml.safe_dump(context)>     ← only when context is not None
        ```

        Skills are concatenated by blank lines with no per-skill
        heading — each skill file already includes its own headings,
        and an extra ``## <slug>`` wrapper would compound that.

        The ``# Skills`` section is omitted entirely when there are no
        skills, so a persona-only sub-agent gets a tidy prompt.
        Similarly, ``# Context`` is only present when the caller passed
        a non-None dict.

        ``extra_dirs`` names the RESOLVED write dirs (from the effective
        grant) and ``server_tools`` is a short human phrase naming the
        RESOLVED MCP server tool surface (subtask 8). Both fall back to the
        pre-chunk-A wording when absent so an ungranted sub-agent gets a
        byte-identical prompt to before.
        """
        import yaml

        extra_dirs = [str(Path(d).expanduser()) for d in (extra_dirs or [])]
        reach_line = (
            f"Your file ops (Read, Write, Edit, Glob, Grep) can reach the "
            f"ANNA vault ({vault_root})"
        )
        if extra_dirs:
            reach_line += (
                " and these additional mounted roots: "
                + ", ".join(extra_dirs)
                + " (e.g. the collaborative Brain vault — read detection "
                "templates, query libraries, and example reports there, "
                "and write outputs to its Inbox when the task asks)"
            )
        reach_line += "."
        tools_phrase = (
            server_tools
            if server_tools is not None
            else SubAgentRunner._DEFAULT_SERVER_TOOLS
        )
        delegation_block = (
            "You are running as a one-shot sub-agent spawned by ANNA. You "
            "do not have the delegate tool; you cannot spawn further "
            "sub-agents. You have "
            + tools_phrase
            + " for outside-the-vault work. "
            + reach_line
            + " Your reply is returned to the parent agent as a single "
            "tool result; write the final answer as one message."
        )

        sections: list[str] = [persona.rstrip()]
        if skills:
            skills_body = "\n\n".join(s.strip() for s in skills if s.strip())
            if skills_body:
                sections.append(f"# Skills\n{skills_body}")
        sections.append(f"# Delegation context\n{delegation_block}")
        sections.append(f"# Task\n{task.strip()}")
        if context is not None:
            # default_flow_style=False keeps YAML readable; sort_keys
            # for determinism (a context dict built from operator data
            # can have insertion-order keys that flap across runs).
            context_yaml = yaml.safe_dump(
                context,
                default_flow_style=False,
                sort_keys=True,
            ).rstrip()
            sections.append(f"# Context\n{context_yaml}")
        return "\n\n".join(sections)

    @staticmethod
    def _summarize_server_tools(resolved: "ResolvedGrant") -> str | None:
        """Human phrase naming the resolved sub-agent server tool surface.

        Threaded into :meth:`_build_system_prompt` as ``server_tools`` so the
        delegation framing matches the actual grant (subtask 8). Returns
        ``None`` for the fallback-only case (a single ``anna_web`` builtin)
        so the prompt stays byte-identical to the pre-chunk-A wording. For
        any other resolved surface, returns a short comma list:

        * ``anna_web`` → its three concrete tool names (web_search,
          web_fetch, vault_download).
        * an external (stdio/http) server → the server name, optionally with
          its pinned ``tool_names`` in parens.

        With no servers resolved at all, returns the empty-string phrase
        ``"no outside-the-vault tools"`` so the prompt does not claim a
        surface the sub-agent lacks.
        """
        names = [name for name, _ in resolved.mcp_specs]
        # Fallback-only: exactly the implicit anna_web builtin. Keep the
        # original wording (return None → _DEFAULT_SERVER_TOOLS).
        if names == ["anna_web"] and resolved.mcp_specs[0][1].kind == "builtin":
            return None

        if not resolved.mcp_specs:
            return "no outside-the-vault tools"

        parts: list[str] = []
        for name, spec in resolved.mcp_specs:
            if spec.kind == "builtin" and spec.builtin_name == "anna_web":
                parts.append("web_search, web_fetch, vault_download")
            elif spec.tool_names:
                parts.append(f"{name} ({', '.join(spec.tool_names)})")
            else:
                parts.append(name)
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Sub-agent ClaudeAgentOptions builder (subtask 5)
    # ------------------------------------------------------------------

    def _build_claude_env(self) -> dict[str, str]:
        """Env overrides for the spawned sub-agent CLI subprocess.

        ``CLAUDE_CONFIG_DIR`` relocates host CLAUDE.md / skills / plugins /
        local-MCP discovery onto ANNA's isolated runtime dir. In max mode we
        ALSO set ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` to the operator's real
        ~/.claude so credential reads and the OAuth refresh-write share the
        operator's ``.credentials.json``. In api_key mode the key comes from
        the inherited env, so the securestorage knob is left unset (mirroring
        the main-session worker and the old max-mode-only credentials symlink).
        """
        env = {"CLAUDE_CONFIG_DIR": str(self._config.claude_runtime_dir)}
        if self._config.auth.mode == "max":
            env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(
                self._config.claude_securestorage_dir
            )
        return env

    def _build_subagent_options(
        self,
        system_prompt: str,
        conv_key: str,
        permission_mode_override: str | None = None,
        resolved: "ResolvedGrant | None" = None,
    ) -> Any:
        """Construct the ``ClaudeAgentOptions`` used to spawn the sub-agent client.

        This is also where the depth-protection invariant is enforced
        at the runtime level: ``anna_self_edit``, ``anna_google``, and
        ``anna_delegate`` are *never* mounted on a sub-agent's options.
        That invariant lives in :func:`anna.runtime.grants.build_mcp_servers`
        — those three builtins are absent from its dispatch table and a
        registry entry naming one is dropped. This method never builds an
        MCP server by any other path, so there is no way to smuggle one in.

        Args:
            system_prompt: Output of :meth:`_build_system_prompt`.
            conv_key: Synthetic conv_key for this delegation; flows
                into the per-builtin server closures so the tool calls
                that fire from inside the sub-agent get audit-stamped
                with a distinct identifier.
            permission_mode_override: Optional per-call permission
                mode. When set it wins over the resolved grant's mode;
                otherwise the resolved grant supplies it (default
                ``acceptEdits``).
            resolved: The effective :class:`ResolvedGrant` for this
                delegation. ``None`` means "no grant" and reproduces the
                pre-chunk-A behavior exactly: ``anna_web`` only (when
                ``tools.enabled``), ``add_dirs`` from
                ``subagents.extra_dirs``, ``allowed_tools`` from
                ``subagents.allowed_tools``. The synthesized fallback grant
                now also carries ``model=config.runtime.model`` (the global
                default; ``None`` = inherit the CLI default) — desired, so an
                override-less sub-agent runs the same model as the main loop.

        Returns:
            ``ClaudeAgentOptions`` ready to feed into ``ClaudeSDKClient``.
        """
        # Lazy import so the unit tests in this module do not pull in
        # the SDK transitively. Mirrors ``ConversationWorker._build_options``.
        from claude_agent_sdk import ClaudeAgentOptions

        from anna.runtime.grants import (
            build_mcp_servers,
            resolve_effective_grant,
        )

        vault_root = self._config.vault.resolved_path

        # No explicit grant → synthesize the fallback grant so this method
        # behaves exactly as it did before chunk A. resolve_effective_grant
        # with no per-agent / frontmatter layers yields today's fallback
        # (anna_web when tools.enabled, extra_dirs, subagents.allowed_tools).
        if resolved is None:
            resolved = resolve_effective_grant(self._config, "", None)

        # Bind the resolved MCP specs into the SDK dict + tool additions.
        # build_mcp_servers is the SINGLE path that constructs MCP servers
        # for a sub-agent; it structurally excludes the forbidden trio.
        mcp_servers, extra_tool_names = build_mcp_servers(
            self._config, resolved.mcp_specs, conv_key
        )

        permission_mode = permission_mode_override or resolved.permission_mode

        # allowed_tools = the grant's tool surface UNION the MCP tool
        # additions, deduped with a stable (first-seen) order so the option
        # set is deterministic across runs and the existing tests can assert
        # on a sorted comparison.
        allowed_tools: list[str] = []
        for name in [*resolved.allowed_tools, *extra_tool_names]:
            if name not in allowed_tools:
                allowed_tools.append(name)

        # write_dirs are already absolute / ~-expanded by the resolver;
        # guard with expanduser defensively in case a future caller passes
        # a tilde path through.
        add_dirs = [str(Path(d).expanduser()) for d in resolved.write_dirs]

        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            # No setting_sources for sub-agents — they live entirely off
            # their persona file plus skills. setting_sources=[] gates the
            # host settings.json; CLAUDE_CONFIG_DIR (below) relocates the
            # CLI's memory/skills/plugins/local-MCP discovery off the
            # operator's ~/.claude so no host Claude Code env leaks in.
            setting_sources=[],
            env=self._build_claude_env(),
            permission_mode=permission_mode,
            # Claude model for this sub-agent, resolved across the three grant
            # layers (runtime.model fallback → anna.yaml agents.<slug>.model →
            # frontmatter grants.model). None inherits the CLI/account default.
            model=resolved.model,
            # Resolved MCP servers only. Never anna_self_edit, anna_google,
            # or anna_delegate — build_mcp_servers enforces that.
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            cwd=str(vault_root),
            # add_dirs grants extra reachable roots beyond the cwd (ANNA
            # vault). Driven by the resolved grant's write_dirs (fallback:
            # subagents.extra_dirs) — typically the collaborative Brain
            # vault. Still never mounts core/: ANNA's identity files stay
            # invisible because core/ is not in any blessed dir_pool entry.
            add_dirs=add_dirs,
        )

    # ------------------------------------------------------------------
    # Transcript writer (subtask 7)
    # ------------------------------------------------------------------

    def _write_transcript_line(
        self,
        slug: str,
        conv_key: str,
        direction: str,
        text: str,
        audit_id: str,
        **fields: Any,
    ) -> Path:
        """Append one JSON line to the per-slug transcript for today.

        Sub-agent transcripts coalesce under
        ``$ANNA_HOME/transcripts/<subagents.transcript_subdir>/<slug>/<YYYY-MM-DD>.jsonl``
        rather than the per-conv_key tree the main transcript writer
        uses. Each delegation typically appends three lines: a ``task``
        line on spawn, an ``outbound`` line on success, and a ``fail``
        line on error paths.

        No threading lock is needed — one writer per delegation,
        slug+day scoped paths. The shape mirrors
        :func:`anna.log.transcript_event` but the path is set by the
        runner rather than derived from the conv_key.

        Args:
            slug: Sub-agent slug; becomes the directory name.
            conv_key: Synthetic sub-agent conv_key
                (``subagent:<slug>:<uuid>``).
            direction: ``task``, ``outbound``, or ``fail``.
            text: Body content for the line.
            audit_id: UUID shared with the matching
                ``audit.subagent.*`` event so the operator can
                cross-reference.
            **fields: Extra fields (``parent_conv``,
                ``duration_seconds``, etc.). Merged into the JSON
                record verbatim.

        Returns:
            The absolute path of the file appended to.
        """
        day = date.today().isoformat()
        path = self._config.subagent_transcript_dir / slug / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "conv_key": conv_key,
            "text": text,
            "audit_id": audit_id,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
        return path

    def _audit(
        self,
        event: str,
        *,
        parent_conv: str,
        level: str = "INFO",
        **fields: Any,
    ) -> None:
        """Wrap :func:`audit_event` with the runner's own audit dir + fsync."""
        audit_event(
            event,
            audit_dir=self._audit_dir,
            actor="anna",
            conv_key=parent_conv,
            fsync_on_write=self._fsync,
            level=level,
            **fields,
        )

    async def delegate(
        self,
        *,
        agent_slug: str,
        task: str,
        parent_conv_key: str,
        context: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> DelegateResult:
        """Spawn the sub-agent, run the task, return the reply.

        Implements the full 12-step flow from the §3 plan: acquire the
        semaphore (bounded wait), load persona + skills, build prompt
        and options, open a fresh ``ClaudeSDKClient``, send the task,
        drain ``receive_response`` until ``ResultMessage``, write
        transcript lines, close the client, release the semaphore, and
        return a :class:`DelegateResult`.

        Args:
            agent_slug: Persona slug — selects
                ``$ANNA_HOME/agents/<slug>.md`` and
                ``$ANNA_HOME/skills/<slug>/``.
            task: The free-text task body the sub-agent should solve.
            parent_conv_key: Caller's conv_key, persisted on audit
                events and the first transcript line so the operator
                can cross-reference a delegation with its parent
                conversation.
            context: Optional dict rendered as a YAML block in the
                sub-agent's system prompt.
            timeout_seconds: Per-call wall-clock cap. None falls back
                to ``config.subagents.default_timeout_seconds``.

        Returns:
            :class:`DelegateResult` with ``status="ok"`` on success.

        Raises:
            :class:`SubAgentError`: for missing persona, depth
                violation, concurrency-acquire timeout, per-delegation
                timeout, or any SDK-level failure during query /
                receive.
        """
        # Depth-protection: sub-agents do not get the anna_delegate
        # MCP server in their options, so this branch is defensive.
        # The check runs before semaphore acquisition so a recursive
        # call cannot even queue up.
        if parent_conv_key.startswith("subagent:"):
            self._audit(
                "audit.subagent.fail",
                parent_conv=parent_conv_key,
                level="WARNING",
                slug=agent_slug,
                kind="depth_violation",
                reason="caller is itself a sub-agent",
            )
            raise SubAgentError("depth_violation")

        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._config.subagents.default_timeout_seconds
        )

        # Step 1: acquire semaphore with bounded wait.
        acquire_timeout = (
            self._config.subagents.concurrency_acquire_timeout_seconds
        )
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=acquire_timeout,
            )
        except asyncio.TimeoutError as exc:
            self._audit(
                "audit.subagent.fail",
                parent_conv=parent_conv_key,
                level="WARNING",
                slug=agent_slug,
                kind="concurrency_timeout",
                acquire_timeout_seconds=acquire_timeout,
            )
            raise SubAgentError(
                "concurrency_timeout",
                reason=f"waited {acquire_timeout}s for a semaphore slot",
            ) from exc

        # Past this point every exit path MUST release the semaphore.
        audit_id = str(uuid.uuid4())
        conv_key = f"subagent:{agent_slug}:{audit_id}"
        try:
            # Step 2: load persona. Missing persona is a spawn-time fail.
            try:
                persona_doc = self._load_persona(agent_slug)
            except SubAgentError as exc:
                # _load_persona only raises kind="not_found"; surface a
                # matching fail audit and re-raise.
                self._audit(
                    "audit.subagent.fail",
                    parent_conv=parent_conv_key,
                    level="WARNING",
                    slug=agent_slug,
                    audit_id=audit_id,
                    kind=exc.kind,
                )
                raise

            # Persona body feeds the prompt; grants flow through the
            # resolver below into both the prompt framing and the spawned
            # options so the sub-agent's reach matches its effective grant.
            persona = persona_doc.body
            persona_grants = persona_doc.grants

            # Resolve the effective grant for this slug across the three
            # layers (fallback → anna.yaml agents.<slug> → frontmatter).
            # Local import keeps the SDK-free unit tests that exercise
            # _load_persona / _build_system_prompt from pulling grants in.
            from anna.runtime.grants import resolve_effective_grant

            resolved = resolve_effective_grant(
                self._config, agent_slug, persona_grants
            )

            # Step 3: load skills (missing dir → []).
            skills = self._load_skills(agent_slug)

            # Step 4: assemble system prompt. The prompt names the RESOLVED
            # write dirs + server tool surface so the framing matches the
            # actual grant (subtask 8). With no grant the resolver returns
            # the fallback and the prompt stays byte-identical to before.
            vault_root = self._config.vault.resolved_path
            system_prompt = self._build_system_prompt(
                persona=persona,
                skills=skills,
                task=task,
                context=context,
                vault_root=vault_root,
                extra_dirs=list(resolved.write_dirs),
                server_tools=self._summarize_server_tools(resolved),
            )

            # Step 5: build options off the resolved grant.
            options = self._build_subagent_options(
                system_prompt=system_prompt,
                conv_key=conv_key,
                resolved=resolved,
            )

            # Step 6+7: stamp spawn audit. Truncate the task for the
            # audit line so a multi-page prompt does not bloat journald.
            truncated_task = task[:500]
            # Resolved model string, shared between the spawn audit event
            # and the outbound transcript trailer so both record the same
            # value. None = inherited CLI default.
            resolved_model = resolved.model or "<cli-default>"
            self._audit(
                "audit.subagent.spawn",
                parent_conv=parent_conv_key,
                slug=agent_slug,
                audit_id=audit_id,
                task=truncated_task,
                timeout_seconds=effective_timeout,
                # Record the resolved model so the operator can verify which
                # model a delegation ran on.
                model=resolved_model,
            )

            # Write the task transcript line up front so a crashing
            # sub-agent still leaves evidence of what was asked.
            transcript_path = self._write_transcript_line(
                slug=agent_slug,
                conv_key=conv_key,
                direction="task",
                text=task,
                audit_id=audit_id,
                parent_conv=parent_conv_key,
            )

            # Step 8-10: open the SDK client and run the task, wrapped
            # in asyncio.wait_for so a runaway sub-agent does not hold
            # the semaphore past timeout_seconds. Lazy import mirrors
            # ConversationWorker._handle so unit tests can monkeypatch
            # ClaudeSDKClient + message types.
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeSDKClient,
                ResultMessage,
                TextBlock,
                ToolUseBlock,
            )

            start_ns = time.monotonic_ns()
            reply_chunks: list[str] = []
            tool_calls: list[str] = []
            cost_usd: float = 0.0

            client = ClaudeSDKClient(options=options)

            async def _drive() -> None:
                """Open the client, send the task, drain the response.

                Inner coroutine so asyncio.wait_for can cancel the
                whole open/query/receive chain on timeout. The finally
                block guarantees __aexit__ runs whether the body
                completes, raises, or is cancelled.
                """
                await client.__aenter__()
                try:
                    await client.query(task)
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    reply_chunks.append(block.text)
                                elif isinstance(block, ToolUseBlock):
                                    tool_calls.append(block.name)
                        if isinstance(msg, ResultMessage):
                            nonlocal cost_usd
                            if msg.total_cost_usd is not None:
                                cost_usd = float(msg.total_cost_usd)
                            break
                finally:
                    try:
                        await client.__aexit__(None, None, None)
                    except Exception as close_exc:  # noqa: BLE001
                        # Closing the client must not mask the outer
                        # exception (or a successful run).
                        self._log.warning(
                            "subagent.client_close_failed",
                            slug=agent_slug,
                            audit_id=audit_id,
                            error=str(close_exc),
                        )

            try:
                await asyncio.wait_for(_drive(), timeout=effective_timeout)
            except asyncio.TimeoutError as exc:
                duration_ms = int(
                    (time.monotonic_ns() - start_ns) / 1_000_000
                )
                self._write_transcript_line(
                    slug=agent_slug,
                    conv_key=conv_key,
                    direction="fail",
                    text=f"timeout after {effective_timeout}s",
                    audit_id=audit_id,
                    parent_conv=parent_conv_key,
                    kind="timeout",
                    duration_seconds=duration_ms / 1000.0,
                )
                self._audit(
                    "audit.subagent.fail",
                    parent_conv=parent_conv_key,
                    level="WARNING",
                    slug=agent_slug,
                    audit_id=audit_id,
                    kind="timeout",
                    duration_seconds=duration_ms / 1000.0,
                    timeout_seconds=effective_timeout,
                )
                raise SubAgentError(
                    "timeout",
                    reason=f"exceeded {effective_timeout}s",
                ) from exc
            except SubAgentError:
                # Sub-agent errors raised inside _drive (none today, but
                # future hook points) propagate without remapping.
                raise
            except Exception as exc:  # noqa: BLE001
                duration_ms = int(
                    (time.monotonic_ns() - start_ns) / 1_000_000
                )
                self._write_transcript_line(
                    slug=agent_slug,
                    conv_key=conv_key,
                    direction="fail",
                    text=repr(exc),
                    audit_id=audit_id,
                    parent_conv=parent_conv_key,
                    kind="error",
                    duration_seconds=duration_ms / 1000.0,
                )
                self._audit(
                    "audit.subagent.fail",
                    parent_conv=parent_conv_key,
                    level="WARNING",
                    slug=agent_slug,
                    audit_id=audit_id,
                    kind="error",
                    reason=repr(exc),
                    duration_seconds=duration_ms / 1000.0,
                )
                raise SubAgentError("error", reason=repr(exc)) from exc

            duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
            reply_text = "\n".join(c for c in reply_chunks if c).strip()
            if not reply_text:
                reply_text = "(no response)"

            # Step 11: write the outbound transcript line.
            self._write_transcript_line(
                slug=agent_slug,
                conv_key=conv_key,
                direction="outbound",
                text=reply_text,
                audit_id=audit_id,
                parent_conv=parent_conv_key,
                duration_seconds=duration_ms / 1000.0,
                cost_usd=cost_usd,
                tool_calls=tool_calls,
                # Same resolved model string the spawn audit event records,
                # so trailer consumers can bucket runs per model without
                # joining back to the audit log.
                model=resolved_model,
            )

            # Step 12: completion audit.
            self._audit(
                "audit.subagent.complete",
                parent_conv=parent_conv_key,
                slug=agent_slug,
                audit_id=audit_id,
                duration_seconds=duration_ms / 1000.0,
                output_length=len(reply_text),
                cost_usd=cost_usd,
            )

            return DelegateResult(
                text=reply_text,
                transcript_path=transcript_path,
                tool_calls=tool_calls,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                status="ok",
            )
        finally:
            # Step 13: always release the semaphore.
            self._semaphore.release()


__all__ = [
    "BackgroundDeliveryCallback",
    "DelegateResult",
    "SubAgentError",
    "SubAgentRunner",
]
