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

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger

if TYPE_CHECKING:
    from anna.agents.registry import SubAgentRegistry
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

    def _load_persona(self, slug: str) -> str:
        """Read ``$ANNA_HOME/agents/<slug>.md`` off disk and return the body.

        Reads on every call — no caching — so the operator can edit a
        persona file without restarting ANNA. The registry is *not*
        consulted because :class:`SubAgentRegistry` exposes only
        ``list_personas()`` and a single-slug lookup would either need
        a linear scan or a new ``get(slug)`` method; the runner skips
        both by reading the well-known path directly.

        Returns:
            Persona text, possibly empty if the file exists but is blank.

        Raises:
            :class:`SubAgentError`: when the persona file does not
                exist. The error message is the literal string
                ``"not_found"`` to match the status field on a
                ``DelegateResult`` of the same shape.
        """
        path = self._config.anna_home / "agents" / f"{slug}.md"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SubAgentError("not_found") from exc

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

    @staticmethod
    def _build_system_prompt(
        persona: str,
        skills: list[str],
        task: str,
        context: dict[str, Any] | None,
        vault_root: Path,
        extra_dirs: list[str] | None = None,
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
        delegation_block = (
            "You are running as a one-shot sub-agent spawned by ANNA. You "
            "do not have the delegate tool; you cannot spawn further "
            "sub-agents. You have web_search, web_fetch, and "
            "vault_download for outside-the-vault work. "
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

    # ------------------------------------------------------------------
    # Sub-agent ClaudeAgentOptions builder (subtask 5)
    # ------------------------------------------------------------------

    def _build_subagent_options(
        self,
        system_prompt: str,
        conv_key: str,
        permission_mode_override: str | None = None,
    ) -> Any:
        """Construct the ``ClaudeAgentOptions`` used to spawn the sub-agent client.

        This is also where the depth-protection invariant is enforced
        at the runtime level: ``anna_self_edit``, ``anna_google``, and
        ``anna_delegate`` are *never* mounted on a sub-agent's options.
        The only MCP server a sub-agent ever sees is ``anna_web`` (and
        only when ``config.tools.enabled`` is true).

        Args:
            system_prompt: Output of :meth:`_build_system_prompt`.
            conv_key: Synthetic conv_key for this delegation; flows
                into the ``anna_web`` server closure so the tool calls
                that fire from inside the sub-agent get audit-stamped
                with a distinct identifier.
            permission_mode_override: Optional per-call permission
                mode. Default is ``acceptEdits`` (stricter than the
                worker's ``bypassPermissions``); pass to tighten or
                loosen on a per-delegation basis.

        Returns:
            ``ClaudeAgentOptions`` ready to feed into ``ClaudeSDKClient``.
        """
        # Lazy import so the unit tests in this module do not pull in
        # the SDK transitively. Mirrors ``ConversationWorker._build_options``.
        from claude_agent_sdk import ClaudeAgentOptions

        from anna.tools.vault_tools import VaultTools
        from anna.tools.web_server import build_web_server
        from anna.tools.web_tools import WebTools

        vault_root = self._config.vault.resolved_path

        mcp_servers: dict[str, Any] = {}
        if self._config.tools.enabled:
            web_tools = WebTools(config=self._config)
            vault_tools = VaultTools(config=self._config)
            web_server = build_web_server(
                config=self._config,
                web_tools=web_tools,
                vault_tools=vault_tools,
                conv_key=conv_key,
            )
            if web_server is not None:
                mcp_servers["anna_web"] = web_server

        permission_mode = permission_mode_override or "acceptEdits"

        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            # No setting_sources for sub-agents — they live entirely off
            # their persona file plus skills. No host Claude Code env
            # leaks in.
            setting_sources=[],
            permission_mode=permission_mode,
            # Only anna_web (when tools enabled). Never anna_self_edit,
            # anna_google, or anna_delegate. This is the runtime-level
            # enforcement of one-level-only delegation.
            mcp_servers=mcp_servers,
            allowed_tools=list(self._config.subagents.allowed_tools),
            cwd=str(vault_root),
            # add_dirs grants extra reachable roots beyond the cwd
            # (ANNA vault). Driven by ``subagents.extra_dirs`` config —
            # typically the collaborative Brain vault so sub-agents can
            # read detection templates / query libraries / example
            # reports and write reports into Brain/Inbox directly. Still
            # never mounts core/: ANNA's identity files stay invisible
            # because core/ is not under any configured extra_dir.
            add_dirs=[
                str(Path(d).expanduser())
                for d in self._config.subagents.extra_dirs
            ],
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
                persona = self._load_persona(agent_slug)
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

            # Step 3: load skills (missing dir → []).
            skills = self._load_skills(agent_slug)

            # Step 4: assemble system prompt.
            vault_root = self._config.vault.resolved_path
            system_prompt = self._build_system_prompt(
                persona=persona,
                skills=skills,
                task=task,
                context=context,
                vault_root=vault_root,
                extra_dirs=list(self._config.subagents.extra_dirs),
            )

            # Step 5: build options.
            options = self._build_subagent_options(
                system_prompt=system_prompt,
                conv_key=conv_key,
            )

            # Step 6+7: stamp spawn audit. Truncate the task for the
            # audit line so a multi-page prompt does not bloat journald.
            truncated_task = task[:500]
            self._audit(
                "audit.subagent.spawn",
                parent_conv=parent_conv_key,
                slug=agent_slug,
                audit_id=audit_id,
                task=truncated_task,
                timeout_seconds=effective_timeout,
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
            )

            # Step 12: completion audit.
            self._audit(
                "audit.subagent.complete",
                parent_conv=parent_conv_key,
                slug=agent_slug,
                audit_id=audit_id,
                duration_seconds=duration_ms / 1000.0,
                output_length=len(reply_text),
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
