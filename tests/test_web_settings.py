"""Tests for the ``/settings`` hub + editor nav wiring (MC-10).

Done conditions from the plan
(Inbox/2026-06-10-anna-web-mission-control-plan.md, subtask 10):

* ``GET /settings`` renders a hub of panels reaching all four tools:
  Config (``/config``), Secrets (``/env``), Schedules (``/schedules``),
  and Service (the restart control embedded inline).
* The restart control on /settings is the same ``restart_button.html``
  partial the Config pages embed — same ``hx-post="/restart"``, same
  confirm, same poller contract. No new mutating endpoints.
* The shell nav highlights Settings on the hub AND on the editor pages
  it absorbed (/config, /config/{section}, /env).
* The Integrations panel reflects ``cfg.integrations.obsidian`` state:
  enabled/disabled badges plus the configured paths.

Fixture strategy mirrors :mod:`tests.test_web_integrations`: copy
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
    vault_path: Path | None = None,
    tasknotes_path: Path | None = None,
) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_integrations.py — the
    # derived anna_home field is forced onto the tmp home.
    object.__setattr__(cfg, "anna_home", anna_home)
    cfg.integrations.obsidian.enabled = obsidian_enabled
    cfg.integrations.obsidian.tasknotes_enabled = tasknotes_enabled
    cfg.integrations.obsidian.vault_path = vault_path
    cfg.integrations.obsidian.tasknotes_path = tasknotes_path
    return cfg


def _client(anna_home: Path, **kwargs) -> TestClient:
    return TestClient(create_app(_make_cfg(anna_home, **kwargs)))


# ---------------------------------------------------------------------------
# Hub shell: 200 + all five panels + the four tool links.
# ---------------------------------------------------------------------------


def test_settings_200_with_all_panels(anna_home: Path) -> None:
    """GET /settings renders every hub panel and links all four tools."""
    response = _client(anna_home).get("/settings")

    assert response.status_code == 200
    body = response.text
    assert 'id="settings-grid"' in body
    for panel_id in (
        "panel-config",
        "panel-secrets",
        "panel-schedules-editor",
        "panel-service",
        "panel-integrations",
    ):
        assert f'id="{panel_id}"' in body, f"missing settings panel {panel_id}"

    # The four tools are reachable from the hub.
    assert 'href="/config"' in body
    assert 'href="/env"' in body
    assert 'href="/schedules"' in body
    assert 'href="/schedules/new"' in body
    # Integrations panel links its /config section editor.
    assert 'href="/config/integrations"' in body


def test_settings_nav_highlights_settings(anna_home: Path) -> None:
    """The shell nav marks Settings — and only Settings — as current."""
    body = _client(anna_home).get("/settings").text

    assert '<a href="/settings" aria-current="page">Settings</a>' in body
    assert body.count('aria-current="page"') == 1


# ---------------------------------------------------------------------------
# Service panel: the restart partial, byte-for-byte the existing contract.
# ---------------------------------------------------------------------------


def test_settings_includes_restart_control(anna_home: Path) -> None:
    """/settings embeds the idle restart partial exactly as /config does:
    same POST target, same confirm guard, same swap wiring."""
    body = _client(anna_home).get("/settings").text

    assert 'data-restart-partial="idle"' in body
    assert 'hx-post="/restart"' in body
    assert 'hx-swap="outerHTML"' in body
    # The confirm guard + button copy carry the pinned unit name via
    # the restart_unit Jinja global fallback.
    assert "Restart anna.service" in body
    assert "Active workers will be drained" in body


def test_settings_adds_no_mutating_routes(anna_home: Path) -> None:
    """The hub only links and embeds — it registers no mutating routes."""
    app = create_app(_make_cfg(anna_home))
    settings_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/settings")
    ]
    assert settings_routes, "expected /settings to be mounted"
    for route in settings_routes:
        methods = getattr(route, "methods", None) or set()
        assert set(methods) <= {"GET", "HEAD"}, (
            f"mutating method on settings route {route.path}: {methods}"
        )


# ---------------------------------------------------------------------------
# Config panel: the loaded badge off the gather_health idiom.
# ---------------------------------------------------------------------------


def test_settings_config_loaded_badge(anna_home: Path) -> None:
    """A real AnnaConfig on app.state renders the config badge as loaded/up.

    (The anna_running badge is environment-dependent — no dbus in CI —
    so only the config flag is pinned here; /healthz tests own the
    probe behavior.)
    """
    body = _client(anna_home).get("/settings").text

    assert 'data-state="up">loaded</span>' in body


# ---------------------------------------------------------------------------
# Integrations panel: cfg.integrations.obsidian state rendering.
# ---------------------------------------------------------------------------


def test_integrations_panel_disabled_by_default(anna_home: Path) -> None:
    """Vanilla deploy: both gates render disabled badges, paths unset."""
    body = _client(anna_home).get("/settings").text

    assert 'data-integration="obsidian">disabled<' in body
    assert 'data-integration="obsidian-tasknotes">disabled<' in body
    assert "not set" in body


def test_integrations_panel_enabled_with_paths(anna_home: Path) -> None:
    """Both flags on + paths set: enabled badges and the paths render."""
    body = _client(
        anna_home,
        obsidian_enabled=True,
        tasknotes_enabled=True,
        vault_path=Path("~/Obsidian/Brain"),
        tasknotes_path=Path("~/Obsidian/Brain/TaskNotes/Tasks"),
    ).get("/settings").text

    assert 'data-integration="obsidian">enabled<' in body
    assert 'data-integration="obsidian-tasknotes">enabled<' in body
    assert "~/Obsidian/Brain" in body
    assert "~/Obsidian/Brain/TaskNotes/Tasks" in body
    assert "not set" not in body


def test_integrations_panel_partial_flags_keep_tasknotes_disabled(
    anna_home: Path,
) -> None:
    """obsidian.enabled alone: the TaskNotes badge stays disabled (the
    board needs BOTH flags — mirrors the web_integrations gate)."""
    body = _client(anna_home, obsidian_enabled=True).get("/settings").text

    assert 'data-integration="obsidian">enabled<' in body
    assert 'data-integration="obsidian-tasknotes">disabled<' in body


# ---------------------------------------------------------------------------
# Editor pages absorbed by Settings highlight the Settings nav entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/config", "/config/web", "/env"])
def test_editor_pages_highlight_settings_nav(anna_home: Path, path: str) -> None:
    """The config/env editors render under the Settings nav highlight."""
    response = _client(anna_home).get(path)

    assert response.status_code == 200
    body = response.text
    assert '<a href="/settings" aria-current="page">Settings</a>' in body
    assert body.count('aria-current="page"') == 1
