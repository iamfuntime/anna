"""MCP tools for explicit, transactionally durable fact memory."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from anna.runtime.memory import MemoryStore


class MemoryTools:
    def __init__(self, store: MemoryStore, *, owner_id: str, scope: str, source_event_id: Callable[[], str | None]) -> None:
        self.store = store
        self.owner_id = owner_id
        self.scope = scope
        self._source_event_id = source_event_id

    async def remember(self, *, kind: str, subject: str, content: str) -> dict[str, Any]:
        # remember() returns only after SQLite's transaction has committed.
        fact = self.store.remember(owner_id=self.owner_id, visibility_scope=self.scope, kind=kind,
                                  subject=subject, content=content, source_event_id=self._source_event_id())
        return {"content": [{"type": "text", "text": f"Remembered fact #{fact.id}: {fact.subject}."}]}

    async def search(self, *, query: str) -> dict[str, Any]:
        facts = self.store.search(owner_id=self.owner_id, visibility_scopes={self.scope}, query=query)
        text = "\n".join(f"#{fact.id} {fact.subject}: {fact.content}" for fact in facts) or "(no matching memories)"
        return {"content": [{"type": "text", "text": text}]}

    async def correct(self, *, fact_id: int, content: str) -> dict[str, Any]:
        prior = self.store.get(fact_id)
        if prior.owner_id != self.owner_id or prior.visibility_scope != self.scope:
            return {"content": [{"type": "text", "text": "error: memory is outside this conversation scope"}]}
        fact = self.store.correct(fact_id, content=content, source_event_id=self._source_event_id())
        return {"content": [{"type": "text", "text": f"Corrected memory #{fact_id} with fact #{fact.id}."}]}

    async def forget(self, *, fact_id: int) -> dict[str, Any]:
        prior = self.store.get(fact_id)
        if prior.owner_id != self.owner_id or prior.visibility_scope != self.scope:
            return {"content": [{"type": "text", "text": "error: memory is outside this conversation scope"}]}
        self.store.forget(fact_id)
        return {"content": [{"type": "text", "text": f"Forgot memory #{fact_id}; transcript/audit history is retained."}]}


def build_memory_server(*, tools: MemoryTools) -> Any:
    @tool("memory_remember", "Persist an explicit preference, decision, or fact before saying it is remembered.", {"kind": str, "subject": str, "content": str})
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.remember(kind=args["kind"], subject=args["subject"], content=args["content"])

    @tool("memory_search", "Search facts visible to this owner and conversation scope.", {"query": str})
    async def search(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.search(query=args["query"])

    @tool("memory_correct", "Supersede a visible fact with corrected content.", {"fact_id": int, "content": str})
    async def correct(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.correct(fact_id=int(args["fact_id"]), content=args["content"])

    @tool("memory_forget", "Remove a visible fact from retrieval; source transcripts remain retained.", {"fact_id": int})
    async def forget(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.forget(fact_id=int(args["fact_id"]))

    return create_sdk_mcp_server(name="anna_memory", version="1.0.0", tools=[remember, search, correct, forget])


MEMORY_TOOL_NAMES = ("memory_remember", "memory_search", "memory_correct", "memory_forget")
