"""Sub-agent persona file registry.

Persona files live at ``$ANNA_HOME/agents/<slug>.md``. The registry knows how
to enumerate them, read them, and create new ones under the supervisor lock.
Every creation and edit emits an audit event.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from anna.log import audit_event, get_logger
from anna.runtime.supervisor import Supervisor


@dataclass(frozen=True)
class SubAgentSpec:
    slug: str
    persona_path: Path
    tokens: int
    diff_hash: str


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count_tokens(text: str) -> int:
    return len(text.split())


class SubAgentRegistry:
    def __init__(self, *, supervisor: Supervisor, agents_dir: Path, audit_dir: Path, fsync_on_write: bool) -> None:
        self._supervisor = supervisor
        self._agents_dir = agents_dir
        self._audit_dir = audit_dir
        self._fsync = fsync_on_write
        self._log = get_logger("anna.agents")
        self._agents_dir.mkdir(parents=True, exist_ok=True)

    def list_personas(self) -> list[SubAgentSpec]:
        out: list[SubAgentSpec] = []
        for path in sorted(self._agents_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            out.append(SubAgentSpec(
                slug=path.stem,
                persona_path=path,
                tokens=_count_tokens(text),
                diff_hash=_sha256(text),
            ))
        return out

    async def create_or_replace(
        self,
        *,
        slug: str,
        persona_text: str,
        creator_conv: str,
    ) -> SubAgentSpec:
        """Create a new persona file (or replace an existing one).

        Acquires the supervisor lock keyed on ``agents/<slug>``. Emits
        ``audit.subagent.created`` for first writes and
        ``audit.subagent.edited`` for replacements.
        """
        lock = await self._supervisor.acquire(f"agents/{slug}")
        async with lock:
            path = self._agents_dir / f"{slug}.md"
            prior_text = path.read_text(encoding="utf-8") if path.exists() else None
            prior_hash = _sha256(prior_text) if prior_text is not None else None

            path.write_text(persona_text, encoding="utf-8")
            new_hash = _sha256(persona_text)
            tokens = _count_tokens(persona_text)

            # NB: audit_event's first positional is ``name`` (the event name),
            # so we cannot pass the sub-agent slug as ``name=...``. Use
            # ``slug=...`` to match the skill registry's convention.
            if prior_text is None:
                audit_event(
                    "audit.subagent.created",
                    audit_dir=self._audit_dir,
                    actor="anna",
                    conv_key=creator_conv,
                    fsync_on_write=self._fsync,
                    slug=slug,
                    persona_file=str(path),
                    creator_conv=creator_conv,
                    tokens=tokens,
                    diff_hash=new_hash,
                )
            else:
                audit_event(
                    "audit.subagent.edited",
                    audit_dir=self._audit_dir,
                    actor="anna",
                    conv_key=creator_conv,
                    fsync_on_write=self._fsync,
                    slug=slug,
                    persona_file=str(path),
                    creator_conv=creator_conv,
                    tokens=tokens,
                    prior_diff_hash=prior_hash,
                    new_diff_hash=new_hash,
                )

            return SubAgentSpec(
                slug=slug,
                persona_path=path,
                tokens=tokens,
                diff_hash=new_hash,
            )
