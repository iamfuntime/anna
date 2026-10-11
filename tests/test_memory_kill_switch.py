"""memory.enabled=false: no DB, no tools, no injection, no extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.memory import MemoryStore
from anna.runtime.memory_extraction import extraction_blocked_reason
from tests.test_memory_extraction import _event, _worker, sdk_fakes  # noqa: F401


def _db(w) -> Path:
    return w._config.anna_home / "state" / "memory.sqlite3"


def test_default_is_opt_in_so_main_style_configs_get_no_second_memory_store():
    cfg = AnnaConfig()
    assert cfg.memory.enabled is False
    assert cfg.memory.extraction.enabled is False  # extraction stays opt-in


def test_extraction_blocked_by_kill_switch_even_if_enabled():
    cfg = AnnaConfig()
    cfg.memory.enabled = True
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
    assert all("durable-memory" not in q for q in queries)


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


# --------------------------------------------------------------------------------------------
# A seeded fact makes the injection assertions meaningful (an empty store would pass either way)
# --------------------------------------------------------------------------------------------

async def _turn_queries(w, text="what coffee do I like?"):
    queries: list[str] = []
    orig = w._client.query

    async def spy(t, *a, **k):
        queries.append(t)
        return await orig(t, *a, **k)

    w._client.query = spy  # type: ignore[method-assign]
    await w._handle(_event(text=text, event_id="slack-9"))
    return queries


async def test_seeded_fact_is_injected_when_enabled_and_never_when_disabled(tmp_path, sdk_fakes):  # noqa: F811
    on = _worker(tmp_path / "on", [], enabled=False)
    owner, scope = on._memory_owner_scope()
    on._memory_store.remember(owner_id=owner, visibility_scope=scope, kind="preference", subject="coffee",
                              content="prefers espresso")
    assert any("<durable-memory>" in q and "espresso" in q for q in await _turn_queries(on))

    off = _worker(tmp_path / "off", [], enabled=False)
    seeded = MemoryStore(_db(off))                           # a DB that already holds the matching fact
    seeded.remember(owner_id=owner, visibility_scope=scope, kind="preference", subject="coffee",
                    content="prefers espresso")
    seeded.close()
    off._config.memory.enabled = False
    assert all("durable-memory" not in q and "espresso" not in q for q in await _turn_queries(off))
    assert off._memory_store_obj is None


# --------------------------------------------------------------------------------------------
# Dashboard: the flag is read per request from anna.yaml
# --------------------------------------------------------------------------------------------

def _dash(tmp_path, *, yaml_enabled: bool, startup_enabled: bool = True):
    from fastapi.testclient import TestClient

    from anna_web.app import create_app

    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "anna.yaml").write_text(f"memory:\n  enabled: {str(yaml_enabled).lower()}\n")
    cfg.memory.enabled = startup_enabled
    return TestClient(create_app(cfg), base_url="http://127.0.0.1:8765"), cfg


def _seed(tmp_path, text="SECRET-FACT-TEXT") -> Path:
    db = tmp_path / "state" / "memory.sqlite3"
    s = MemoryStore(db)
    s.remember(owner_id="seth", visibility_scope="private", kind="preference", subject="coffee", content=text)
    s.close()
    return db


def test_dashboard_page_shows_no_facts_and_says_disabled(tmp_path):
    _seed(tmp_path)
    client, _ = _dash(tmp_path, yaml_enabled=False, startup_enabled=False)
    body = client.get("/memory?owner=seth&scope=private").text
    assert "Memory is disabled" in body and "SECRET-FACT-TEXT" not in body and "/forget" not in body
    assert 'href="/memory"' not in body  # nav entry hidden (dashboard started with the flag off)


def test_flag_flipped_in_yaml_after_dashboard_start_applies_without_restart(tmp_path):
    db = _seed(tmp_path)
    client, cfg = _dash(tmp_path, yaml_enabled=True)
    assert "SECRET-FACT-TEXT" in client.get("/memory?owner=seth&scope=private").text
    (tmp_path / "anna.yaml").write_text("memory:\n  enabled: false\n")  # operator flips the kill switch
    assert cfg.memory.enabled is True                                      # the dashboard's start-up snapshot is stale
    body = client.get("/memory?owner=seth&scope=private").text
    assert "Memory is disabled" in body and "SECRET-FACT-TEXT" not in body
    r = client.post("/memory/1/forget", data={"owner": "seth", "scope": "private"}, follow_redirects=False)
    assert r.status_code == 303 and "flash=disabled" in r.headers["location"]
    r = client.post("/memory/1/correct", data={"owner": "seth", "scope": "private", "content": "x"},
                    follow_redirects=False)
    assert "flash=disabled" in r.headers["location"]
    store = MemoryStore(db)
    assert store.search(owner_id="seth", visibility_scopes={"private"}, query="coffee", limit=3)  # untouched
    store.close()


def test_flag_flipped_in_yaml_creates_no_db_from_a_write_route(tmp_path):
    client, _ = _dash(tmp_path, yaml_enabled=True)
    (tmp_path / "anna.yaml").write_text("memory:\n  enabled: false\n")
    r = client.post("/memory/1/forget", data={"owner": "o", "scope": "private"}, follow_redirects=False)
    assert r.status_code == 303
    assert not (tmp_path / "state" / "memory.sqlite3").exists()


def test_unreadable_yaml_fails_closed(tmp_path):
    _seed(tmp_path)
    client, _ = _dash(tmp_path, yaml_enabled=True)
    (tmp_path / "anna.yaml").write_text("memory: [unterminated\n")
    assert "SECRET-FACT-TEXT" not in client.get("/memory?owner=seth&scope=private").text


def test_health_reports_memory_disabled_without_opening_the_db(tmp_path):
    _seed(tmp_path)
    client, _ = _dash(tmp_path, yaml_enabled=True)
    (tmp_path / "anna.yaml").write_text("memory:\n  enabled: false\n")
    [mem] = [c for c in client.get("/health.json").json()["checks"] if c["id"] == "memory"]
    assert mem["status"] == "disabled" and "disabled by memory.enabled" in mem["summary"]
    assert "active fact" not in mem["summary"]


def test_doctor_reports_disabled_and_never_opens_the_database(tmp_path, monkeypatch):
    import anna.doctor as doctor

    _seed(tmp_path)
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", tmp_path); cfg.memory.enabled = False
    cfg.memory.extraction.enabled = True                   # would normally warn "enabled but NOT running"
    real = doctor.ReadOnlyDB

    class Guard(real):  # type: ignore[valid-type, misc]
        def __init__(self, path, *a, **k):
            assert "memory.sqlite3" not in str(path), "doctor opened the memory DB with memory.enabled=false"
            super().__init__(path, *a, **k)

    monkeypatch.setattr(doctor, "ReadOnlyDB", Guard)
    [mem] = [c for c in doctor.collect(cfg)["checks"] if c["id"] == "memory"]
    assert mem["status"] == "disabled" and "unaffected" in mem["summary"] and "NOT running" not in mem["summary"]
    assert "[ -- ] Memory: disabled" in doctor.render_plain(doctor.collect(cfg))


# --------------------------------------------------------------------------------------------
# anna-admin memory-import
# --------------------------------------------------------------------------------------------

def _import(tmp_path, monkeypatch, *flags, enabled=True):
    from click.testing import CliRunner

    from anna.cli.admin import main

    monkeypatch.setenv("ANNA_HOME", str(tmp_path))
    (tmp_path / "anna.yaml").write_text(f"memory:\n  enabled: {str(enabled).lower()}\n")
    md = tmp_path / "MEMORY.md"; md.write_text("- likes oolong tea\n")
    return CliRunner().invoke(main, ["memory-import", str(md), "--owner", "seth", "--scope", "private", *flags])


@pytest.mark.parametrize("flags", [(), ("--apply",)])
def test_import_with_memory_disabled_opens_and_creates_nothing(tmp_path, monkeypatch, flags):
    res = _import(tmp_path, monkeypatch, *flags, enabled=False)
    assert res.exit_code == 2 and "memory.enabled is false" in res.output
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").iterdir())


def test_import_dry_run_never_creates_or_changes_the_database(tmp_path, monkeypatch):
    res = _import(tmp_path, monkeypatch)                                  # no DB yet
    assert res.exit_code == 0 and "(dry-run): would_import=1" in res.output
    assert not (tmp_path / "state" / "memory.sqlite3").exists()
    db = _seed(tmp_path)
    before = db.read_bytes()
    res = _import(tmp_path, monkeypatch)
    assert res.exit_code == 0 and "would_import=1" in res.output
    assert db.read_bytes() == before and not list((tmp_path / "state").glob("*.bak-*"))


def test_import_force_overrides_the_kill_switch_for_a_deliberate_import(tmp_path, monkeypatch):
    res = _import(tmp_path, monkeypatch, "--apply", "--force", enabled=False)
    assert res.exit_code == 0 and "imported=1" in res.output
    assert (tmp_path / "state" / "memory.sqlite3").exists()


# --------------------------------------------------------------------------------------------
# queued extraction jobs: retained while disabled, expired (not extracted) after a long gap
# --------------------------------------------------------------------------------------------

def test_stale_queued_jobs_are_expired_not_extracted_after_reenable(tmp_path):
    from datetime import UTC, datetime, timedelta

    store = MemoryStore(tmp_path / "m.sqlite3")
    store.enqueue_extraction(owner_id="o", visibility_scope="private", conversation_key="c",
                             source_event_id="old", source_text="I prefer espresso over drip coffee.")
    store.enqueue_extraction(owner_id="o", visibility_scope="private", conversation_key="c",
                             source_event_id="new", source_text="I also like oolong tea very much.")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store._conn.execute("UPDATE extraction_jobs SET created_at=? WHERE source_event_id='old'", (old,)); store._conn.commit()
    job = store.claim_extraction(lease_seconds=300, max_attempts=3, max_age_seconds=72 * 3600)
    assert job.source_event_id == "new"
    row = store._conn.execute("SELECT status, source_text, last_error FROM extraction_jobs WHERE source_event_id='old'").fetchone()
    assert row["status"] == "dead" and row["source_text"] is None and "expired" in row["last_error"]
    store.close()


# ---- opt-in default: a main-compatible config (no ``memory:`` key) gets nothing -----------------

def test_absent_memory_key_in_yaml_loads_disabled(tmp_path, monkeypatch):
    from anna.config import load_config

    monkeypatch.setenv("ANNA_HOME", str(tmp_path))
    (tmp_path / "anna.yaml").write_text("transports:\n  slack:\n    enabled: true\n")
    assert load_config().memory.enabled is False
    (tmp_path / "anna.yaml").write_text("memory:\n  enabled: true\n")
    assert load_config().memory.enabled is True


async def test_default_config_worker_has_no_memory_db_tools_injection_or_extraction(tmp_path, sdk_fakes):  # noqa: F811
    sent: list = []
    w = _worker(tmp_path, sent, enabled=True)
    w._config.memory.enabled = AnnaConfig().memory.enabled  # the absent-config default
    memory_md = w._config.core_dir / "MEMORY.md"
    memory_md.parent.mkdir(parents=True, exist_ok=True)
    memory_md.write_text("- file memory stays authoritative\n")
    before = memory_md.read_bytes()
    options = w._build_options()
    assert "anna_memory" not in options.mcp_servers
    assert not any("anna_memory" in t for t in options.allowed_tools)
    queries: list[str] = []
    orig = w._client.query

    async def spy(text, *a, **k):
        queries.append(text)
        return await orig(text, *a, **k)

    w._client.query = spy  # type: ignore[method-assign]
    await w._handle(_event(event_id="slack-1"))
    assert [m.text for m in sent] == ["ok"]
    assert not _db(w).exists() and w._memory_store_obj is None
    assert all("durable-memory" not in q for q in queries)
    assert memory_md.read_bytes() == before


def test_explicit_true_enables_tools_and_db(tmp_path):
    w = _worker(tmp_path, [])  # helper sets memory.enabled = True explicitly
    assert w._config.memory.enabled is True
    assert "anna_memory" in w._build_options().mcp_servers
