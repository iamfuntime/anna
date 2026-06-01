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
