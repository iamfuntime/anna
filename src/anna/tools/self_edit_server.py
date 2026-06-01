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
from anna.runtime.nl_cron import parse_natural_language
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.schedule_types import Schedule, ScheduleDestination
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
    "schedule_create",
    "schedule_update",
    "schedule_delete",
    "schedule_list",
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
        schedule_store: ScheduleStore | None = None,
    ) -> None:
        self.config = config
        self.supervisor = supervisor
        self.agents_registry = agents_registry
        self.skills_registry = skills_registry
        self.schedule_store = schedule_store
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

    # ------------------------------------------------------------------
    # Scheduler (Phase 2)
    # ------------------------------------------------------------------

    def _require_store(self) -> ScheduleStore:
        if self.schedule_store is None:
            raise RuntimeError(
                "scheduler is not configured for this process; "
                "set scheduler.enabled: true in anna.yaml and restart"
            )
        return self.schedule_store

    async def schedule_create(
        self,
        *,
        id: str,
        prompt: str,
        destination_transport: str,
        destination_channel: str,
        cron: str | None = None,
        natural_language: str | None = None,
        tz_name: str = "America/New_York",
        timeout_seconds: int = 300,
        enabled: bool = True,
        creator_conv: str,
    ) -> dict[str, Any]:
        store = self._require_store()
        if (cron is None) == (natural_language is None):
            raise ValueError(
                "schedule_create requires exactly one of cron or natural_language"
            )
        if natural_language is not None:
            cron_expr = parse_natural_language(natural_language)
        else:
            cron_expr = cron  # type: ignore[assignment]
        if destination_transport not in ("slack", "telegram"):
            raise ValueError(
                f"destination_transport must be 'slack' or 'telegram', got {destination_transport!r}"
            )
        schedule = Schedule(
            id=id,
            natural_language=natural_language,
            cron=cron_expr,  # type: ignore[arg-type]
            timezone=tz_name,
            prompt=prompt,
            destination=ScheduleDestination(transport=destination_transport, channel=destination_channel),  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
            enabled=enabled,
            created_at=datetime.now(timezone.utc),
        )
        await store.create(schedule, actor_conv=creator_conv)
        return _text_response(
            f"schedule '{id}' created with cron {cron_expr!r} "
            f"posting to {destination_transport}:{destination_channel}"
        )

    async def schedule_update(
        self,
        *,
        id: str,
        creator_conv: str,
        cron: str | None = None,
        natural_language: str | None = None,
        tz_name: str | None = None,
        prompt: str | None = None,
        destination_transport: str | None = None,
        destination_channel: str | None = None,
        timeout_seconds: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        store = self._require_store()
        changes: dict[str, Any] = {}
        if cron is not None and natural_language is not None:
            raise ValueError(
                "schedule_update accepts cron OR natural_language, not both"
            )
        if natural_language is not None:
            changes["cron"] = parse_natural_language(natural_language)
            changes["natural_language"] = natural_language
        elif cron is not None:
            changes["cron"] = cron
            changes["natural_language"] = None
        if tz_name is not None:
            changes["timezone"] = tz_name
        if prompt is not None:
            changes["prompt"] = prompt
        if destination_transport is not None or destination_channel is not None:
            existing = store.get(id)
            if existing is None:
                raise ValueError(f"schedule '{id}' does not exist")
            new_transport = destination_transport or existing.destination.transport
            new_channel = destination_channel or existing.destination.channel
            changes["destination"] = ScheduleDestination(  # type: ignore[arg-type]
                transport=new_transport,
                channel=new_channel,
            ).model_dump()
        if timeout_seconds is not None:
            changes["timeout_seconds"] = timeout_seconds
        if enabled is not None:
            changes["enabled"] = enabled

        if not changes:
            raise ValueError("schedule_update called with no fields to change")
        updated = await store.update(id, actor_conv=creator_conv, **changes)
        return _text_response(
            f"schedule '{id}' updated; new cron: {updated.cron!r}, "
            f"enabled: {updated.enabled}"
        )

    async def schedule_delete(
        self,
        *,
        id: str,
        creator_conv: str,
    ) -> dict[str, Any]:
        store = self._require_store()
        await store.delete(id, actor_conv=creator_conv)
        return _text_response(f"schedule '{id}' deleted")

    async def schedule_list(self) -> dict[str, Any]:
        store = self._require_store()
        schedules = store.list()
        if not schedules:
            return _text_response("(no schedules configured)")
        lines: list[str] = []
        for s in schedules:
            state = s.state
            last = state.last_fired_at.isoformat() if state.last_fired_at else "never"
            enabled_marker = "ENABLED " if s.enabled else "disabled"
            lines.append(
                f"- [{enabled_marker}] {s.id}: cron={s.cron!r} "
                f"-> {s.destination.transport}:{s.destination.channel} "
                f"(last fired: {last}, consecutive failures: {state.consecutive_failures})"
            )
        return _text_response("\n".join(lines))


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

    @tool(
        "schedule_create",
        "Create a new scheduled task. Either cron (5-field cron expression) or natural_language ('every morning at 6am', 'every weekday at 9am', 'every Monday at 10am', 'every N hours', 'every N minutes', 'daily at HH:MM', 'weekly on DAY at HH:MM') must be set, not both. destination_transport is 'slack' or 'telegram'; destination_channel is the channel id or chat id. The admin channel is reserved for alerter traffic and will be rejected.",
        {
            "id": str,
            "prompt": str,
            "destination_transport": str,
            "destination_channel": str,
            "cron": str,
            "natural_language": str,
            "tz_name": str,
            "timeout_seconds": int,
            "enabled": bool,
        },
    )
    async def _schedule_create(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.schedule_create(
            id=args["id"],
            prompt=args["prompt"],
            destination_transport=args["destination_transport"],
            destination_channel=args["destination_channel"],
            cron=args.get("cron") or None,
            natural_language=args.get("natural_language") or None,
            tz_name=args.get("tz_name") or "America/New_York",
            timeout_seconds=int(args.get("timeout_seconds") or 300),
            enabled=bool(args.get("enabled", True)),
            creator_conv=conv_key,
        )

    @tool(
        "schedule_update",
        "Update an existing schedule by id. Any field other than id may be passed; only set the fields you want to change. cron and natural_language are mutually exclusive.",
        {
            "id": str,
            "cron": str,
            "natural_language": str,
            "tz_name": str,
            "prompt": str,
            "destination_transport": str,
            "destination_channel": str,
            "timeout_seconds": int,
            "enabled": bool,
        },
    )
    async def _schedule_update(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.schedule_update(
            id=args["id"],
            creator_conv=conv_key,
            cron=args.get("cron") or None,
            natural_language=args.get("natural_language") or None,
            tz_name=args.get("tz_name") or None,
            prompt=args.get("prompt") or None,
            destination_transport=args.get("destination_transport") or None,
            destination_channel=args.get("destination_channel") or None,
            timeout_seconds=int(args["timeout_seconds"]) if args.get("timeout_seconds") is not None else None,
            enabled=args.get("enabled") if "enabled" in args else None,
        )

    @tool(
        "schedule_delete",
        "Delete a schedule by id.",
        {"id": str},
    )
    async def _schedule_delete(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.schedule_delete(
            id=args["id"],
            creator_conv=conv_key,
        )

    @tool(
        "schedule_list",
        "List all configured schedules with their cron, destination, and last-fire state.",
        {},
    )
    async def _schedule_list(_args: dict[str, Any]) -> dict[str, Any]:
        return await tools.schedule_list()

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
            _schedule_create,
            _schedule_update,
            _schedule_delete,
            _schedule_list,
        ],
    )
