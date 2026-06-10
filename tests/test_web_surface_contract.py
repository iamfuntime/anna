"""Static surface contract for the mission-control dashboard (MC-11).

The mission-control rebuild's standing rule: *observability never
mutates*. Every view added by the rebuild (dashboard, activity feed,
schedule run board, delegations, gated tasks board, settings hub, and
all of their htmx partials) is GET-only; the only mutating endpoints in
the whole app are the pre-existing Phase 2.5 editors (config / env /
schedules CRUD) and the restart button.

These tests introspect the built app's route table directly — no HTTP,
no fixtures beyond a tmp anna_home — and pin:

1. The EXACT set of mutating ``(method, path)`` pairs. A future subtask
   that adds a POST/PUT/PATCH/DELETE anywhere fails loudly here and has
   to update ``ALLOWED_MUTATING_ROUTES`` deliberately (and answer for
   it in review) rather than slipping a write path into an
   observability surface.
2. That the gated ``/tasks`` surface adds zero mutating routes even
   when its integration gate is enabled.
3. That every observability path is registered (nav-reachability at
   the route layer) and answers only GET/HEAD.

Fixture strategy mirrors :mod:`tests.test_web_integrations`: copy
``anna.yaml.example`` into a tmp home and build the app via
``create_app``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI

from anna.config import AnnaConfig
from anna_web.app import create_app

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# The complete, deliberately-pinned mutating surface of the dashboard.
# Everything here predates the mission-control rebuild: the schema-driven
# config editor (subtask 7), the masked .env editor (subtask 8), the
# schedule CRUD editor (subtask 9), and the restart button (subtask 10).
# The mission-control views (MC-02..MC-10) contribute NOTHING to this
# set — if a route you added shows up in the diff against this constant,
# either it does not belong on an observability surface or this contract
# needs a reviewed, intentional update.
ALLOWED_MUTATING_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/config/{section}"),
        ("POST", "/env"),
        ("DELETE", "/env/{key}"),
        ("POST", "/schedules"),
        ("POST", "/schedules/"),
        ("PUT", "/schedules/{schedule_id}"),
        ("DELETE", "/schedules/{schedule_id}"),
        ("POST", "/restart"),
    }
)

# Every observability path the mission-control rebuild ships: full pages
# and their htmx partials, plus the healthz probe. /tasks and
# /tasks/board are config-gated (present only with the obsidian
# tasknotes gate on), which the tests below account for.
OBSERVABILITY_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/dashboard/panels/activity",
        "/activity",
        "/activity/feed",
        "/schedules",
        "/schedules/board",
        "/delegations",
        "/delegations/panels/data",
        "/settings",
        "/healthz",
        "/tasks",
        "/tasks/board",
    }
)

GATED_PATHS: frozenset[str] = frozenset({"/tasks", "/tasks/board"})


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` with a fresh copy of anna.yaml.example."""
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


def _make_cfg(anna_home: Path, *, tasks_enabled: bool = False) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_integrations.py — the
    # derived anna_home field is forced onto the tmp home.
    object.__setattr__(cfg, "anna_home", anna_home)
    if tasks_enabled:
        cfg.integrations.obsidian.enabled = True
        cfg.integrations.obsidian.tasknotes_enabled = True
    return cfg


def _method_path_pairs(app: FastAPI) -> set[tuple[str, str]]:
    """Every ``(method, path)`` pair in the app's route table.

    Mounts (the /static files app) expose no ``methods`` and are
    skipped; Starlette's auto-added HEAD/OPTIONS ride along and are
    harmless to the assertions below.
    """
    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        for method in methods:
            pairs.add((method.upper(), path))
    return pairs


def _mutating_pairs(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, path)
        for method, path in _method_path_pairs(app)
        if method in MUTATING_METHODS
    }


# ---------------------------------------------------------------------------
# The pinned mutating set
# ---------------------------------------------------------------------------


def test_mutating_routes_exactly_match_the_pinned_editor_set(
    anna_home: Path,
) -> None:
    """Default config: the mutating surface is the editor set, verbatim.

    Exact set equality on purpose — a missing route (an editor
    regressed) fails just as loudly as an extra one (a write path
    snuck into an observability surface).
    """
    app = create_app(_make_cfg(anna_home))
    assert _mutating_pairs(app) == set(ALLOWED_MUTATING_ROUTES)


def test_enabling_tasks_gate_adds_no_mutating_routes(anna_home: Path) -> None:
    """The gated /tasks surface is read-only: same pinned set with it on."""
    app = create_app(_make_cfg(anna_home, tasks_enabled=True))
    assert _mutating_pairs(app) == set(ALLOWED_MUTATING_ROUTES)


# ---------------------------------------------------------------------------
# Observability paths: registered, and GET/HEAD only
# ---------------------------------------------------------------------------


def test_every_observability_path_is_registered(anna_home: Path) -> None:
    """With the tasks gate on, every mission-control path is in the table."""
    app = create_app(_make_cfg(anna_home, tasks_enabled=True))
    registered = {path for _method, path in _method_path_pairs(app)}
    missing = OBSERVABILITY_PATHS - registered
    assert not missing, f"observability paths not mounted: {sorted(missing)}"


def test_gated_paths_absent_under_default_config(anna_home: Path) -> None:
    """Defaults keep /tasks unmounted entirely (MC-08's gate, re-pinned)."""
    app = create_app(_make_cfg(anna_home))
    registered = {path for _method, path in _method_path_pairs(app)}
    assert not (GATED_PATHS & registered)
    # ...and the rest of the observability surface is still all there.
    missing = (OBSERVABILITY_PATHS - GATED_PATHS) - registered
    assert not missing, f"non-gated paths not mounted: {sorted(missing)}"


def test_observability_paths_answer_only_get_and_head(anna_home: Path) -> None:
    """No observability path registers a mutating (or OPTIONS) handler."""
    app = create_app(_make_cfg(anna_home, tasks_enabled=True))
    offenders = {
        (method, path)
        for method, path in _method_path_pairs(app)
        if path in OBSERVABILITY_PATHS and method not in {"GET", "HEAD"}
    }
    assert not offenders, f"non-GET methods on observability paths: {sorted(offenders)}"
