"""Self-edit MCP server.

Per v3 §6 (carry-forward from v1 §8), ANNA mutates her own identity files,
sub-agent personas, skill files, and the AGENTS.md/MEMORY.md core entries
through a tightly-scoped MCP server that runs in-process. The SDK pattern is
``claude_agent_sdk.create_sdk_mcp_server`` plus the ``@tool`` decorator.

Every tool here:

* Goes through ``Supervisor.acquire(key)`` for any write that touches
  ``core/<file>.md``, ``agents/<slug>.md``, or ``skills/<agent>/<slug>.md``.
* Emits an audit event so the operator can reconstruct what ANNA did.
* Returns a structured MCP response (``content`` list of text blocks).

The server is instantiated per-worker by :class:`ConversationWorker`. The
worker passes in its conversation_key as ``conv_key`` context so the tools
can stamp audit events with the right caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from anna.agents.registry import SubAgentRegistry
from anna.config import AnnaConfig
from anna.core.identity import CORE_FILES, CoreFile
from anna.log import audit_event, get_logger
from anna.runtime.supervisor import Supervisor
from anna.skills.registry import SkillRegistry, SkillTrigger
from anna.vault.checkpoint import list_recent_checkpoints


SELF_EDIT_TOOL_NAMES: tuple[str, ...] = (
    "subagent_create",
    "subagent_edit",
    "skill_create",
    "skill_edit",
    "agents_md_append_row",
    "memory_md_append",
    "checkpoint_read_recent",
)


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SelfEditTools:
    """Bundles registry and supervisor handles for one worker's tool surface.

    The MCP tool factories in :func:`build_self_edit_server` close over an
    instance of this class so each tool call sees the same supervisor and
    registries.
    """

    def __init__(
        self,
        *,
        config: AnnaConfig,
        supervisor: Supervisor,
        agents_registry: SubAgentRegistry,
        skills_registry: SkillRegistry,
    ) -> None:
        self.config = config
        self.supervisor = supervisor
        self.agents_registry = agents_registry
        self.skills_registry = skills_registry
        self._log = get_logger("anna.tools.self_edit")

    # ------------------------------------------------------------------
    # Sub-agent personas
    # ------------------------------------------------------------------

    async def subagent_create(
        self,
        *,
        slug: str,
        persona_text: str,
        creator_conv: str,
    ) -> dict[str, Any]:
        spec = await self.agents_registry.create_or_replace(
            slug=slug,
            persona_text=persona_text,
            creator_conv=creator_conv,
        )
        return _text_response(
            f"sub-agent {slug} persona written to {spec.persona_path} "
            f"({spec.tokens} tokens)"
        )

    async def subagent_edit(
        self,
        *,
        slug: str,
        persona_text: str,
        creator_conv: str,
        edit_reason: str,
    ) -> dict[str, Any]:
        spec = await self.agents_registry.create_or_replace(
            slug=slug,
            persona_text=persona_text,
            creator_conv=creator_conv,
        )
        # The registry distinguishes create vs edit by checking file existence,
        # but it does not carry an edit_reason field. Mirror the reason into a
        # second audit event so the operator can see the why.
        audit_event(
            "audit.subagent.edit_reason",
            audit_dir=self.config.audit_dir,
            actor="anna",
            conv_key=creator_conv,
            fsync_on_write=self.config.logging.audit.fsync_on_write,
            slug=slug,
            edit_reason=edit_reason,
        )
        return _text_response(
            f"sub-agent {slug} persona updated ({spec.tokens} tokens) — reason: {edit_reason}"
        )

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    async def skill_create(
        self,
        *,
        agent: str,
        slug: str,
        skill_text: str,
        creator_conv: str,
        trigger: str,
    ) -> dict[str, Any]:
        # Validate the trigger string matches the registry's Literal contract.
        if trigger not in ("third_iteration_threshold", "operator_request", "manual_paste"):
            raise ValueError(
                f"unknown skill trigger {trigger!r}; "
                f"expected third_iteration_threshold, operator_request, or manual_paste"
            )
        cast_trigger: SkillTrigger = trigger  # type: ignore[assignment]
        spec = await self.skills_registry.create_or_replace(
            agent=agent,
            slug=slug,
            skill_text=skill_text,
            creator_conv=creator_conv,
            trigger=cast_trigger,
        )
        return _text_response(
            f"skill {agent}/{slug} written to {spec.skill_path} "
            f"({spec.tokens} tokens, trigger={trigger})"
        )

    async def skill_edit(
        self,
        *,
        agent: str,
        slug: str,
        skill_text: str,
        creator_conv: str,
        iteration_notes_appended: str,
    ) -> dict[str, Any]:
        spec = await self.skills_registry.create_or_replace(
            agent=agent,
            slug=slug,
            skill_text=skill_text,
            creator_conv=creator_conv,
            trigger="operator_request",
            iteration_notes_appended=iteration_notes_appended,
        )
        return _text_response(
            f"skill {agent}/{slug} updated ({spec.tokens} tokens) — notes: {iteration_notes_appended}"
        )

    # ------------------------------------------------------------------
    # AGENTS.md row append / replace
    # ------------------------------------------------------------------

    async def agents_md_append_row(
        self,
        *,
        slug: str,
        description: str,
        when_to_invoke: str,
        conv_key: str,
    ) -> dict[str, Any]:
        """Append (or in-place replace) a roster row in core/AGENTS.md.

        The lock key is ``core/AGENTS.md`` — same as the supervisor uses for
        full-file writes. Holding the lock for the read-modify-write window
        prevents the supervisor's write_core_file from racing with us.
        """
        lock = await self.supervisor.acquire("core/AGENTS.md")
        async with lock:
            core_dir = self.config.core_dir
            core_dir.mkdir(parents=True, exist_ok=True)
            path = core_dir / "AGENTS.md"
            existing = path.read_text(encoding="utf-8") if path.exists() else ""

            row = f"- **{slug}** — {description} (invoke when: {when_to_invoke})"
            new_text, replaced = self._splice_row(existing, slug, row)
            path.write_text(new_text, encoding="utf-8")

        audit_event(
            "audit.agents_md.row_written",
            audit_dir=self.config.audit_dir,
            actor="anna",
            conv_key=conv_key,
            fsync_on_write=self.config.logging.audit.fsync_on_write,
            slug=slug,
            replaced_existing=replaced,
        )
        verb = "replaced" if replaced else "appended"
        return _text_response(f"AGENTS.md row for {slug} {verb}")

    @staticmethod
    def _splice_row(existing: str, slug: str, new_row: str) -> tuple[str, bool]:
        """Replace a row with the matching slug, otherwise append.

        Detection: the slug always appears in the canonical ``- **{slug}** —``
        prefix that this server emits. Any existing line that starts with
        that prefix is the row to replace.
        """
        marker = f"- **{slug}** —"
        lines = existing.splitlines()
        replaced = False
        out: list[str] = []
        for line in lines:
            if line.startswith(marker):
                out.append(new_row)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            # Make sure we land on a fresh line.
            if existing and not existing.endswith("\n"):
                out.append("")
            out.append(new_row)
        # Preserve a trailing newline.
        return ("\n".join(out) + "\n"), replaced

    # ------------------------------------------------------------------
    # MEMORY.md dated append
    # ------------------------------------------------------------------

    async def memory_md_append(
        self,
        *,
        entry: str,
        conv_key: str,
    ) -> dict[str, Any]:
        lock = await self.supervisor.acquire("core/MEMORY.md")
        async with lock:
            core_dir = self.config.core_dir
            core_dir.mkdir(parents=True, exist_ok=True)
            path = core_dir / "MEMORY.md"
            existing = path.read_text(encoding="utf-8") if path.exists() else ""

            today = _today()
            heading = f"## {today}"
            if heading in existing:
                new_text = existing
                if not new_text.endswith("\n"):
                    new_text += "\n"
                new_text += f"- {entry}\n"
            else:
                prefix = existing
                if prefix and not prefix.endswith("\n"):
                    prefix += "\n"
                if prefix and not prefix.endswith("\n\n"):
                    prefix += "\n"
                new_text = f"{prefix}{heading}\n- {entry}\n"

            path.write_text(new_text, encoding="utf-8")

        audit_event(
            "audit.memory_md.appended",
            audit_dir=self.config.audit_dir,
            actor="anna",
            conv_key=conv_key,
            fsync_on_write=self.config.logging.audit.fsync_on_write,
            entry_len=len(entry),
        )
        return _text_response(f"MEMORY.md entry appended under {_today()}")

    # ------------------------------------------------------------------
    # Checkpoint reader
    # ------------------------------------------------------------------

    async def checkpoint_read_recent(
        self,
        *,
        conv_key: str,
        limit: int = 2,
    ) -> dict[str, Any]:
        paths = list_recent_checkpoints(
            vault_root=self.config.vault.resolved_path,
            conversation_key=conv_key,
            limit=limit,
        )
        if not paths:
            return _text_response("(no checkpoints found for this conversation)")
        chunks: list[str] = []
        # list_recent_checkpoints returns newest first; show oldest first so
        # the resume context reads chronologically.
        for path in reversed(paths):
            chunks.append(f"### {path.name}\n{path.read_text(encoding='utf-8')}")
        return _text_response("\n\n---\n\n".join(chunks))


def build_self_edit_server(*, tools: SelfEditTools, conv_key: str) -> Any:
    """Construct the per-worker MCP server.

    The ``conv_key`` is captured in the tool closures so every audit event
    fired by these tools is stamped with the right caller without the SDK
    having to pass it explicitly.
    """

    @tool(
        "subagent_create",
        "Create a new sub-agent persona file at agents/<slug>.md. Use when the operator asks you to spin up a new specialized agent.",
        {
            "slug": str,
            "persona_text": str,
        },
    )
    async def _subagent_create(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.subagent_create(
            slug=args["slug"],
            persona_text=args["persona_text"],
            creator_conv=conv_key,
        )

    @tool(
        "subagent_edit",
        "Edit (rewrite in place) an existing sub-agent persona file. Provide a short edit_reason for the audit log.",
        {
            "slug": str,
            "persona_text": str,
            "edit_reason": str,
        },
    )
    async def _subagent_edit(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.subagent_edit(
            slug=args["slug"],
            persona_text=args["persona_text"],
            creator_conv=conv_key,
            edit_reason=args["edit_reason"],
        )

    @tool(
        "skill_create",
        "Create a new skill file at skills/<agent>/<slug>.md. trigger is one of: third_iteration_threshold, operator_request, manual_paste.",
        {
            "agent": str,
            "slug": str,
            "skill_text": str,
            "trigger": str,
        },
    )
    async def _skill_create(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.skill_create(
            agent=args["agent"],
            slug=args["slug"],
            skill_text=args["skill_text"],
            creator_conv=conv_key,
            trigger=args["trigger"],
        )

    @tool(
        "skill_edit",
        "Edit an existing skill file. iteration_notes_appended explains what changed for the audit trail.",
        {
            "agent": str,
            "slug": str,
            "skill_text": str,
            "iteration_notes_appended": str,
        },
    )
    async def _skill_edit(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.skill_edit(
            agent=args["agent"],
            slug=args["slug"],
            skill_text=args["skill_text"],
            creator_conv=conv_key,
            iteration_notes_appended=args["iteration_notes_appended"],
        )

    @tool(
        "agents_md_append_row",
        "Add (or in-place replace) a row in core/AGENTS.md describing a sub-agent. Pair this with subagent_create.",
        {
            "slug": str,
            "description": str,
            "when_to_invoke": str,
        },
    )
    async def _agents_md_append_row(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.agents_md_append_row(
            slug=args["slug"],
            description=args["description"],
            when_to_invoke=args["when_to_invoke"],
            conv_key=conv_key,
        )

    @tool(
        "memory_md_append",
        "Append a dated entry to core/MEMORY.md. The entry is prefixed with today's date heading when needed.",
        {
            "entry": str,
        },
    )
    async def _memory_md_append(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.memory_md_append(
            entry=args["entry"],
            conv_key=conv_key,
        )

    @tool(
        "checkpoint_read_recent",
        "Read the N most recent checkpoint files for a conversation key. Returns their contents, oldest first.",
        {
            "conv_key": str,
            "limit": int,
        },
    )
    async def _checkpoint_read_recent(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.checkpoint_read_recent(
            conv_key=args["conv_key"],
            limit=int(args.get("limit", 2)),
        )

    return create_sdk_mcp_server(
        name="anna_self_edit",
        version="1.0.0",
        tools=[
            _subagent_create,
            _subagent_edit,
            _skill_create,
            _skill_edit,
            _agents_md_append_row,
            _memory_md_append,
            _checkpoint_read_recent,
        ],
    )
