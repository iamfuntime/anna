"""Tests for the Phase 2 §3 sub-agent spawn runtime.

This file covers the runner skeleton (subtask 2). Subsequent subtasks
add persona/skills loading (3), system prompt assembly (4), options
building (5), the delegate happy path (6), the transcript writer (7),
and failure paths (8). Tests for those subtasks land in this same file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anna.agents.registry import SubAgentRegistry
from anna.config import AnnaConfig
from anna.runtime.subagent import DelegateResult, SubAgentError, SubAgentRunner
from anna.runtime.supervisor import Supervisor
from anna.skills.registry import SkillRegistry


def _make_runner(tmp_path: Path) -> SubAgentRunner:
    """Build a runner against a tmp anna_home with empty registries."""
    cfg = AnnaConfig.model_validate({"auth": {"mode": "max"}})
    # Override anna_home to point at tmp so audit / agents / skills dirs
    # do not leak into the user's real ~/anna during tests.
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    supervisor = Supervisor(config=cfg)
    agents_registry = SubAgentRegistry(
        supervisor=supervisor,
        agents_dir=tmp_path / "agents",
        audit_dir=tmp_path / "audit",
        fsync_on_write=False,
    )
    skills_registry = SkillRegistry(
        supervisor=supervisor,
        skills_dir=tmp_path / "skills",
        audit_dir=tmp_path / "audit",
        fsync_on_write=False,
    )
    return SubAgentRunner(
        config=cfg,
        supervisor=supervisor,
        agents_registry=agents_registry,
        skills_registry=skills_registry,
    )


# ---------------------------------------------------------------------------
# Subtask 2: skeleton
# ---------------------------------------------------------------------------


def test_runner_constructs_semaphore_from_config(tmp_path: Path) -> None:
    """The semaphore size equals config.subagents.max_concurrent."""
    runner = _make_runner(tmp_path)
    # The semaphore is private; introspect via the asyncio.Semaphore
    # public counter ``_value`` (only attribute giving the current
    # capacity). Equivalent assertion path is locked.
    sem = runner._semaphore  # noqa: SLF001
    assert isinstance(sem, asyncio.Semaphore)
    assert sem._value == 3  # noqa: SLF001  # default from SubagentsConfig


def test_runner_respects_custom_max_concurrent(tmp_path: Path) -> None:
    cfg = AnnaConfig.model_validate({"subagents": {"max_concurrent": 7}})
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    supervisor = Supervisor(config=cfg)
    agents_registry = SubAgentRegistry(
        supervisor=supervisor,
        agents_dir=tmp_path / "agents",
        audit_dir=tmp_path / "audit",
        fsync_on_write=False,
    )
    skills_registry = SkillRegistry(
        supervisor=supervisor,
        skills_dir=tmp_path / "skills",
        audit_dir=tmp_path / "audit",
        fsync_on_write=False,
    )
    runner = SubAgentRunner(
        config=cfg,
        supervisor=supervisor,
        agents_registry=agents_registry,
        skills_registry=skills_registry,
    )
    assert runner._semaphore._value == 7  # noqa: SLF001


def test_delegate_result_is_a_frozen_dataclass(tmp_path: Path) -> None:
    """DelegateResult exposes the new shape (text/transcript_path/...)."""
    result = DelegateResult(
        text="hello",
        transcript_path=tmp_path / "out.jsonl",
        tool_calls=["Read"],
        cost_usd=0.0123,
        duration_ms=456,
        status="ok",
    )
    assert result.text == "hello"
    assert result.transcript_path == tmp_path / "out.jsonl"
    assert result.tool_calls == ["Read"]
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.duration_ms == 456
    assert result.status == "ok"
    # Frozen — mutation must raise.
    with pytest.raises(Exception):
        result.text = "nope"  # type: ignore[misc]


def test_subagent_error_is_an_exception() -> None:
    err = SubAgentError("not_found")
    assert isinstance(err, Exception)
    assert str(err) == "not_found"


def test_runner_logger_name(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    # structlog BoundLogger stores the requested name in the bound
    # context for json output; the simplest assertion is that the call
    # itself does not raise and the logger is non-None.
    assert runner._log is not None  # noqa: SLF001


async def test_delegate_stub_raises_not_implemented(tmp_path: Path) -> None:
    """Until subtask 6 lands, delegate raises NotImplementedError."""
    runner = _make_runner(tmp_path)
    with pytest.raises(NotImplementedError):
        await runner.delegate(
            agent_slug="threat-researcher",
            task="dig into CVE-2026-0001",
            parent_conv_key="slack:dm:U123",
        )


# ---------------------------------------------------------------------------
# Subtask 3: persona + skills loading
# ---------------------------------------------------------------------------


def test_load_persona_reads_file_off_disk(tmp_path: Path) -> None:
    """A persona file at agents/<slug>.md is returned verbatim."""
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    persona = "You are a threat researcher.\n\nFocus on CVEs.\n"
    (agents_dir / "threat-researcher.md").write_text(persona, encoding="utf-8")
    assert runner._load_persona("threat-researcher") == persona  # noqa: SLF001


def test_load_persona_missing_raises_subagent_error(tmp_path: Path) -> None:
    """A missing persona file raises SubAgentError('not_found')."""
    runner = _make_runner(tmp_path)
    with pytest.raises(SubAgentError) as exc_info:
        runner._load_persona("does-not-exist")  # noqa: SLF001
    assert str(exc_info.value) == "not_found"


def test_load_persona_empty_file_returns_empty_string(tmp_path: Path) -> None:
    """An empty persona file returns '' — not an error.

    A persona-create flow that lands an empty file should not crash the
    runner; the operator can edit it in place and try again.
    """
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "blank.md").write_text("", encoding="utf-8")
    assert runner._load_persona("blank") == ""  # noqa: SLF001


def test_load_skills_missing_directory_returns_empty(tmp_path: Path) -> None:
    """No skills directory → [] (not an error)."""
    runner = _make_runner(tmp_path)
    assert runner._load_skills("nobody") == []  # noqa: SLF001


def test_load_skills_multiple_returns_alphabetical(tmp_path: Path) -> None:
    """Skill bodies come back sorted by slug for deterministic prompts."""
    runner = _make_runner(tmp_path)
    skills_dir = tmp_path / "skills" / "threat-researcher"
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Write in NOT-alphabetical order to prove the sort is doing work.
    (skills_dir / "zeta.md").write_text("z body", encoding="utf-8")
    (skills_dir / "alpha.md").write_text("a body", encoding="utf-8")
    (skills_dir / "mike.md").write_text("m body", encoding="utf-8")
    bodies = runner._load_skills("threat-researcher")  # noqa: SLF001
    assert bodies == ["a body", "m body", "z body"]


def test_load_skills_ignores_non_md_files(tmp_path: Path) -> None:
    """Non-.md files in the skills dir are ignored."""
    runner = _make_runner(tmp_path)
    skills_dir = tmp_path / "skills" / "threat-researcher"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "real.md").write_text("real body", encoding="utf-8")
    (skills_dir / "notes.txt").write_text("notes — skip me", encoding="utf-8")
    (skills_dir / "README").write_text("readme — skip me", encoding="utf-8")
    bodies = runner._load_skills("threat-researcher")  # noqa: SLF001
    assert bodies == ["real body"]
