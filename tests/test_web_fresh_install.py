"""Fresh-install smoke test for the mission-control dashboard (MC-11).

The graceful-degradation contract from the mission-control plan, taken
end-to-end: build the app against an ``anna_home`` that contains
nothing but ``anna.yaml`` — NO ``audit/``, NO ``transcripts/``, NO
``schedules.yaml`` — and walk every non-gated view. Each must answer
200 with no traceback in the body and render its documented empty
state; ``/tasks`` must 404 because the integration gate is off by
default.

The per-view empty-state details are owned by the per-subtask test
files (test_web_dashboard / _activity / _delegations /
_schedule_routes / _settings); this file is the one place that sweeps
the *whole* surface against the single shared fresh-install fixture,
so a regression in any view's fail-soft path fails here even if its
own test file's fixtures mask it.

Fixture strategy mirrors :mod:`tests.test_web_dashboard`: copy
``anna.yaml.example`` into a tmp home, force ``anna_home`` onto a
default :class:`AnnaConfig`, build via ``create_app``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.app import create_app

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"

# Every non-gated view the mission-control rebuild ships: full pages
# and their htmx partials. (path, is_partial) — partials must not ship
# the page chrome.
NON_GATED_VIEWS: list[tuple[str, bool]] = [
    ("/", False),
    ("/dashboard/panels/activity", True),
    ("/activity", False),
    ("/activity/feed", True),
    ("/schedules", False),
    ("/schedules/board", True),
    ("/delegations", False),
    ("/delegations/panels/data", True),
    ("/settings", False),
]


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Dir-less fake ``$ANNA_HOME`` — the fresh-install shape.

    Deliberately NO ``audit/``, NO ``transcripts/``, NO
    ``schedules.yaml``: only the config file a real first boot has.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


@pytest.fixture
def client(anna_home: Path) -> TestClient:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_dashboard.py — the derived
    # anna_home field is forced onto the tmp home so audit_dir /
    # subagent_transcript_dir / schedules.yaml all resolve under it.
    object.__setattr__(cfg, "anna_home", anna_home)
    return TestClient(create_app(cfg))


# ---------------------------------------------------------------------------
# The sweep: every non-gated view answers 200 with no traceback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "is_partial"), NON_GATED_VIEWS)
def test_view_serves_200_without_traceback(
    client: TestClient, path: str, is_partial: bool
) -> None:
    response = client.get(path)

    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert "Traceback" not in response.text, f"{path} rendered a traceback"
    if is_partial:
        assert "<html" not in response.text, f"{path} is a partial, got a full page"
    else:
        assert "<html" in response.text, f"{path} should be a full page"


def test_tasks_404s_under_default_config(client: TestClient) -> None:
    """The gated /tasks surface stays unmounted on a vanilla install."""
    assert client.get("/tasks").status_code == 404
    assert client.get("/tasks/board").status_code == 404


# ---------------------------------------------------------------------------
# Empty-state copy, where the view defines one.
# ---------------------------------------------------------------------------


def test_dashboard_renders_three_empty_panels(client: TestClient) -> None:
    """Activity / schedules / cost panels all degrade; service always renders."""
    body = client.get("/").text
    assert body.count("No data yet.") == 3
    assert 'id="anna-status"' in body
    assert 'id="config-status"' in body


def test_activity_page_and_partial_render_empty_state(client: TestClient) -> None:
    assert "No data yet." in client.get("/activity").text
    assert "No data yet." in client.get("/activity/feed").text


def test_schedule_board_renders_empty_row(client: TestClient) -> None:
    for path in ("/schedules", "/schedules/board"):
        assert "No schedules yet." in client.get(path).text, path


def test_delegations_render_four_empty_sections(client: TestClient) -> None:
    """Summary, model split, history, and daily strip all degrade."""
    for path in ("/delegations", "/delegations/panels/data"):
        assert client.get(path).text.count("No data yet.") == 4, path


def test_settings_hub_renders_all_panels(client: TestClient) -> None:
    """The hub has no reader-backed empty state; its panels always render."""
    body = client.get("/settings").text
    for panel_id in (
        "panel-config",
        "panel-secrets",
        "panel-schedules-editor",
        "panel-service",
        "panel-integrations",
    ):
        assert f'id="{panel_id}"' in body, f"missing settings panel {panel_id}"
