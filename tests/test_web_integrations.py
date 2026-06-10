"""Tests for the optional-integration registry + gating (mission-control subtask 8).

Done condition from the plan (Inbox/2026-06-10-anna-web-mission-control-plan.md):

* With default config there is NO Tasks nav entry and ``/tasks``
  returns 404.
* Flipping ``integrations.obsidian.enabled`` + ``tasknotes_enabled``
  surfaces both.
* The schema-driven Settings config editor renders the new
  ``integrations`` block with no editor-specific code.

Fixture strategy mirrors :mod:`tests.test_web_config_routes`: copy
``anna.yaml.example`` into a tmp home, build a fresh app via
``create_app`` with the integration flags under test, and exercise it
through :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web import integrations as web_integrations
from anna_web.app import create_app

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` with a fresh copy of anna.yaml.example."""
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


def _make_cfg(
    anna_home: Path,
    *,
    obsidian_enabled: bool = False,
    tasknotes_enabled: bool = False,
) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_config_routes.py — the
    # derived anna_home field is forced onto the tmp home.
    object.__setattr__(cfg, "anna_home", anna_home)
    cfg.integrations.obsidian.enabled = obsidian_enabled
    cfg.integrations.obsidian.tasknotes_enabled = tasknotes_enabled
    return cfg


def _client(anna_home: Path, **flags: bool) -> TestClient:
    return TestClient(create_app(_make_cfg(anna_home, **flags)))


# ---------------------------------------------------------------------------
# Registry level
# ---------------------------------------------------------------------------


def test_registry_all_disabled_by_default() -> None:
    """Default config satisfies no gate: no integrations, nav, or routers."""
    cfg = AnnaConfig()
    assert web_integrations.enabled_integrations(cfg) == []
    assert web_integrations.nav_entries(cfg) == []
    assert web_integrations.routers(cfg) == []


@pytest.mark.parametrize(
    ("obsidian_enabled", "tasknotes_enabled", "expected"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, True),
    ],
)
def test_obsidian_tasknotes_gate_requires_both_flags(
    obsidian_enabled: bool, tasknotes_enabled: bool, expected: bool
) -> None:
    cfg = AnnaConfig()
    cfg.integrations.obsidian.enabled = obsidian_enabled
    cfg.integrations.obsidian.tasknotes_enabled = tasknotes_enabled
    assert (
        web_integrations.is_enabled(cfg, web_integrations.OBSIDIAN_TASKNOTES)
        is expected
    )


def test_is_enabled_unknown_name_is_disabled_not_error() -> None:
    cfg = AnnaConfig()
    assert web_integrations.is_enabled(cfg, "no_such_integration") is False


def test_crashing_gate_counts_as_disabled() -> None:
    """A raising is_enabled probe degrades to disabled (fail-soft contract)."""

    def _boom(cfg: AnnaConfig) -> bool:
        raise RuntimeError("gate probe crashed")

    integration = web_integrations.Integration(name="boom", is_enabled=_boom)
    assert web_integrations._gate(integration, AnnaConfig()) is False


def test_enabled_gate_yields_nav_and_router() -> None:
    """The enabled Obsidian/TaskNotes registration exposes Tasks nav + router."""
    cfg = AnnaConfig()
    cfg.integrations.obsidian.enabled = True
    cfg.integrations.obsidian.tasknotes_enabled = True

    entries = web_integrations.nav_entries(cfg)
    assert entries == [web_integrations.NavEntry(label="Tasks", href="/tasks")]

    routers = web_integrations.routers(cfg)
    assert len(routers) == 1
    assert routers[0].prefix == "/tasks"


# ---------------------------------------------------------------------------
# App level — the plan's done condition
# ---------------------------------------------------------------------------


def test_default_config_hides_tasks_nav_and_route_404s(anna_home: Path) -> None:
    """Vanilla deploy: no Tasks nav entry anywhere, /tasks → 404."""
    client = _client(anna_home)

    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/tasks"' not in home.text
    assert ">Tasks<" not in home.text

    assert client.get("/tasks").status_code == 404


def test_partial_flags_keep_tasks_hidden(anna_home: Path) -> None:
    """obsidian.enabled alone is not enough — the sub-gate must also flip."""
    client = _client(anna_home, obsidian_enabled=True)

    assert 'href="/tasks"' not in client.get("/").text
    assert client.get("/tasks").status_code == 404


def test_flipped_flags_surface_tasks_nav_and_route(anna_home: Path) -> None:
    """Both flags on (+ app rebuild, the test analogue of a restart):
    the Tasks nav entry renders and /tasks serves the board."""
    client = _client(anna_home, obsidian_enabled=True, tasknotes_enabled=True)

    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/tasks"' in home.text
    assert ">Tasks<" in home.text

    board = client.get("/tasks")
    assert board.status_code == 200
    assert 'id="tasks-board"' in board.text
    # Subtask 9 lands the reader; until then (and on any fresh install
    # after) the board renders the fail-soft empty state.
    assert "No task data yet" in board.text


def test_tasks_surface_adds_no_mutating_routes(anna_home: Path) -> None:
    """The gated surface is GET-only — observability views never mutate."""
    app = create_app(
        _make_cfg(anna_home, obsidian_enabled=True, tasknotes_enabled=True)
    )
    tasks_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/tasks")
    ]
    assert tasks_routes, "expected /tasks to be mounted when both flags are on"
    for route in tasks_routes:
        methods = getattr(route, "methods", None) or set()
        assert set(methods) <= {"GET", "HEAD"}, (
            f"mutating method on gated route {route.path}: {methods}"
        )


# ---------------------------------------------------------------------------
# Schema-driven config editor picks the block up with zero editor code
# ---------------------------------------------------------------------------


def test_config_index_lists_integrations_section(anna_home: Path) -> None:
    """GET /config grows an integrations card purely from the pydantic schema."""
    client = _client(anna_home)
    response = client.get("/config")
    assert response.status_code == 200
    assert 'href="/config/integrations"' in response.text


def test_config_editor_renders_integrations_block_generically(
    anna_home: Path,
) -> None:
    """GET /config/integrations renders every field via the generic
    schema walker — the dotted-path input names prove no
    integrations-specific editor code exists (or is needed)."""
    client = _client(anna_home)
    response = client.get("/config/integrations")
    assert response.status_code == 200
    body = response.text
    for path in (
        "integrations.obsidian.enabled",
        "integrations.obsidian.vault_path",
        "integrations.obsidian.tasknotes_enabled",
        "integrations.obsidian.tasknotes_path",
    ):
        assert f'name="{path}"' in body, f"input {path!r} missing from editor"
