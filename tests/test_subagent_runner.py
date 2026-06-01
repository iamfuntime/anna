"""Tests for the Phase 2 §3 sub-agent spawn runtime.

This file covers the runner skeleton (subtask 2). Subsequent subtasks
add persona/skills loading (3), system prompt assembly (4), options
building (5), the delegate happy path (6), the transcript writer (7),
and failure paths (8). Tests for those subtasks land in this same file.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
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


async def test_delegate_missing_persona_raises_not_found(tmp_path: Path) -> None:
    """Subtask 6 happy path is wired; missing persona still raises early.

    Replaces the original ``NotImplementedError`` smoke test now that
    :meth:`SubAgentRunner.delegate` is implemented.
    """
    runner = _make_runner(tmp_path)
    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="threat-researcher",
            task="dig into CVE-2026-0001",
            parent_conv_key="slack:dm:U123",
        )
    assert exc_info.value.kind == "not_found"


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


# ---------------------------------------------------------------------------
# Subtask 4: system prompt assembly
# ---------------------------------------------------------------------------


def test_build_system_prompt_persona_only(tmp_path: Path) -> None:
    """No skills, no context → persona + delegation framing + task."""
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="You are a threat researcher.",
        skills=[],
        task="dig into CVE-2026-0001",
        context=None,
        vault_root=tmp_path / "vault",
    )
    assert prompt.startswith("You are a threat researcher.")
    assert "# Skills" not in prompt
    assert "# Delegation context" in prompt
    assert "You do not have the delegate tool" in prompt
    assert "# Task\ndig into CVE-2026-0001" in prompt
    assert "# Context" not in prompt
    assert str(tmp_path / "vault") in prompt


def test_build_system_prompt_persona_and_skills(tmp_path: Path) -> None:
    """Skills are concatenated with blank-line separators under # Skills."""
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="You are a threat researcher.",
        skills=["## CVE digging\nUse Brave + Mitre.", "## Vendor advisories\nCheck PSIRT feeds."],
        task="task body",
        context=None,
        vault_root=tmp_path / "vault",
    )
    assert "# Skills" in prompt
    assert "## CVE digging\nUse Brave + Mitre." in prompt
    assert "## Vendor advisories\nCheck PSIRT feeds." in prompt
    # Blank-line separator between skills.
    skills_idx = prompt.index("# Skills")
    delegation_idx = prompt.index("# Delegation context")
    skills_section = prompt[skills_idx:delegation_idx]
    assert "Use Brave + Mitre.\n\n## Vendor advisories" in skills_section


def test_build_system_prompt_persona_and_context(tmp_path: Path) -> None:
    """Context dict renders as YAML under # Context when non-None."""
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="You are a threat researcher.",
        skills=[],
        task="task body",
        context={"cve_id": "CVE-2026-0001", "severity": "high"},
        vault_root=tmp_path / "vault",
    )
    assert "# Context" in prompt
    # YAML rendering (safe_dump, sort_keys=True).
    assert "cve_id: CVE-2026-0001" in prompt
    assert "severity: high" in prompt
    # Section ordering: Task before Context.
    assert prompt.index("# Task") < prompt.index("# Context")


def test_build_system_prompt_persona_skills_and_context(tmp_path: Path) -> None:
    """All four sections present, in the canonical order."""
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="You are a researcher.",
        skills=["skill body one", "skill body two"],
        task="task body",
        context={"key": "value"},
        vault_root=tmp_path / "vault",
    )
    # Canonical order: persona → Skills → Delegation context → Task → Context.
    persona_idx = prompt.index("You are a researcher.")
    skills_idx = prompt.index("# Skills")
    delegation_idx = prompt.index("# Delegation context")
    task_idx = prompt.index("# Task")
    context_idx = prompt.index("# Context")
    assert persona_idx < skills_idx < delegation_idx < task_idx < context_idx


def test_build_system_prompt_no_skill_header_per_skill(tmp_path: Path) -> None:
    """No '## <slug>' wrapper around individual skills — they own their headings.

    A regression here would compound the skill's own markdown headings
    and confuse the model's section parsing.
    """
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="persona",
        skills=["## skill heading\nbody"],
        task="t",
        context=None,
        vault_root=tmp_path / "vault",
    )
    skills_section = prompt[prompt.index("# Skills"):prompt.index("# Delegation")]
    # Exactly one '## skill heading' — no wrapped slug heading layered above it.
    assert skills_section.count("## skill heading") == 1


# ---------------------------------------------------------------------------
# Subtask 5: sub-agent options builder
# ---------------------------------------------------------------------------


def _make_runner_with_tools(tmp_path: Path, *, tools_enabled: bool = True) -> SubAgentRunner:
    """Build a runner with full config overrides so tools.enabled is configurable."""
    raw: dict = {"tools": {"enabled": tools_enabled}}
    cfg = AnnaConfig.model_validate(raw)
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    cfg.vault.path = str(tmp_path / "vault")
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


def test_build_subagent_options_mounts_anna_web_when_tools_enabled(tmp_path: Path) -> None:
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert set(options.mcp_servers.keys()) == {"anna_web"}


def test_build_subagent_options_omits_all_mcp_when_tools_disabled(tmp_path: Path) -> None:
    runner = _make_runner_with_tools(tmp_path, tools_enabled=False)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.mcp_servers == {}


def test_build_subagent_options_never_mounts_forbidden_servers(tmp_path: Path) -> None:
    """anna_self_edit / anna_google / anna_delegate are never on a sub-agent."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    keys = set(options.mcp_servers.keys())
    assert "anna_self_edit" not in keys
    assert "anna_google" not in keys
    assert "anna_delegate" not in keys


def test_build_subagent_options_allowed_tools_excludes_forbidden_prefixes(tmp_path: Path) -> None:
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    for name in options.allowed_tools:
        assert not name.startswith("mcp__anna_self_edit__"), name
        assert not name.startswith("mcp__anna_google__"), name
        assert not name.startswith("mcp__anna_delegate__"), name


def test_build_subagent_options_allowed_tools_from_config(tmp_path: Path) -> None:
    """allowed_tools is populated from config.subagents.allowed_tools verbatim."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert sorted(options.allowed_tools) == sorted(runner._config.subagents.allowed_tools)  # noqa: SLF001


def test_build_subagent_options_permission_mode_default(tmp_path: Path) -> None:
    """Default permission_mode is acceptEdits (stricter than the worker)."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.permission_mode == "acceptEdits"


def test_build_subagent_options_permission_mode_override(tmp_path: Path) -> None:
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
        permission_mode_override="bypassPermissions",
    )
    assert options.permission_mode == "bypassPermissions"


def test_build_subagent_options_setting_sources_empty(tmp_path: Path) -> None:
    """No host Claude Code env should leak into a sub-agent."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.setting_sources == []


def test_build_subagent_options_cwd_is_vault_root(tmp_path: Path) -> None:
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.cwd == str(runner._config.vault.resolved_path)  # noqa: SLF001


def test_build_subagent_options_add_dirs_empty(tmp_path: Path) -> None:
    """Sub-agents must not see core/ — add_dirs is hard-coded empty."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.add_dirs == []


def test_build_system_prompt_is_pure(tmp_path: Path) -> None:
    """No side effects: two calls with the same args return identical output."""
    args = {
        "persona": "p",
        "skills": ["s1", "s2"],
        "task": "t",
        "context": {"a": 1},
        "vault_root": tmp_path / "v",
    }
    a = SubAgentRunner._build_system_prompt(**args)  # noqa: SLF001
    b = SubAgentRunner._build_system_prompt(**args)  # noqa: SLF001
    assert a == b


# ---------------------------------------------------------------------------
# Subtask 7: transcript writer
# ---------------------------------------------------------------------------


def test_write_transcript_line_creates_file_at_correct_path(tmp_path: Path) -> None:
    """File lands at transcripts/subagent/<slug>/<today>.jsonl."""
    runner = _make_runner(tmp_path)
    path = runner._write_transcript_line(  # noqa: SLF001
        slug="threat-researcher",
        conv_key="subagent:threat-researcher:abc",
        direction="task",
        text="dig into CVE",
        audit_id="audit-uuid-1",
    )
    today = date.today().isoformat()
    expected = tmp_path / "transcripts" / "subagent" / "threat-researcher" / f"{today}.jsonl"
    assert path == expected
    assert expected.exists()


def test_write_transcript_line_writes_valid_json(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    path = runner._write_transcript_line(  # noqa: SLF001
        slug="threat-researcher",
        conv_key="subagent:threat-researcher:abc",
        direction="task",
        text="dig into CVE",
        audit_id="audit-uuid-1",
        parent_conv="slack:dm:U123",
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["direction"] == "task"
    assert record["conv_key"] == "subagent:threat-researcher:abc"
    assert record["text"] == "dig into CVE"
    assert record["audit_id"] == "audit-uuid-1"
    assert record["parent_conv"] == "slack:dm:U123"
    # ts field is set by the writer.
    assert "ts" in record


def test_write_transcript_line_appends_on_repeat_calls(tmp_path: Path) -> None:
    """Two delegations to the same slug+day append to the same file."""
    runner = _make_runner(tmp_path)
    runner._write_transcript_line(  # noqa: SLF001
        slug="threat-researcher",
        conv_key="subagent:threat-researcher:abc",
        direction="task",
        text="task one",
        audit_id="a1",
    )
    runner._write_transcript_line(  # noqa: SLF001
        slug="threat-researcher",
        conv_key="subagent:threat-researcher:abc",
        direction="outbound",
        text="reply one",
        audit_id="a1",
        duration_seconds=12.4,
    )
    runner._write_transcript_line(  # noqa: SLF001
        slug="threat-researcher",
        conv_key="subagent:threat-researcher:def",
        direction="task",
        text="task two",
        audit_id="a2",
    )
    today = date.today().isoformat()
    path = tmp_path / "transcripts" / "subagent" / "threat-researcher" / f"{today}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert records[0]["direction"] == "task"
    assert records[0]["text"] == "task one"
    assert records[1]["direction"] == "outbound"
    assert records[1]["duration_seconds"] == 12.4
    assert records[2]["direction"] == "task"
    assert records[2]["text"] == "task two"


def test_write_transcript_line_creates_parent_directories(tmp_path: Path) -> None:
    """The transcripts/subagent/<slug>/ tree is mkdir-p'd on first call."""
    runner = _make_runner(tmp_path)
    # Pre-condition: nothing exists yet under transcripts/.
    transcripts_dir = tmp_path / "transcripts"
    assert not transcripts_dir.exists()
    runner._write_transcript_line(  # noqa: SLF001
        slug="brand-new-slug",
        conv_key="subagent:brand-new-slug:xyz",
        direction="task",
        text="hello",
        audit_id="audit-1",
    )
    assert (transcripts_dir / "subagent" / "brand-new-slug").is_dir()


def test_write_transcript_line_per_slug_isolation(tmp_path: Path) -> None:
    """Two slugs land in separate directories on the same day."""
    runner = _make_runner(tmp_path)
    runner._write_transcript_line(  # noqa: SLF001
        slug="threat-researcher",
        conv_key="subagent:threat-researcher:abc",
        direction="task",
        text="t1",
        audit_id="a1",
    )
    runner._write_transcript_line(  # noqa: SLF001
        slug="vuln-triager",
        conv_key="subagent:vuln-triager:abc",
        direction="task",
        text="t2",
        audit_id="a2",
    )
    today = date.today().isoformat()
    tr_path = tmp_path / "transcripts" / "subagent" / "threat-researcher" / f"{today}.jsonl"
    vt_path = tmp_path / "transcripts" / "subagent" / "vuln-triager" / f"{today}.jsonl"
    assert tr_path.exists()
    assert vt_path.exists()
    assert json.loads(tr_path.read_text(encoding="utf-8"))["text"] == "t1"
    assert json.loads(vt_path.read_text(encoding="utf-8"))["text"] == "t2"


# ---------------------------------------------------------------------------
# Subtask 6: delegate happy path
# ---------------------------------------------------------------------------


from dataclasses import dataclass as _dataclass
from typing import Any as _Any


@_dataclass
class _FakeTextBlock:
    text: str


@_dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, _Any]


@_dataclass
class _FakeAssistantMessage:
    content: list[_Any]


@_dataclass
class _FakeResultMessage:
    total_cost_usd: float | None = None
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "fake"
    subtype: str = "success"


class _FakeReplyClient:
    """Fake ClaudeSDKClient that yields one assistant message + result."""

    def __init__(
        self,
        *,
        reply: str = "all done",
        tool_calls: list[str] | None = None,
        cost: float | None = 0.0042,
    ) -> None:
        self._reply = reply
        self._tool_calls = tool_calls or []
        self._cost = cost
        self.queries: list[str] = []
        self.entered = False
        self.exited = False

    def __init_options__(self, options: _Any) -> None:
        pass

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_a):
        self.exited = True
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        content: list[_Any] = []
        for name in self._tool_calls:
            content.append(_FakeToolUseBlock(id=name, name=name, input={}))
        content.append(_FakeTextBlock(text=self._reply))
        yield _FakeAssistantMessage(content=content)
        yield _FakeResultMessage(total_cost_usd=self._cost)


def _install_fake_sdk(monkeypatch, client_factory) -> None:
    """Patch the SDK names looked up by ``SubAgentRunner.delegate``.

    The runner does a lazy ``from claude_agent_sdk import ...`` inside
    ``delegate``, so patching the module attributes here is enough.
    """
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    monkeypatch.setattr(
        sdk,
        "ClaudeSDKClient",
        lambda options=None: client_factory(options),
        raising=False,
    )


def _write_persona(tmp_path: Path, slug: str, body: str = "persona body") -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{slug}.md").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_delegate_happy_path_returns_ok_result(
    tmp_path: Path, monkeypatch
) -> None:
    """Happy path: persona loads, fake SDK runs, DelegateResult populated."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "threat-researcher")

    captured: list[_FakeReplyClient] = []

    def _factory(_options):
        c = _FakeReplyClient(
            reply="CVE-2026-0001 affects Foo v1.2.",
            tool_calls=["mcp__anna_web__web_search", "Read"],
            cost=0.0123,
        )
        captured.append(c)
        return c

    _install_fake_sdk(monkeypatch, _factory)

    result = await runner.delegate(
        agent_slug="threat-researcher",
        task="dig into CVE-2026-0001",
        parent_conv_key="slack:dm:U123",
    )

    assert isinstance(result, DelegateResult)
    assert result.status == "ok"
    assert result.text == "CVE-2026-0001 affects Foo v1.2."
    assert result.tool_calls == ["mcp__anna_web__web_search", "Read"]
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.duration_ms >= 0
    today = date.today().isoformat()
    expected = tmp_path / "transcripts" / "subagent" / "threat-researcher" / f"{today}.jsonl"
    assert result.transcript_path == expected
    assert expected.exists()
    # SDK lifecycle was exercised end-to-end.
    assert len(captured) == 1
    assert captured[0].entered is True
    assert captured[0].exited is True
    assert captured[0].queries == ["dig into CVE-2026-0001"]


@pytest.mark.asyncio
async def test_delegate_writes_task_and_outbound_transcript_lines(
    tmp_path: Path, monkeypatch
) -> None:
    """Two transcript lines per delegation: task, then outbound."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "threat-researcher")
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _FakeReplyClient(reply="reply body"),
    )

    result = await runner.delegate(
        agent_slug="threat-researcher",
        task="dig into CVE",
        parent_conv_key="slack:dm:U123",
    )
    lines = result.transcript_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 2
    assert records[0]["direction"] == "task"
    assert records[0]["text"] == "dig into CVE"
    assert records[0]["parent_conv"] == "slack:dm:U123"
    assert records[1]["direction"] == "outbound"
    assert records[1]["text"] == "reply body"
    assert records[1]["parent_conv"] == "slack:dm:U123"
    # task + outbound share the same audit_id.
    assert records[0]["audit_id"] == records[1]["audit_id"]
    # synthetic conv_key shape.
    assert records[0]["conv_key"].startswith("subagent:threat-researcher:")


@pytest.mark.asyncio
async def test_delegate_emits_spawn_and_complete_audit_events(
    tmp_path: Path, monkeypatch
) -> None:
    """spawn + complete events fire in order, share audit_id."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "threat-researcher")
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _FakeReplyClient(reply="reply"),
    )

    await runner.delegate(
        agent_slug="threat-researcher",
        task="t",
        parent_conv_key="slack:dm:U123",
    )

    audit_dir = tmp_path / "audit"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    audit_path = audit_dir / f"audit-{today}.jsonl"
    assert audit_path.exists()
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    names = [e["event"] for e in events]
    assert "audit.subagent.spawn" in names
    assert "audit.subagent.complete" in names
    # spawn comes before complete.
    assert names.index("audit.subagent.spawn") < names.index(
        "audit.subagent.complete"
    )
    spawn = next(e for e in events if e["event"] == "audit.subagent.spawn")
    complete = next(e for e in events if e["event"] == "audit.subagent.complete")
    assert spawn["audit_id"] == complete["audit_id"]
    assert spawn["slug"] == "threat-researcher"
    assert spawn["task"] == "t"
    assert spawn["timeout_seconds"] == runner._config.subagents.default_timeout_seconds  # noqa: SLF001
    assert complete["output_length"] == len("reply")


@pytest.mark.asyncio
async def test_delegate_truncates_task_in_spawn_audit(
    tmp_path: Path, monkeypatch
) -> None:
    """Long tasks are truncated to 500 chars in the spawn audit event."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _FakeReplyClient(reply="r"),
    )

    long_task = "x" * 2000
    await runner.delegate(
        agent_slug="slug",
        task=long_task,
        parent_conv_key="slack:dm:U123",
    )
    audit_dir = tmp_path / "audit"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events = [
        json.loads(line)
        for line in (audit_dir / f"audit-{today}.jsonl").read_text().splitlines()
    ]
    spawn = next(e for e in events if e["event"] == "audit.subagent.spawn")
    assert len(spawn["task"]) == 500


@pytest.mark.asyncio
async def test_delegate_releases_semaphore_on_success(
    tmp_path: Path, monkeypatch
) -> None:
    """The semaphore counter returns to its baseline after a successful run."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _FakeReplyClient(reply="r"),
    )

    baseline = runner._semaphore._value  # noqa: SLF001
    await runner.delegate(
        agent_slug="slug",
        task="t",
        parent_conv_key="slack:dm:U123",
    )
    assert runner._semaphore._value == baseline  # noqa: SLF001


# `datetime` and `timezone` are imported at top of file via `from datetime
# import date`; pull the rest for audit-path queries.
from datetime import datetime, timezone  # noqa: E402
