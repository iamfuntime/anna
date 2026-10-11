"""memory.enabled=false: no DB, no tools, no injection, no extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.memory_extraction import extraction_blocked_reason
from tests.test_memory_extraction import _event, _worker, sdk_fakes  # noqa: F401


def _db(w) -> Path:
    return w._config.anna_home / "state" / "memory.sqlite3"


def test_default_keeps_memory_enabled_for_compatibility():
    cfg = AnnaConfig()
    assert cfg.memory.enabled is True
    assert cfg.memory.extraction.enabled is False  # extraction stays opt-in


def test_extraction_blocked_by_kill_switch_even_if_enabled():
    cfg = AnnaConfig()
    cfg.memory.extraction.enabled = True
    assert extraction_blocked_reason(cfg) != "memory.enabled is false (memory kill switch)"
    cfg.memory.enabled = False
    assert extraction_blocked_reason(cfg) == "memory.enabled is false (memory kill switch)"


def test_enabled_worker_offers_memory_tools(tmp_path):
    w = _worker(tmp_path, [])
    options = w._build_options()
    assert "anna_memory" in options.mcp_servers
    assert any(t.startswith("mcp__anna_memory__") for t in options.allowed_tools)


def test_disabled_worker_exposes_no_memory_server_or_tools(tmp_path):
    w = _worker(tmp_path, [])
    w._config.memory.enabled = False
    options = w._build_options()
    assert "anna_memory" not in options.mcp_servers
    assert not any("anna_memory" in t for t in options.allowed_tools)
    assert not _db(w).exists() and w._memory_store_obj is None


async def test_disabled_turn_injects_nothing_enqueues_nothing_and_creates_no_db(tmp_path, sdk_fakes):  # noqa: F811
    sent: list = []
    w = _worker(tmp_path, sent, enabled=True)  # extraction.enabled=True is ignored when memory is off
    w._config.memory.enabled = False
    queries: list[str] = []
    orig = w._client.query

    async def spy(text, *a, **k):
        queries.append(text)
        return await orig(text, *a, **k)

    w._client.query = spy  # type: ignore[method-assign]
    await w._handle(_event(event_id="slack-1"))
    assert [m.text for m in sent] == ["ok"]
    assert not _db(w).exists()
    assert w._memory_store_obj is None
    assert all("memory" not in q.lower() or "espresso" in q.lower() for q in queries)


async def test_enabled_turn_still_enqueues(tmp_path, sdk_fakes):  # noqa: F811
    w = _worker(tmp_path, [], enabled=True)
    await w._handle(_event(event_id="slack-1"))
    assert _db(w).exists()
    assert w._memory_store.extraction_stats() == {"pending": 1}


def test_disabled_dashboard_write_routes_do_not_create_db(tmp_path):
    from fastapi.testclient import TestClient

    from anna_web.app import create_app

    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    (tmp_path / "anna.yaml").write_text((Path(__file__).parents[1] / "anna.yaml.example").read_text())
    cfg.memory.enabled = False
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8765")
    r = client.post("/memory/1/forget", data={"owner": "o", "scope": "private"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/memory/1/correct", data={"owner": "o", "scope": "private", "content": "x"}, follow_redirects=False)
    assert r.status_code == 303
    assert not (tmp_path / "state" / "memory.sqlite3").exists()
