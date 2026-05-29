"""Skill file registry.

Skills are persona modifiers attached to sub-agents. Files live at
``$ANNA_HOME/skills/<agent>/<slug>.md``. Creation emits
``audit.skill.created`` and edits emit ``audit.skill.edited``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anna.log import audit_event, get_logger
from anna.runtime.supervisor import Supervisor


SkillTrigger = Literal["third_iteration_threshold", "operator_request", "manual_paste"]


@dataclass(frozen=True)
class SkillSpec:
    agent: str
    slug: str
    skill_path: Path
    tokens: int
    diff_hash: str


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count_tokens(text: str) -> int:
    return len(text.split())


class SkillRegistry:
    def __init__(
        self,
        *,
        supervisor: Supervisor,
        skills_dir: Path,
        audit_dir: Path,
        fsync_on_write: bool,
    ) -> None:
        self._supervisor = supervisor
        self._skills_dir = skills_dir
        self._audit_dir = audit_dir
        self._fsync = fsync_on_write
        self._log = get_logger("anna.skills")
        self._skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self, agent: str) -> list[SkillSpec]:
        agent_dir = self._skills_dir / agent
        if not agent_dir.is_dir():
            return []
        out: list[SkillSpec] = []
        for path in sorted(agent_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            out.append(SkillSpec(
                agent=agent,
                slug=path.stem,
                skill_path=path,
                tokens=_count_tokens(text),
                diff_hash=_sha256(text),
            ))
        return out

    async def create_or_replace(
        self,
        *,
        agent: str,
        slug: str,
        skill_text: str,
        creator_conv: str,
        trigger: SkillTrigger,
        iteration_notes_appended: str | None = None,
    ) -> SkillSpec:
        lock = await self._supervisor.acquire(f"skills/{agent}/{slug}")
        async with lock:
            agent_dir = self._skills_dir / agent
            agent_dir.mkdir(parents=True, exist_ok=True)
            path = agent_dir / f"{slug}.md"
            prior_text = path.read_text(encoding="utf-8") if path.exists() else None
            prior_hash = _sha256(prior_text) if prior_text is not None else None

            path.write_text(skill_text, encoding="utf-8")
            new_hash = _sha256(skill_text)
            tokens = _count_tokens(skill_text)

            if prior_text is None:
                audit_event(
                    "audit.skill.created",
                    audit_dir=self._audit_dir,
                    actor="anna",
                    conv_key=creator_conv,
                    fsync_on_write=self._fsync,
                    agent=agent,
                    slug=slug,
                    skill_file=str(path),
                    creator_conv=creator_conv,
                    trigger=trigger,
                    tokens=tokens,
                    diff_hash=new_hash,
                )
            else:
                audit_event(
                    "audit.skill.edited",
                    audit_dir=self._audit_dir,
                    actor="anna",
                    conv_key=creator_conv,
                    fsync_on_write=self._fsync,
                    agent=agent,
                    slug=slug,
                    skill_file=str(path),
                    creator_conv=creator_conv,
                    tokens=tokens,
                    prior_diff_hash=prior_hash,
                    new_diff_hash=new_hash,
                    iteration_notes_appended=iteration_notes_appended,
                )

            return SkillSpec(
                agent=agent,
                slug=slug,
                skill_path=path,
                tokens=tokens,
                diff_hash=new_hash,
            )
