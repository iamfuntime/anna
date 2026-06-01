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
from dataclasses import dataclass
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
