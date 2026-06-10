"""Tests for the mission-control dashboard landing (MC-02).

Done conditions from the plan
(Inbox/2026-06-10-anna-web-mission-control-plan.md, subtask 2):

* The shell nav renders every non-gated section (Dashboard / Activity
  / Schedules / Delegations / Settings) with active-page highlighting,
  and NO Tasks entry under default config (the integrations gate).
* ``GET /`` renders the panel grid: service status, recent-activity
  head, schedule health, today's delegation cost.
* On a dir-less fixture (fresh install: no ``audit/``, no
  ``transcripts/``, no ``schedules.yaml``) every data panel degrades
  to the ``_panel_empty.html`` "No data yet." state — zero 500s.
* Panels render real data when the on-disk streams exist (audit JSONL,
  schedules.yaml, subagent transcript trailers).

Fixture strategy mirrors :mod:`tests.test_web_integrations`: copy
``anna.yaml.example`` into a tmp home, build a fresh app via
``create_app``, exercise through :class:`fastapi.testclient.TestClient`.
Seed-file shapes are copied from the writer-side conventions the
reader tests pin (:mod:`tests.test_web_audit_reader`,
:mod:`tests.test_web_schedule_routes`).
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.app import create_app

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` — deliberately dir-less.

    No ``audit/``, no ``transcripts/``, no ``schedules.yaml``: the
    fresh-install shape every panel must degrade gracefully on.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


def _make_cfg(anna_home: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_integrations.py — the
    # derived anna_home field is forced onto the tmp home so audit_dir
    # / subagent_transcript_dir / schedules.yaml all resolve under it.
    object.__setattr__(cfg, "anna_home", anna_home)
    return cfg


def _client(anna_home: Path) -> TestClient:
    return TestClient(create_app(_make_cfg(anna_home)))


# ---------------------------------------------------------------------------
# Seed helpers — writer-shaped fixture files.
# ---------------------------------------------------------------------------


def _seed_audit_events(anna_home: Path, events: list[dict[str, Any]]) -> None:
    """Write today's audit JSONL the way anna.log.audit_event does.

    AuditReader windows on UTC-dated filenames, so the day-file is
    named with the current UTC date (the route passes no ``now``).
    """
    audit_dir = anna_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    (audit_dir / f"audit-{today.isoformat()}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )


def _audit_record(event: str, ts: str, **fields: Any) -> dict[str, Any]:
    return {
        "ts": ts,
        "level": "INFO",
        "event": event,
        "actor": "anna",
        "conv_key": None,
        **fields,
    }


def _seed_schedules_yaml(anna_home: Path, records: list[dict[str, Any]]) -> None:
    """Write a v1 schedules.yaml (shape per tests/test_web_schedule_routes)."""
    (anna_home / "schedules.yaml").write_text(
        yaml.safe_dump({"version": 1, "schedules": records}, sort_keys=False),
        encoding="utf-8",
    )


def _schedule_record(
    *,
    id: str,
    enabled: bool = True,
    consecutive_failures: int = 0,
) -> dict[str, Any]:
    return {
        "id": id,
        "natural_language": None,
        "cron": "0 6 * * *",
        "timezone": "UTC",
        "prompt": "Compose a morning brief.",
        "destination": {"transport": "slack", "channel": "C0AFD2LM38R"},
        "timeout_seconds": 300,
        "enabled": enabled,
        "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        "state": {
            "last_fired_at": None,
            "last_status": "fail" if consecutive_failures else None,
            "consecutive_failures": consecutive_failures,
        },
    }


def _seed_delegation_trailer(
    cfg: AnnaConfig, *, slug: str = "researcher", cost_usd: float = 1.25
) -> None:
    """Write one outbound transcript trailer under today's day-file.

    DelegationReader windows on the *local* ``date.today()`` (its
    ``daily_rollup`` anchor), so the day-file uses the local date.
    """
    slug_dir = cfg.subagent_transcript_dir / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": "outbound",
        "conv_key": f"subagent:{slug}",
        "text": "done",
        "audit_id": "11111111-2222-3333-4444-555555555555",
        "cost_usd": cost_usd,
        "duration_seconds": 12.5,
        "tool_calls": ["Read", "Write"],
        "model": "claude-fable-5",
    }
    (slug_dir / f"{date.today().isoformat()}.jsonl").write_text(
        json.dumps(line) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Shell: panel grid + nav.
# ---------------------------------------------------------------------------


def test_dashboard_200_with_panel_grid(anna_home: Path) -> None:
    """GET / renders the panel grid with all four MC-02 panels."""
    response = _client(anna_home).get("/")

    assert response.status_code == 200
    body = response.text
    assert 'id="dashboard-grid"' in body
    for panel_id in (
        "panel-service",
        "panel-activity",
        "panel-schedules",
        "panel-cost",
    ):
        assert f'id="{panel_id}"' in body, f"missing dashboard panel {panel_id}"
    # The activity head live-refreshes via htmx, per the plan's
    # polling-not-SSE decision.
    assert 'hx-get="/dashboard/panels/activity"' in body
    assert 'hx-trigger="every 5s"' in body


def test_nav_renders_non_gated_sections_with_active_highlight(
    anna_home: Path,
) -> None:
    """All five non-gated nav targets render; Dashboard carries
    aria-current on its own page."""
    body = _client(anna_home).get("/").text

    for href, label in (
        ("/", "Dashboard"),
        ("/activity", "Activity"),
        ("/schedules", "Schedules"),
        ("/delegations", "Delegations"),
        ("/settings", "Settings"),
    ):
        assert f'href="{href}"' in body, f"nav target {label} missing"
        assert f">{label}</a>" in body
    # Active-page highlighting: only the Dashboard link is current.
    assert '<a href="/" aria-current="page">Dashboard</a>' in body
    assert body.count('aria-current="page"') == 1
    # The MC-01 theme toggle survives the shell rework.
    assert 'data-action="toggle-theme"' in body


def test_tasks_nav_absent_with_default_config(anna_home: Path) -> None:
    """Default config fails the integrations gate: no Tasks entry."""
    body = _client(anna_home).get("/").text

    assert 'href="/tasks"' not in body
    assert ">Tasks<" not in body


# ---------------------------------------------------------------------------
# Empty states — the dir-less fresh-install fixture.
# ---------------------------------------------------------------------------


def test_every_data_panel_degrades_to_no_data_yet(anna_home: Path) -> None:
    """Dir-less home: activity, schedule, and cost panels all render the
    shared _panel_empty.html state (service status always renders its
    badges — /healthz never returns "nothing")."""
    response = _client(anna_home).get("/")

    assert response.status_code == 200
    assert response.text.count("No data yet.") == 3
    # The service panel still seeds badges from gather_health.
    assert 'id="anna-status"' in response.text
    assert 'id="config-status"' in response.text


def test_activity_partial_empty_on_dirless_home(anna_home: Path) -> None:
    response = _client(anna_home).get("/dashboard/panels/activity")

    assert response.status_code == 200
    assert "No data yet." in response.text
    # It's a partial, not a full page.
    assert "<html" not in response.text


# ---------------------------------------------------------------------------
# Data-bearing panels.
# ---------------------------------------------------------------------------


def test_activity_panel_lists_recent_audit_events(anna_home: Path) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.subagent.spawn", f"{today}T10:00:00.000Z", slug="researcher"
            ),
            _audit_record(
                "audit.subagent.complete",
                f"{today}T10:05:00.000Z",
                slug="researcher",
                cost_usd=0.42,
            ),
        ],
    )

    body = _client(anna_home).get("/").text

    assert "audit.subagent.complete" in body
    assert "audit.subagent.spawn" in body
    assert "researcher" in body
    # Only the schedule + cost panels are still empty.
    assert body.count("No data yet.") == 2


def test_activity_partial_serves_feed_rows(anna_home: Path) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    _seed_audit_events(
        anna_home,
        [_audit_record("audit.schedule.fire", f"{today}T07:00:00.000Z", schedule_id="brief")],
    )

    response = _client(anna_home).get("/dashboard/panels/activity")

    assert response.status_code == 200
    assert 'class="feed-row"' in response.text
    assert "audit.schedule.fire" in response.text
    assert "brief" in response.text


def test_schedule_panel_summarizes_counts_and_failures(anna_home: Path) -> None:
    _seed_schedules_yaml(
        anna_home,
        [
            _schedule_record(id="morning-brief"),
            _schedule_record(
                id="flaky-roundup", enabled=False, consecutive_failures=2
            ),
        ],
    )

    body = _client(anna_home).get("/").text

    assert 'data-total="2"' in body
    assert "(1 enabled)" in body
    assert "1 failing" in body
    # The failing schedule is named, with its failure count.
    assert "flaky-roundup" in body
    assert "2 consecutive failures" in body


def test_schedule_panel_all_healthy(anna_home: Path) -> None:
    _seed_schedules_yaml(anna_home, [_schedule_record(id="morning-brief")])

    body = _client(anna_home).get("/").text

    assert 'data-total="1"' in body
    assert "no failures" in body


def test_cost_panel_renders_todays_rollup(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home)
    _seed_delegation_trailer(cfg, slug="researcher", cost_usd=1.25)

    body = TestClient(create_app(cfg)).get("/").text

    assert "$1.25" in body
    assert "across 1 run" in body
    # Activity + schedule panels remain empty; the cost panel does not.
    assert body.count("No data yet.") == 2
