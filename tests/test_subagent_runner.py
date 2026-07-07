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
    """A persona file at agents/<slug>.md has its body returned verbatim."""
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    persona = "You are a threat researcher.\n\nFocus on CVEs.\n"
    (agents_dir / "threat-researcher.md").write_text(persona, encoding="utf-8")
    doc = runner._load_persona("threat-researcher")  # noqa: SLF001
    assert doc.body == persona
    assert doc.grants is None


def test_load_persona_missing_raises_subagent_error(tmp_path: Path) -> None:
    """A missing persona file raises SubAgentError('not_found')."""
    runner = _make_runner(tmp_path)
    with pytest.raises(SubAgentError) as exc_info:
        runner._load_persona("does-not-exist")  # noqa: SLF001
    assert str(exc_info.value) == "not_found"


def test_load_persona_empty_file_returns_empty_string(tmp_path: Path) -> None:
    """An empty persona file returns body='' — not an error.

    A persona-create flow that lands an empty file should not crash the
    runner; the operator can edit it in place and try again.
    """
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "blank.md").write_text("", encoding="utf-8")
    doc = runner._load_persona("blank")  # noqa: SLF001
    assert doc.body == ""
    assert doc.grants is None


def test_load_persona_frontmatter_grants_parses(tmp_path: Path) -> None:
    """A grants: frontmatter block parses into AgentGrants; body is stripped."""
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        "grants:\n"
        "  mcp_servers: [playwright]\n"
        "  write_dirs: [reports]\n"
        "  permission_mode: bypassPermissions\n"
        "---\n"
        "# Persona\nYou are a researcher.\n"
    )
    (agents_dir / "fm.md").write_text(text, encoding="utf-8")
    doc = runner._load_persona("fm")  # noqa: SLF001
    assert doc.body == "# Persona\nYou are a researcher.\n"
    assert "---" not in doc.body
    assert doc.grants is not None
    assert doc.grants.mcp_servers == ["playwright"]
    assert doc.grants.write_dirs == ["reports"]
    assert doc.grants.permission_mode == "bypassPermissions"


def test_load_persona_malformed_grants_returns_none(tmp_path: Path) -> None:
    """A grants block of the wrong shape degrades to grants=None, body intact."""
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    text = "---\ngrants: not-a-mapping\n---\n# Persona\nbody\n"
    (agents_dir / "bad.md").write_text(text, encoding="utf-8")
    doc = runner._load_persona("bad")  # noqa: SLF001
    assert doc.body == "# Persona\nbody\n"
    assert doc.grants is None


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


def test_build_subagent_options_model_defaults_to_none(tmp_path: Path) -> None:
    """With no grant + no runtime.model, the SDK model is None (CLI default)."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.model is None


def test_build_subagent_options_model_from_resolved_grant(tmp_path: Path) -> None:
    """A resolved grant's model is threaded into ClaudeAgentOptions.model."""
    from anna.runtime.grants import ResolvedGrant

    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
        resolved=ResolvedGrant(model="haiku"),
    )
    assert options.model == "haiku"


def test_build_subagent_options_effort_defaults_to_none(tmp_path: Path) -> None:
    """With no grant effort, the SDK effort is None (SDK default 'high')."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.effort is None


def test_build_subagent_options_effort_not_inherited_from_runtime(
    tmp_path: Path,
) -> None:
    """runtime.effort is main-loop only — the synthesized fallback grant
    leaves effort None, so the sub-agent gets the SDK default."""
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    runner._config.runtime.effort = "xhigh"  # noqa: SLF001
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.effort is None


def test_build_subagent_options_effort_from_resolved_grant(tmp_path: Path) -> None:
    """A resolved grant's effort is threaded into ClaudeAgentOptions.effort."""
    from anna.runtime.grants import ResolvedGrant

    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
        resolved=ResolvedGrant(effort="low"),
    )
    assert options.effort == "low"


def test_build_subagent_options_sets_claude_config_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-agents get the same isolated CLAUDE_CONFIG_DIR as the main loop.

    In max mode they ALSO get CLAUDE_SECURESTORAGE_CONFIG_DIR pointing at the
    operator's real ~/.claude so OAuth reads / refresh-writes share the
    operator's .credentials.json (no symlink seeded).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    assert runner._config.auth.mode == "max"  # noqa: SLF001
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.env["CLAUDE_CONFIG_DIR"] == str(
        runner._config.claude_runtime_dir  # noqa: SLF001
    )
    assert options.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(
        home / ".claude"
    )
    assert options.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(
        runner._config.claude_securestorage_dir  # noqa: SLF001
    )
    assert options.setting_sources == []


def test_build_subagent_options_omits_securestorage_in_api_key_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_key mode does not set CLAUDE_SECURESTORAGE_CONFIG_DIR for sub-agents."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    runner._config.auth.mode = "api_key"  # noqa: SLF001
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.env["CLAUDE_CONFIG_DIR"] == str(
        runner._config.claude_runtime_dir  # noqa: SLF001
    )
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in options.env


def test_build_subagent_options_cwd_is_vault_root(tmp_path: Path) -> None:
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.cwd == str(runner._config.vault.resolved_path)  # noqa: SLF001


def test_build_subagent_options_add_dirs_empty_by_default(tmp_path: Path) -> None:
    """add_dirs is empty when subagents.extra_dirs is unset (the default).

    Sub-agents do not see core/ because no configured extra_dir points at
    it; with no extra_dirs at all, the only reachable root is the cwd
    (ANNA vault).
    """
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.add_dirs == []


def test_build_subagent_options_add_dirs_from_config(tmp_path: Path) -> None:
    """subagents.extra_dirs flows into add_dirs, ~-expanded, in order."""
    raw: dict = {
        "tools": {"enabled": True},
        "subagents": {"extra_dirs": ["~/Obsidian/Brain", str(tmp_path / "extra")]},
    }
    cfg = AnnaConfig.model_validate(raw)
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    cfg.vault.path = str(tmp_path / "vault")
    supervisor = Supervisor(config=cfg)
    runner = SubAgentRunner(
        config=cfg,
        supervisor=supervisor,
        agents_registry=SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=tmp_path / "agents",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
        skills_registry=SkillRegistry(
            supervisor=supervisor,
            skills_dir=tmp_path / "skills",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
    )
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert options.add_dirs == [
        str(Path("~/Obsidian/Brain").expanduser()),
        str(tmp_path / "extra"),
    ]
    # No '~' should survive into the mounted paths.
    assert all("~" not in d for d in options.add_dirs)


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
# Subtask 7/8: per-agent grant wiring into options + prompt
# ---------------------------------------------------------------------------


def _make_runner_with_subagents(tmp_path: Path, **subagents: object) -> SubAgentRunner:
    """Runner whose config carries the given subagents block overrides."""
    raw: dict = {"tools": {"enabled": True}, "subagents": subagents}
    cfg = AnnaConfig.model_validate(raw)
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    cfg.vault.path = str(tmp_path / "vault")
    supervisor = Supervisor(config=cfg)
    return SubAgentRunner(
        config=cfg,
        supervisor=supervisor,
        agents_registry=SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=tmp_path / "agents",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
        skills_registry=SkillRegistry(
            supervisor=supervisor,
            skills_dir=tmp_path / "skills",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
    )


def test_build_subagent_options_empty_grant_equivalent_to_today(
    tmp_path: Path,
) -> None:
    """No grant (resolved=None) reproduces the pre-chunk-A option surface.

    anna_web only (tools enabled), add_dirs from extra_dirs, allowed_tools
    equal to subagents.allowed_tools.
    """
    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:slug:abc",
    )
    assert set(options.mcp_servers.keys()) == {"anna_web"}
    assert options.add_dirs == []
    assert sorted(options.allowed_tools) == sorted(
        runner._config.subagents.allowed_tools  # noqa: SLF001
    )
    assert options.permission_mode == "acceptEdits"


def test_build_subagent_options_resolved_grant_changes_surface(
    tmp_path: Path,
) -> None:
    """A resolved grant drives mcp_servers / add_dirs / allowed_tools / mode."""
    from anna.runtime.grants import resolve_effective_grant

    runner = _make_runner_with_subagents(
        tmp_path,
        dir_pool={"reports": str(tmp_path / "reports")},
        mcp_registry={"pw": {"kind": "stdio", "command": "npx"}},
        agents={
            "writer": {
                "write_dirs": ["reports"],
                "mcp_servers": ["pw"],
                "allowed_tools": ["Read", "Glob"],
                "permission_mode": "bypassPermissions",
            }
        },
    )
    resolved = resolve_effective_grant(runner._config, "writer", None)  # noqa: SLF001
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:writer:abc",
        resolved=resolved,
    )
    # External stdio server bound; anna_web NOT present (grant replaced it).
    assert set(options.mcp_servers.keys()) == {"pw"}
    assert options.mcp_servers["pw"]["command"] == "npx"
    # write_dirs resolved to the pool's absolute path.
    assert options.add_dirs == [str(tmp_path / "reports")]
    # allowed_tools = grant tools UNION the external wildcard, deduped.
    assert options.allowed_tools == ["Read", "Glob", "mcp__pw__*"]
    assert options.permission_mode == "bypassPermissions"


def test_build_subagent_options_permission_override_beats_resolved(
    tmp_path: Path,
) -> None:
    """An explicit permission_mode_override wins over the resolved grant."""
    from anna.runtime.grants import resolve_effective_grant

    runner = _make_runner_with_subagents(
        tmp_path,
        agents={"writer": {"permission_mode": "bypassPermissions"}},
    )
    resolved = resolve_effective_grant(runner._config, "writer", None)  # noqa: SLF001
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:writer:abc",
        permission_mode_override="plan",
        resolved=resolved,
    )
    assert options.permission_mode == "plan"


def test_build_subagent_options_forbidden_builtin_never_mounts(
    tmp_path: Path,
) -> None:
    """A registry entry naming a forbidden builtin never reaches mcp_servers."""
    from anna.runtime.grants import resolve_effective_grant

    runner = _make_runner_with_subagents(
        tmp_path,
        mcp_registry={"evil": {"kind": "builtin", "builtin_name": "anna_self_edit"}},
        agents={"writer": {"mcp_servers": ["evil"]}},
    )
    resolved = resolve_effective_grant(runner._config, "writer", None)  # noqa: SLF001
    options = runner._build_subagent_options(  # noqa: SLF001
        system_prompt="system",
        conv_key="subagent:writer:abc",
        resolved=resolved,
    )
    assert "anna_self_edit" not in options.mcp_servers
    assert "evil" not in options.mcp_servers
    assert options.mcp_servers == {}


def test_summarize_server_tools_fallback_is_none(tmp_path: Path) -> None:
    """The fallback-only (anna_web) surface returns None → default wording."""
    from anna.runtime.grants import resolve_effective_grant

    runner = _make_runner_with_tools(tmp_path, tools_enabled=True)
    resolved = resolve_effective_grant(runner._config, "x", None)  # noqa: SLF001
    assert SubAgentRunner._summarize_server_tools(resolved) is None  # noqa: SLF001


def test_summarize_server_tools_names_external(tmp_path: Path) -> None:
    """A resolved external server is named in the summary phrase."""
    from anna.runtime.grants import resolve_effective_grant

    runner = _make_runner_with_subagents(
        tmp_path,
        mcp_registry={
            "detections": {
                "kind": "http",
                "url": "https://srv/mcp",
                "tool_names": ["search_rules"],
            }
        },
        agents={"writer": {"mcp_servers": ["detections"]}},
    )
    resolved = resolve_effective_grant(runner._config, "writer", None)  # noqa: SLF001
    phrase = SubAgentRunner._summarize_server_tools(resolved)  # noqa: SLF001
    assert phrase == "detections (search_rules)"


def test_build_system_prompt_empty_case_byte_identical(tmp_path: Path) -> None:
    """server_tools=None reproduces the pre-chunk-A delegation wording exactly."""
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="persona",
        skills=[],
        task="t",
        context=None,
        vault_root=tmp_path / "vault",
        server_tools=None,
    )
    assert (
        "You have web_search, web_fetch, and vault_download for "
        "outside-the-vault work." in prompt
    )


def test_build_system_prompt_names_resolved_servers(tmp_path: Path) -> None:
    """A non-fallback server_tools phrase appears in the delegation framing."""
    prompt = SubAgentRunner._build_system_prompt(  # noqa: SLF001
        persona="persona",
        skills=[],
        task="t",
        context=None,
        vault_root=tmp_path / "vault",
        extra_dirs=[str(tmp_path / "reports")],
        server_tools="playwright",
    )
    assert "You have playwright for outside-the-vault work." in prompt
    assert str(tmp_path / "reports") in prompt


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
    # Trailer carries the resolved model; no grant + no runtime.model
    # resolves to the CLI-default sentinel (same as the spawn audit).
    assert records[1]["model"] == "<cli-default>"
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
    # complete stamps the delegation cost (fake client default cost).
    assert complete["cost_usd"] == pytest.approx(0.0042)


@pytest.mark.asyncio
async def test_delegate_trailer_model_matches_spawn_audit(
    tmp_path: Path, monkeypatch
) -> None:
    """The outbound trailer records the resolved model, same as spawn.

    A frontmatter ``grants.model`` resolves through
    resolve_effective_grant; both the spawn audit event and the outbound
    transcript trailer must record that same string so trailer consumers
    (Mission Control delegations view) can bucket runs per model without
    joining back to the audit log.
    """
    runner = _make_runner(tmp_path)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "modeled.md").write_text(
        "---\ngrants:\n  model: haiku\n---\npersona body\n",
        encoding="utf-8",
    )
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _FakeReplyClient(reply="reply"),
    )

    result = await runner.delegate(
        agent_slug="modeled",
        task="t",
        parent_conv_key="slack:dm:U123",
    )

    lines = result.transcript_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    outbound = next(r for r in records if r["direction"] == "outbound")
    assert outbound["model"] == "haiku"

    audit_dir = tmp_path / "audit"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    audit_path = audit_dir / f"audit-{today}.jsonl"
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    spawn = next(e for e in events if e["event"] == "audit.subagent.spawn")
    assert spawn["model"] == "haiku"
    assert outbound["model"] == spawn["model"]


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


# ---------------------------------------------------------------------------
# Subtask 8: failure paths
# ---------------------------------------------------------------------------


class _RaiseOnQueryClient:
    """Fake SDK client whose query() raises a configurable exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_a):
        self.exited = True
        return None

    async def query(self, prompt: str) -> None:
        raise self._exc

    async def receive_response(self):
        if False:  # pragma: no cover - unreachable
            yield None


class _RaiseOnReceiveClient:
    """Fake SDK client whose receive_response() raises mid-stream."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_a):
        self.exited = True
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self):
        raise self._exc
        yield  # pragma: no cover - unreachable, makes this an async generator


class _SlowReplyClient:
    """Fake SDK client whose receive_response() never yields ResultMessage.

    Used to exercise the asyncio.wait_for timeout path. The await
    inside the loop is long enough to outlive the configured
    timeout_seconds, after which wait_for cancels the task.
    """

    def __init__(self, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_a):
        self.exited = True
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self):
        await asyncio.sleep(self._sleep_seconds)
        # If we ever reach here the test setup is wrong — the
        # outer wait_for should have fired first.
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text="late")])
        yield _FakeResultMessage()


def _audit_events(tmp_path: Path) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / "audit" / f"audit-{today}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_delegate_not_found_emits_fail_audit_and_raises(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _make_runner(tmp_path)
    # No persona file written → not_found.
    _install_fake_sdk(monkeypatch, lambda opts: _FakeReplyClient())

    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="ghost",
            task="t",
            parent_conv_key="slack:dm:U123",
        )
    assert exc_info.value.kind == "not_found"

    events = _audit_events(tmp_path)
    fail_events = [e for e in events if e["event"] == "audit.subagent.fail"]
    assert len(fail_events) == 1
    assert fail_events[0]["kind"] == "not_found"
    # Spawn-time fail before any sub-agent transcript dir is created.
    today = date.today().isoformat()
    transcript_dir = tmp_path / "transcripts" / "subagent" / "ghost"
    assert not (transcript_dir / f"{today}.jsonl").exists()
    # Semaphore was released.
    assert runner._semaphore._value == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_delegate_depth_violation_short_circuits_before_semaphore(
    tmp_path: Path, monkeypatch
) -> None:
    """A parent_conv_key starting with ``subagent:`` raises immediately."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(monkeypatch, lambda opts: _FakeReplyClient())

    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="slug",
            task="t",
            parent_conv_key="subagent:other:abc-uuid",
        )
    assert exc_info.value.kind == "depth_violation"

    events = _audit_events(tmp_path)
    fail = next(e for e in events if e["event"] == "audit.subagent.fail")
    assert fail["kind"] == "depth_violation"
    # Semaphore was never acquired (still at full capacity).
    assert runner._semaphore._value == 3  # noqa: SLF001
    # No spawn audit fired.
    assert not any(e["event"] == "audit.subagent.spawn" for e in events)


@pytest.mark.asyncio
async def test_delegate_concurrency_acquire_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    """The 4th concurrent delegate raises concurrency_timeout once the wait elapses."""
    # Tighten the acquire timeout so the test does not block.
    cfg = AnnaConfig.model_validate(
        {"subagents": {"max_concurrent": 1, "concurrency_acquire_timeout_seconds": 1}}
    )
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    supervisor = Supervisor(config=cfg)
    runner = SubAgentRunner(
        config=cfg,
        supervisor=supervisor,
        agents_registry=SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=tmp_path / "agents",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
        skills_registry=SkillRegistry(
            supervisor=supervisor,
            skills_dir=tmp_path / "skills",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
    )
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(monkeypatch, lambda opts: _FakeReplyClient())

    # Drain the lone semaphore slot. We hold it manually so the
    # delegate call has nothing to acquire and trips the timeout.
    await runner._semaphore.acquire()  # noqa: SLF001

    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="slug",
            task="t",
            parent_conv_key="slack:dm:U123",
        )
    assert exc_info.value.kind == "concurrency_timeout"

    events = _audit_events(tmp_path)
    fail = next(e for e in events if e["event"] == "audit.subagent.fail")
    assert fail["kind"] == "concurrency_timeout"
    # Release the held slot to leave the runner clean for any follow-on.
    runner._semaphore.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_delegate_query_exception_emits_fail_audit(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _RaiseOnQueryClient(RuntimeError("rate limit")),
    )

    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="slug",
            task="t",
            parent_conv_key="slack:dm:U123",
        )
    assert exc_info.value.kind == "error"
    assert "rate limit" in (exc_info.value.reason or "")

    events = _audit_events(tmp_path)
    # Both spawn and fail fire when the exception is mid-run.
    assert any(e["event"] == "audit.subagent.spawn" for e in events)
    fail = next(e for e in events if e["event"] == "audit.subagent.fail")
    assert fail["kind"] == "error"
    assert "rate limit" in fail["reason"]

    # Transcript fail line was written.
    today = date.today().isoformat()
    transcript = tmp_path / "transcripts" / "subagent" / "slug" / f"{today}.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    directions = [r["direction"] for r in records]
    assert "task" in directions
    assert "fail" in directions
    # Semaphore released.
    assert runner._semaphore._value == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_delegate_receive_exception_emits_fail_audit(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _RaiseOnReceiveClient(ConnectionError("socket closed")),
    )

    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="slug",
            task="t",
            parent_conv_key="slack:dm:U123",
        )
    assert exc_info.value.kind == "error"
    assert "socket closed" in (exc_info.value.reason or "")

    events = _audit_events(tmp_path)
    fail = next(e for e in events if e["event"] == "audit.subagent.fail")
    assert fail["kind"] == "error"
    assert runner._semaphore._value == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_delegate_timeout_emits_fail_audit_and_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """A query that outlives timeout_seconds raises SubAgentError(timeout)."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    # The fake client sleeps longer than the timeout we pass below.
    _install_fake_sdk(
        monkeypatch,
        lambda opts: _SlowReplyClient(sleep_seconds=2.0),
    )

    with pytest.raises(SubAgentError) as exc_info:
        await runner.delegate(
            agent_slug="slug",
            task="t",
            parent_conv_key="slack:dm:U123",
            timeout_seconds=1,
        )
    assert exc_info.value.kind == "timeout"

    events = _audit_events(tmp_path)
    fail = next(e for e in events if e["event"] == "audit.subagent.fail")
    assert fail["kind"] == "timeout"
    assert fail["timeout_seconds"] == 1

    # Transcript fail line was written.
    today = date.today().isoformat()
    transcript = tmp_path / "transcripts" / "subagent" / "slug" / f"{today}.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    directions = [r["direction"] for r in records]
    assert "fail" in directions
    fail_record = next(r for r in records if r["direction"] == "fail")
    assert fail_record["kind"] == "timeout"

    # Semaphore released so a follow-on call can proceed.
    assert runner._semaphore._value == 3  # noqa: SLF001


# ---------------------------------------------------------------------------
# Subtask 12: concurrency + depth-protection invariant
# ---------------------------------------------------------------------------


class _TimedHoldClient:
    """Fake SDK client that records acquire / release wall-clock times.

    The fake holds inside ``receive_response`` for ``hold_seconds`` after
    recording ``acquired_at`` (the moment the SDK lifecycle starts) and
    releases by recording ``released_at`` just before the final
    ResultMessage. The semaphore in ``SubAgentRunner.delegate`` is
    acquired before any SDK call, so the ``acquired_at`` timestamp on
    the fake is a faithful proxy for when the runner finally got a
    slot.
    """

    def __init__(self, *, hold_seconds: float, observations: list[dict[str, float]]) -> None:
        self._hold_seconds = hold_seconds
        self._observations = observations
        self._obs: dict[str, float] = {}

    async def __aenter__(self):
        self._obs["acquired_at"] = asyncio.get_event_loop().time()
        return self

    async def __aexit__(self, *_a):
        self._obs["released_at"] = asyncio.get_event_loop().time()
        self._observations.append(self._obs)
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self):
        await asyncio.sleep(self._hold_seconds)
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text="done")])
        yield _FakeResultMessage(total_cost_usd=0.0)


@pytest.mark.asyncio
async def test_delegate_concurrency_is_semaphore_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    """N+1 concurrent delegates: the extra call only starts once a slot frees.

    With ``max_concurrent=2`` and three concurrent delegates each
    holding the SDK lifecycle for ~0.4s, we expect the first two to run
    in parallel and the third to wait for one of them to release the
    semaphore before its own SDK lifecycle even starts. The
    ``_TimedHoldClient`` records ``acquired_at`` / ``released_at``
    around ``__aenter__`` / ``__aexit__`` so we can assert
    ``third.acquired_at >= min(first.released_at, second.released_at)``.
    """
    cfg = AnnaConfig.model_validate(
        {
            "subagents": {
                "max_concurrent": 2,
                # Generous acquire timeout so the third call definitely
                # gets in once a slot opens up. We do not want to race
                # the concurrency-acquire-timeout path here.
                "concurrency_acquire_timeout_seconds": 30,
            },
        }
    )
    cfg = cfg.model_copy(update={"anna_home": tmp_path})
    supervisor = Supervisor(config=cfg)
    runner = SubAgentRunner(
        config=cfg,
        supervisor=supervisor,
        agents_registry=SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=tmp_path / "agents",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
        skills_registry=SkillRegistry(
            supervisor=supervisor,
            skills_dir=tmp_path / "skills",
            audit_dir=tmp_path / "audit",
            fsync_on_write=False,
        ),
    )
    _write_persona(tmp_path, "slug")

    hold_seconds = 0.4
    observations: list[dict[str, float]] = []

    def _factory(_options):
        return _TimedHoldClient(
            hold_seconds=hold_seconds,
            observations=observations,
        )

    _install_fake_sdk(monkeypatch, _factory)

    # Fire three concurrent delegates and wait for all to complete.
    results = await asyncio.gather(
        runner.delegate(
            agent_slug="slug",
            task="t1",
            parent_conv_key="slack:dm:U123",
        ),
        runner.delegate(
            agent_slug="slug",
            task="t2",
            parent_conv_key="slack:dm:U123",
        ),
        runner.delegate(
            agent_slug="slug",
            task="t3",
            parent_conv_key="slack:dm:U123",
        ),
    )

    # All three completed cleanly.
    assert all(r.status == "ok" for r in results)
    assert len(observations) == 3
    # Sort by acquire time so we can talk about "the third" deterministically.
    observations.sort(key=lambda o: o["acquired_at"])
    first, second, third = observations
    # The first two ran in parallel: their acquire times are within a
    # small slack of each other (well under the hold duration).
    parallel_gap = abs(second["acquired_at"] - first["acquired_at"])
    assert parallel_gap < hold_seconds / 2, (
        f"first two acquires should be near-simultaneous; gap={parallel_gap:.3f}s"
    )
    # The third did NOT start until at least one of the first two
    # released its semaphore slot. We give a small slack for asyncio
    # scheduling jitter.
    earliest_release = min(first["released_at"], second["released_at"])
    assert third["acquired_at"] >= earliest_release - 0.05, (
        f"third delegate should wait for a slot; "
        f"third.acquired_at={third['acquired_at']:.3f}, "
        f"earliest_release={earliest_release:.3f}"
    )
    # And the third's acquire-time gap is comparable to the hold
    # duration, not zero.
    third_wait = third["acquired_at"] - first["acquired_at"]
    assert third_wait >= hold_seconds * 0.5, (
        f"third delegate should have waited ~{hold_seconds}s; "
        f"actual wait={third_wait:.3f}s"
    )


# ---------------------------------------------------------------------------
# Background delegation (async / detached)
# ---------------------------------------------------------------------------


class _GatedReplyClient(_FakeReplyClient):
    """Fake SDK client that blocks in receive_response until released.

    Lets a test observe that ``start_background`` returned a job id while
    the sub-agent run is still in flight, then release the gate so the
    run completes and the delivery callback fires.
    """

    def __init__(self, gate: asyncio.Event, *, reply: str = "bg done") -> None:
        super().__init__(reply=reply)
        self._gate = gate

    async def receive_response(self):
        await self._gate.wait()
        async for msg in super().receive_response():
            yield msg


@pytest.mark.asyncio
async def test_start_background_returns_job_id_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    """start_background returns a job id without waiting for the sub-agent."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")

    gate = asyncio.Event()
    _install_fake_sdk(monkeypatch, lambda opts: _GatedReplyClient(gate))

    delivered: list[tuple[str, str, str]] = []

    async def _delivery(transport: str, conv_key: str, text: str) -> None:
        delivered.append((transport, conv_key, text))

    runner.set_delivery(_delivery)

    job_id = runner.start_background(
        agent_slug="slug",
        task="t",
        parent_conv_key="slack:dm:U123",
        parent_transport="slack",
    )
    # A job id came back synchronously and the run is still gated (not yet
    # delivered).
    assert isinstance(job_id, str)
    assert job_id
    assert delivered == []
    assert len(runner._background_jobs) == 1  # noqa: SLF001

    # Release the gate and let the background task finish + deliver.
    gate.set()
    await runner.drain_background_jobs()

    assert len(delivered) == 1
    transport, conv_key, text = delivered[0]
    assert transport == "slack"
    assert conv_key == "slack:dm:U123"
    assert "bg done" in text
    assert job_id in text


@pytest.mark.asyncio
async def test_background_completion_delivers_to_origin_conv_key(
    tmp_path: Path, monkeypatch
) -> None:
    """The completion turn carries the originating conv_key + transport."""
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(
        monkeypatch, lambda opts: _FakeReplyClient(reply="research result")
    )

    delivered: list[tuple[str, str, str]] = []

    async def _delivery(transport: str, conv_key: str, text: str) -> None:
        delivered.append((transport, conv_key, text))

    runner.set_delivery(_delivery)

    runner.start_background(
        agent_slug="slug",
        task="dig",
        parent_conv_key="telegram:dm:99",
        parent_transport="telegram",
    )
    await runner.drain_background_jobs()

    assert len(delivered) == 1
    transport, conv_key, text = delivered[0]
    assert transport == "telegram"
    assert conv_key == "telegram:dm:99"
    assert "research result" in text
    # The YAML trailer rides along so ANNA reads a consistent surface.
    assert "delegation:" in text
    assert "status: ok" in text


@pytest.mark.asyncio
async def test_background_emits_start_and_complete_audit(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(monkeypatch, lambda opts: _FakeReplyClient(reply="r"))

    async def _delivery(transport: str, conv_key: str, text: str) -> None:
        return None

    runner.set_delivery(_delivery)
    job_id = runner.start_background(
        agent_slug="slug",
        task="t",
        parent_conv_key="slack:dm:U123",
        parent_transport="slack",
    )
    await runner.drain_background_jobs()

    events = _audit_events(tmp_path)
    names = [e["event"] for e in events]
    assert "audit.subagent.background_start" in names
    assert "audit.subagent.background_complete" in names
    start = next(
        e for e in events if e["event"] == "audit.subagent.background_start"
    )
    complete = next(
        e for e in events if e["event"] == "audit.subagent.background_complete"
    )
    assert start["job_id"] == job_id
    assert complete["job_id"] == job_id
    assert complete["status"] == "ok"


@pytest.mark.asyncio
async def test_background_failure_delivers_failure_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """A failing background job still delivers a turn (with the failure)."""
    runner = _make_runner(tmp_path)
    # No persona written → not_found inside delegate().
    _install_fake_sdk(monkeypatch, lambda opts: _FakeReplyClient())

    delivered: list[tuple[str, str, str]] = []

    async def _delivery(transport: str, conv_key: str, text: str) -> None:
        delivered.append((transport, conv_key, text))

    runner.set_delivery(_delivery)
    job_id = runner.start_background(
        agent_slug="ghost",
        task="t",
        parent_conv_key="slack:dm:U123",
        parent_transport="slack",
    )
    await runner.drain_background_jobs()

    assert len(delivered) == 1
    _, conv_key, text = delivered[0]
    assert conv_key == "slack:dm:U123"
    assert "failed" in text
    assert "not_found" in text
    assert job_id in text

    events = _audit_events(tmp_path)
    complete = next(
        e for e in events if e["event"] == "audit.subagent.background_complete"
    )
    assert complete["status"] == "not_found"


@pytest.mark.asyncio
async def test_background_raw_exception_delivers_failure_and_warns(
    tmp_path: Path, monkeypatch
) -> None:
    """A NON-SubAgentError raised mid-run still delivers a turn + warns.

    Steps that run after semaphore acquisition but are not wrapped as a
    SubAgentError (skill load, prompt build, options build, spawn audit,
    transcript write) can raise a raw exception. The broad ``except
    Exception`` branch in ``_run_background`` must still deliver a failure
    turn AND emit a WARNING-level ``background_complete`` audit so the job
    never silently vanishes from the operator's view. Here we patch a
    post-semaphore step (``_load_skills``) to raise ``RuntimeError``.
    """
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")
    _install_fake_sdk(monkeypatch, lambda opts: _FakeReplyClient())

    def _boom(_slug: str) -> list[str]:
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(runner, "_load_skills", _boom)

    delivered: list[tuple[str, str, str]] = []

    async def _delivery(transport: str, conv_key: str, text: str) -> None:
        delivered.append((transport, conv_key, text))

    runner.set_delivery(_delivery)
    job_id = runner.start_background(
        agent_slug="slug",
        task="t",
        parent_conv_key="slack:dm:U123",
        parent_transport="slack",
    )
    await runner.drain_background_jobs()

    # A failure turn still landed despite the raw (non-SubAgentError)
    # exception.
    assert len(delivered) == 1
    transport, conv_key, text = delivered[0]
    assert transport == "slack"
    assert conv_key == "slack:dm:U123"
    assert "failed" in text
    assert job_id in text
    assert "disk exploded" in text

    # A WARNING-level background_complete audit fired with the exception
    # type as the status.
    events = _audit_events(tmp_path)
    complete = next(
        e for e in events if e["event"] == "audit.subagent.background_complete"
    )
    assert complete["level"] == "WARNING"
    assert complete["status"] == "RuntimeError"
    assert complete["job_id"] == job_id


@pytest.mark.asyncio
async def test_drain_background_jobs_no_jobs_is_noop(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    # Should not raise with an empty job set.
    await runner.drain_background_jobs()


@pytest.mark.asyncio
async def test_drain_cancels_jobs_past_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    """A background job that never finishes is cancelled by the drain.

    Mirrors a SIGTERM landing while a sub-agent is still running: the
    drain waits up to its timeout, then cancels the dangling task so the
    loop teardown does not hang. The job set is empty afterward (no
    orphans).
    """
    runner = _make_runner(tmp_path)
    _write_persona(tmp_path, "slug")

    never = asyncio.Event()  # never set → receive_response blocks forever
    _install_fake_sdk(monkeypatch, lambda opts: _GatedReplyClient(never))

    delivered: list[tuple[str, str, str]] = []

    async def _delivery(transport: str, conv_key: str, text: str) -> None:
        delivered.append((transport, conv_key, text))

    runner.set_delivery(_delivery)
    runner.start_background(
        agent_slug="slug",
        task="t",
        parent_conv_key="slack:dm:U123",
        parent_transport="slack",
    )
    assert len(runner._background_jobs) == 1  # noqa: SLF001

    # Short drain timeout so the test does not block; the job is still
    # gated, so the drain falls through to the cancel branch.
    await runner.drain_background_jobs(timeout=0.2)

    assert runner._background_jobs == set()  # noqa: SLF001
    # The cancelled job never reached its delivery callback.
    assert delivered == []
