"""Validate the self-edit MCP server's seven tools.

The tests bypass the MCP transport layer and call the tool handler closures
directly. The closures own the supervisor lock and audit-write side effects
that matter; the SDK wiring is exercised in test_worker_tool_wiring.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anna.agents.registry import SubAgentRegistry
from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.supervisor import Supervisor
from anna.skills.registry import SkillRegistry
from anna.tools.self_edit_server import SelfEditTools, build_self_edit_server


CONV_KEY = "slack:dm:UTEST"


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    return cfg


def _make_tools(cfg: AnnaConfig, *, with_schedule_store: bool = False) -> SelfEditTools:
    supervisor = Supervisor(config=cfg)
    agents_registry = SubAgentRegistry(
        supervisor=supervisor,
        agents_dir=cfg.anna_home / "agents",
        audit_dir=cfg.audit_dir,
        fsync_on_write=False,
    )
    skills_registry = SkillRegistry(
        supervisor=supervisor,
        skills_dir=cfg.anna_home / "skills",
        audit_dir=cfg.audit_dir,
        fsync_on_write=False,
    )
    schedule_store = None
    if with_schedule_store:
        # Override the scheduler state_path under the test cfg's anna_home so
        # the store does not write into the real ~/anna directory.
        cfg.scheduler.state_path = str(cfg.anna_home / "schedules.yaml")
        schedule_store = ScheduleStore(config=cfg, supervisor=supervisor)
    return SelfEditTools(
        config=cfg,
        supervisor=supervisor,
        agents_registry=agents_registry,
        skills_registry=skills_registry,
        schedule_store=schedule_store,
    )


def _read_audit_records(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Direct method coverage (no MCP transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_create_writes_file_and_emits_audit(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)

    result = await tools.subagent_create(
        slug="researcher",
        persona_text="You research things.",
        creator_conv=CONV_KEY,
    )
    assert "researcher" in result["content"][0]["text"]

    path = cfg.anna_home / "agents" / "researcher.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "You research things."

    audits = _read_audit_records(cfg.audit_dir)
    created = [a for a in audits if a["event"] == "audit.subagent.created"]
    assert created and created[0]["slug"] == "researcher"


@pytest.mark.asyncio
async def test_subagent_edit_emits_reason_audit(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)
    await tools.subagent_create(slug="x", persona_text="v1", creator_conv=CONV_KEY)

    await tools.subagent_edit(
        slug="x",
        persona_text="v2",
        creator_conv=CONV_KEY,
        edit_reason="clarified scope",
    )
    path = cfg.anna_home / "agents" / "x.md"
    assert path.read_text(encoding="utf-8") == "v2"

    audits = _read_audit_records(cfg.audit_dir)
    edited = [a for a in audits if a["event"] == "audit.subagent.edited"]
    reasons = [a for a in audits if a["event"] == "audit.subagent.edit_reason"]
    assert edited
    assert reasons and reasons[0]["edit_reason"] == "clarified scope"


@pytest.mark.asyncio
async def test_skill_create_and_edit(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)

    await tools.skill_create(
        agent="researcher",
        slug="cite-sources",
        skill_text="Always cite sources.",
        creator_conv=CONV_KEY,
        trigger="operator_request",
    )
    path = cfg.anna_home / "skills" / "researcher" / "cite-sources.md"
    assert path.is_file()

    await tools.skill_edit(
        agent="researcher",
        slug="cite-sources",
        skill_text="Always cite sources. Prefer primary.",
        creator_conv=CONV_KEY,
        iteration_notes_appended="added primary-source preference",
    )
    assert "primary" in path.read_text(encoding="utf-8")

    audits = _read_audit_records(cfg.audit_dir)
    assert any(a["event"] == "audit.skill.created" for a in audits)
    assert any(a["event"] == "audit.skill.edited" for a in audits)


@pytest.mark.asyncio
async def test_skill_create_rejects_unknown_trigger(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)
    with pytest.raises(ValueError):
        await tools.skill_create(
            agent="r",
            slug="s",
            skill_text="x",
            creator_conv=CONV_KEY,
            trigger="not_a_trigger",
        )


@pytest.mark.asyncio
async def test_agents_md_append_and_replace(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)

    await tools.agents_md_append_row(
        slug="researcher",
        description="Research helper",
        when_to_invoke="operator asks about a topic",
        conv_key=CONV_KEY,
    )
    path = cfg.core_dir / "AGENTS.md"
    text1 = path.read_text(encoding="utf-8")
    assert "- **researcher** — Research helper" in text1
    assert text1.count("**researcher**") == 1

    # Adding the same slug again should replace, not duplicate.
    await tools.agents_md_append_row(
        slug="researcher",
        description="Research helper, v2",
        when_to_invoke="operator asks about a deep topic",
        conv_key=CONV_KEY,
    )
    text2 = path.read_text(encoding="utf-8")
    assert text2.count("**researcher**") == 1
    assert "v2" in text2

    audits = _read_audit_records(cfg.audit_dir)
    written = [a for a in audits if a["event"] == "audit.agents_md.row_written"]
    assert len(written) == 2
    assert written[0]["replaced_existing"] is False
    assert written[1]["replaced_existing"] is True


@pytest.mark.asyncio
async def test_memory_md_dated_append(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)

    await tools.memory_md_append(entry="operator prefers black coffee", conv_key=CONV_KEY)
    await tools.memory_md_append(entry="operator's dog is named Pico", conv_key=CONV_KEY)

    path = cfg.core_dir / "MEMORY.md"
    text = path.read_text(encoding="utf-8")
    # Both entries land under one dated heading on the same day.
    assert text.count("## ") == 1
    assert "operator prefers black coffee" in text
    assert "Pico" in text

    audits = _read_audit_records(cfg.audit_dir)
    appends = [a for a in audits if a["event"] == "audit.memory_md.appended"]
    assert len(appends) == 2


@pytest.mark.asyncio
async def test_checkpoint_read_recent_returns_file_contents(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)

    # No checkpoints yet.
    empty = await tools.checkpoint_read_recent(conv_key=CONV_KEY, limit=2)
    assert "no checkpoints" in empty["content"][0]["text"]

    # Write two checkpoints directly to avoid the same-minute filename
    # collision that ``write_checkpoint`` is exposed to.
    safe_key = CONV_KEY.replace(":", "-")
    conv_dir = cfg.vault.resolved_path / "Conversations" / safe_key
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / "2026-05-30-1200.md").write_text(
        "# Checkpoint\n\nfirst session about the project layout\n",
        encoding="utf-8",
    )
    (conv_dir / "2026-05-31-0900.md").write_text(
        "# Checkpoint\n\nsecond session debugging a thing\n",
        encoding="utf-8",
    )

    out = await tools.checkpoint_read_recent(conv_key=CONV_KEY, limit=2)
    text = out["content"][0]["text"]
    assert "first session" in text
    assert "second session" in text
    # Oldest first: "first session" must appear before "second session".
    assert text.index("first session") < text.index("second session")


# ---------------------------------------------------------------------------
# Server build path (smoke)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_create_with_cron(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    out = await tools.schedule_create(
        id="morning-brief",
        prompt="Compose a morning brief.",
        destination_transport="slack",
        destination_channel="C0AFD2LM38R",
        cron="0 6 * * *",
        creator_conv=CONV_KEY,
    )
    assert "morning-brief" in out["content"][0]["text"]
    assert tools.schedule_store is not None
    assert tools.schedule_store.get("morning-brief") is not None
    audits = _read_audit_records(cfg.audit_dir)
    assert any(a.get("event") == "audit.schedule.created" for a in audits)


@pytest.mark.asyncio
async def test_schedule_create_with_natural_language(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    await tools.schedule_create(
        id="weekly-report",
        prompt="Write the weekly report.",
        destination_transport="slack",
        destination_channel="C07MLJPHX9V",
        natural_language="every Monday at 9am",
        creator_conv=CONV_KEY,
    )
    s = tools.schedule_store.get("weekly-report")  # type: ignore[union-attr]
    assert s is not None
    assert s.cron == "0 9 * * MON"
    assert s.natural_language == "every Monday at 9am"


@pytest.mark.asyncio
async def test_schedule_create_requires_exactly_one_of_cron_or_nl(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    with pytest.raises(ValueError, match=r"exactly one"):
        await tools.schedule_create(
            id="x",
            prompt="p",
            destination_transport="slack",
            destination_channel="C",
            creator_conv=CONV_KEY,
        )
    with pytest.raises(ValueError, match=r"exactly one"):
        await tools.schedule_create(
            id="x",
            prompt="p",
            destination_transport="slack",
            destination_channel="C",
            cron="0 6 * * *",
            natural_language="every morning at 6am",
            creator_conv=CONV_KEY,
        )


@pytest.mark.asyncio
async def test_schedule_create_without_store_raises(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=False)
    with pytest.raises(RuntimeError, match=r"scheduler is not configured"):
        await tools.schedule_create(
            id="x",
            prompt="p",
            destination_transport="slack",
            destination_channel="C",
            cron="0 6 * * *",
            creator_conv=CONV_KEY,
        )


@pytest.mark.asyncio
async def test_schedule_update_partial(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    await tools.schedule_create(
        id="m",
        prompt="p",
        destination_transport="slack",
        destination_channel="C1",
        cron="0 6 * * *",
        creator_conv=CONV_KEY,
    )
    await tools.schedule_update(
        id="m",
        creator_conv=CONV_KEY,
        enabled=False,
        timeout_seconds=600,
    )
    s = tools.schedule_store.get("m")  # type: ignore[union-attr]
    assert s.enabled is False
    assert s.timeout_seconds == 600


@pytest.mark.asyncio
async def test_schedule_update_no_changes_raises(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    await tools.schedule_create(
        id="m",
        prompt="p",
        destination_transport="slack",
        destination_channel="C1",
        cron="0 6 * * *",
        creator_conv=CONV_KEY,
    )
    with pytest.raises(ValueError, match=r"no fields"):
        await tools.schedule_update(id="m", creator_conv=CONV_KEY)


@pytest.mark.asyncio
async def test_schedule_delete(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    await tools.schedule_create(
        id="m",
        prompt="p",
        destination_transport="slack",
        destination_channel="C1",
        cron="0 6 * * *",
        creator_conv=CONV_KEY,
    )
    await tools.schedule_delete(id="m", creator_conv=CONV_KEY)
    assert tools.schedule_store.get("m") is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_schedule_list_empty_and_populated(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg, with_schedule_store=True)
    empty = await tools.schedule_list()
    assert "no schedules" in empty["content"][0]["text"]
    await tools.schedule_create(
        id="m",
        prompt="p",
        destination_transport="slack",
        destination_channel="C1",
        cron="0 6 * * *",
        creator_conv=CONV_KEY,
    )
    populated = await tools.schedule_list()
    assert "m" in populated["content"][0]["text"]
    assert "ENABLED" in populated["content"][0]["text"]


def test_build_self_edit_server_exposes_all_tools(tmp_path: Path) -> None:
    from anna.tools.self_edit_server import SELF_EDIT_TOOL_NAMES

    cfg = _make_config(tmp_path)
    tools = _make_tools(cfg)
    server = build_self_edit_server(tools=tools, conv_key=CONV_KEY)

    # The SDK returns an McpSdkServerConfig dict whose "instance" key is an
    # mcp.server.lowlevel.server.Server. The Server exposes registered tools
    # through a request handler dict; we walk it to confirm every tool we
    # registered shows up.
    assert isinstance(server, dict) and server.get("type") == "sdk"
    assert server.get("name") == "anna_self_edit"
    instance = server["instance"]

    # Pull the tool names out via the public list_tools handler the SDK
    # registered on the underlying mcp Server.
    import asyncio
    import mcp.types as mcp_types

    async def _list() -> list[str]:
        list_handler = instance.request_handlers.get(mcp_types.ListToolsRequest)
        assert list_handler is not None
        req = mcp_types.ListToolsRequest(method="tools/list")
        result = await list_handler(req)
        # result is a ServerResult wrapping ListToolsResult
        tools_payload = result.root if hasattr(result, "root") else result
        return [t.name for t in tools_payload.tools]

    names = asyncio.get_event_loop().run_until_complete(_list()) if False else asyncio.run(_list())
    for name in SELF_EDIT_TOOL_NAMES:
        assert name in names, f"server missing tool {name}; got {names}"
