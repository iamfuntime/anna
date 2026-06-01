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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anna.config import AnnaConfig
from anna.log import get_logger

if TYPE_CHECKING:
    from anna.agents.registry import SubAgentRegistry
    from anna.runtime.supervisor import Supervisor
    from anna.skills.registry import SkillRegistry


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
    """Raised by :meth:`SubAgentRunner.delegate` for early-fail cases.

    Covers the conditions a delegation cannot even be attempted under:

    * Missing persona file at ``$ANNA_HOME/agents/<slug>.md``.
    * Caller is itself a sub-agent (depth > 1) — enforced at the
      runtime layer because the ``anna_delegate`` server is never
      mounted on a sub-agent's options, but a defensive raise covers
      the case where a future change accidentally mounts it.
    * Concurrency semaphore could not be acquired within
      ``config.subagents.concurrency_acquire_timeout_seconds``.
    * Per-delegation timeout elapsed.
    * The SDK refused to spawn (auth, rate-limit, network).

    Caught and rendered as the tool's text response in
    ``build_delegate_server``; never propagates out of the worker.
    """


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

        delegation_block = (
            "You are running as a one-shot sub-agent spawned by ANNA. You "
            "do not have the delegate tool; you cannot spawn further "
            "sub-agents. You have web_search, web_fetch, and "
            "vault_download for outside-the-vault work, plus the file "
            "ops (Read, Write, Edit, Glob, Grep) scoped to the ANNA "
            "vault. Your reply is returned to the parent agent as a "
            "single tool result; write the final answer as one message.\n"
            f"Vault root: {vault_root}."
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
            # Empty add_dirs — sub-agents do not see core/. Persona +
            # skills are the entire identity surface; ANNA's identity
            # files stay invisible.
            add_dirs=[],
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

        Stubbed until subtask 6 lands. Method signature is locked here
        so the MCP wrapper and worker wiring (later subtasks) can be
        written against a stable contract.

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
                sub-agent's system prompt. None when the caller has
                nothing structured to pass.
            timeout_seconds: Per-call wall-clock cap. None falls back
                to ``config.subagents.default_timeout_seconds``.

        Returns:
            :class:`DelegateResult` on success.

        Raises:
            :class:`SubAgentError`: for missing persona, depth
                violation, concurrency-acquire timeout, or SDK-level
                failures before a response could be assembled.
            :class:`NotImplementedError`: until subtask 6 ships.
        """
        raise NotImplementedError(
            "SubAgentRunner.delegate lands in subtask 6 of the §3 plan"
        )


__all__ = [
    "DelegateResult",
    "SubAgentError",
    "SubAgentRunner",
]
