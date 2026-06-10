"""Tests for the full activity feed view (MC-05).

Done conditions from the plan
(Inbox/2026-06-10-anna-web-mission-control-plan.md, subtask 5):

* ``GET /activity`` renders the full-page feed with the Activity nav
  entry highlighted and the four kind-filter buttons
  (All / Sub-agents / Schedules / Dashboard).
* ``GET /activity/feed`` is the self-polling htmx partial: it serves
  feed rows from the seeded audit JSONL, newest first, with a
  group-break line when the date changes.
* ``?kind=`` filtering maps onto AuditReader's event-prefix filter;
  unknown kinds normalize to ``all``.
* fail/timeout/error events carry the visibly-distinct fail class;
  completes ok; spawns/fires idle.
* On a dir-less fixture (fresh install: no ``audit/``) both the page
  and the partial degrade to the shared ``_panel_empty.html`` state.

Fixture strategy mirrors :mod:`tests.test_web_dashboard`: copy
``anna.yaml.example`` into a tmp home, build a fresh app via
``create_app``, exercise through :class:`fastapi.testclient.TestClient`.
Seed-file shapes are copied from the writer-side conventions the
reader tests pin (:mod:`tests.test_web_audit_reader`).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.app import create_app

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` — deliberately dir-less.

    No ``audit/``: the fresh-install shape the feed must degrade
    gracefully on.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


def _make_cfg(anna_home: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_dashboard.py — the
    # derived anna_home field is forced onto the tmp home so audit_dir
    # resolves under it.
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
    named with the current UTC date (the routes pass no ``now``).
    Events are written oldest-first (append order); the reader serves
    them newest-first.
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


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _yesterday() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def _seed_mixed_kinds(anna_home: Path) -> None:
    """One event per filterable family, all stamped today."""
    today = _today()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.subagent.spawn",
                f"{today}T10:00:00.000Z",
                slug="researcher",
                model="claude-fable-5",
            ),
            _audit_record(
                "audit.schedule.fire",
                f"{today}T10:01:00.000Z",
                schedule_id="morning-brief",
            ),
            _audit_record(
                "audit.web.dashboard.config_write",
                f"{today}T10:02:00.000Z",
                actor="operator",
                section="web",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Full page: 200 + nav highlight + filter buttons + poll wiring.
# ---------------------------------------------------------------------------


def test_activity_page_200_with_nav_highlight(anna_home: Path) -> None:
    response = _client(anna_home).get("/activity")

    assert response.status_code == 200
    body = response.text
    # Active-page highlighting: only the Activity nav link is current.
    assert '<a href="/activity" aria-current="page">Activity</a>' in body
    assert body.count('aria-current="page"') == 1


def test_activity_page_renders_filter_buttons(anna_home: Path) -> None:
    body = _client(anna_home).get("/activity").text

    for kind, label in (
        ("all", "All"),
        ("subagents", "Sub-agents"),
        ("schedules", "Schedules"),
        ("dashboard", "Dashboard"),
    ):
        assert f'hx-get="/activity/feed?kind={kind}"' in body, f"missing {label} filter"
        assert f">{label}</a>" in body
        # No-JS / deep-link fallback.
        assert f'href="/activity?kind={kind}"' in body
    # The default filter is active.
    assert body.count("filter-active") == 1


def test_activity_page_polls_feed_partial(anna_home: Path) -> None:
    """The feed container self-polls the partial with its kind baked in."""
    body = _client(anna_home).get("/activity").text

    assert 'id="activity-feed"' in body
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-swap="outerHTML"' in body
    assert 'hx-get="/activity/feed?kind=all"' in body


def test_activity_page_filter_param_selects_active_button(anna_home: Path) -> None:
    body = _client(anna_home).get("/activity", params={"kind": "schedules"}).text

    # The poll re-fetches the filtered partial, and Schedules is active.
    assert 'hx-get="/activity/feed?kind=schedules"' in body
    assert body.count("filter-active") == 1
    assert "filter-active" in body.split('href="/activity?kind=schedules"')[1].split(">")[0]


# ---------------------------------------------------------------------------
# Empty states — the dir-less fresh-install fixture.
# ---------------------------------------------------------------------------


def test_activity_page_empty_state_on_dirless_home(anna_home: Path) -> None:
    response = _client(anna_home).get("/activity")

    assert response.status_code == 200
    assert "No data yet." in response.text
    assert 'class="feed-row' not in response.text


def test_activity_partial_empty_on_dirless_home(anna_home: Path) -> None:
    response = _client(anna_home).get("/activity/feed")

    assert response.status_code == 200
    assert "No data yet." in response.text
    # It's a partial, not a full page.
    assert "<html" not in response.text


# ---------------------------------------------------------------------------
# Feed rows from seeded audit fixtures.
# ---------------------------------------------------------------------------


def test_partial_serves_subagent_rows_with_meta(anna_home: Path) -> None:
    """Spawn carries slug+model; complete carries cost+duration."""
    today = _today()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.subagent.spawn",
                f"{today}T10:00:00.000Z",
                slug="researcher",
                model="claude-fable-5",
            ),
            _audit_record(
                "audit.subagent.complete",
                f"{today}T10:05:00.000Z",
                slug="researcher",
                cost_usd=0.42,
                duration_seconds=12.5,
            ),
        ],
    )

    response = _client(anna_home).get("/activity/feed")

    assert response.status_code == 200
    body = response.text
    assert 'class="feed-row' in body
    assert "audit.subagent.spawn" in body
    assert "audit.subagent.complete" in body
    assert "researcher" in body
    assert "claude-fable-5" in body
    assert "$0.42" in body
    assert "12.5s" in body
    # Mono HH:MM:SS timestamps, not the raw ISO string.
    assert ">10:05:00</span>" in body
    assert "T10:05:00" not in body


def test_partial_rows_newest_first(anna_home: Path) -> None:
    """File order is append-chronological; the feed reverses it."""
    _seed_mixed_kinds(anna_home)

    body = _client(anna_home).get("/activity/feed").text

    newest = body.index("audit.web.dashboard.config_write")
    middle = body.index("audit.schedule.fire")
    oldest = body.index("audit.subagent.spawn")
    assert newest < middle < oldest


def test_schedule_rows_carry_id_and_status(anna_home: Path) -> None:
    today = _today()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.schedule.complete",
                f"{today}T07:00:05.000Z",
                schedule_id="morning-brief",
                duration_seconds=4.2,
            ),
        ],
    )

    body = _client(anna_home).get("/activity/feed").text

    assert "morning-brief" in body
    # The meta line names the status (event tail) alongside the id.
    assert "morning-brief · complete" in body


def test_dashboard_rows_carry_actor_and_action(anna_home: Path) -> None:
    today = _today()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.web.dashboard.secret_write",
                f"{today}T09:00:00.000Z",
                actor="operator",
                key="SLACK_BOT_TOKEN",
            ),
        ],
    )

    body = _client(anna_home).get("/activity/feed").text

    assert "audit.web.dashboard.secret_write" in body
    assert "operator · secret_write" in body


def test_date_group_break_when_date_changes(anna_home: Path) -> None:
    """Newest-first rows spanning two dates get one break line between
    them (the reader windows by filename, so yesterday-stamped rows in
    today's file still serve)."""
    today, yesterday = _today(), _yesterday()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.schedule.fire",
                f"{yesterday}T23:59:00.000Z",
                schedule_id="brief",
            ),
            _audit_record(
                "audit.schedule.complete",
                f"{today}T00:01:00.000Z",
                schedule_id="brief",
            ),
        ],
    )

    body = _client(anna_home).get("/activity/feed").text

    assert body.count('class="feed-break"') == 1
    assert f">{yesterday}</span>" in body
    # The break sits between the rows: after today's, before yesterday's.
    assert body.index("audit.schedule.complete") < body.index('class="feed-break"')
    assert body.index('class="feed-break"') < body.index("audit.schedule.fire")


def test_no_group_break_within_a_single_date(anna_home: Path) -> None:
    _seed_mixed_kinds(anna_home)

    body = _client(anna_home).get("/activity/feed").text

    assert 'class="feed-break"' not in body


# ---------------------------------------------------------------------------
# Kind filtering.
# ---------------------------------------------------------------------------


def test_filter_param_respected_on_partial(anna_home: Path) -> None:
    _seed_mixed_kinds(anna_home)
    client = _client(anna_home)

    subagents = client.get("/activity/feed", params={"kind": "subagents"}).text
    assert "audit.subagent.spawn" in subagents
    assert "audit.schedule.fire" not in subagents
    assert "audit.web.dashboard.config_write" not in subagents

    schedules = client.get("/activity/feed", params={"kind": "schedules"}).text
    assert "audit.schedule.fire" in schedules
    assert "audit.subagent.spawn" not in schedules

    dashboard = client.get("/activity/feed", params={"kind": "dashboard"}).text
    assert "audit.web.dashboard.config_write" in dashboard
    assert "audit.subagent.spawn" not in dashboard
    assert "audit.schedule.fire" not in dashboard


def test_filter_param_respected_on_full_page(anna_home: Path) -> None:
    _seed_mixed_kinds(anna_home)

    body = _client(anna_home).get("/activity", params={"kind": "subagents"}).text

    assert "audit.subagent.spawn" in body
    assert "audit.schedule.fire" not in body


def test_all_filter_shows_every_family(anna_home: Path) -> None:
    _seed_mixed_kinds(anna_home)

    body = _client(anna_home).get("/activity/feed", params={"kind": "all"}).text

    assert "audit.subagent.spawn" in body
    assert "audit.schedule.fire" in body
    assert "audit.web.dashboard.config_write" in body


def test_unknown_kind_normalizes_to_all(anna_home: Path) -> None:
    _seed_mixed_kinds(anna_home)

    response = _client(anna_home).get("/activity/feed", params={"kind": "bogus"})

    assert response.status_code == 200
    body = response.text
    assert "audit.subagent.spawn" in body
    assert "audit.schedule.fire" in body
    # The poll URL re-anchors to the normalized kind.
    assert 'hx-get="/activity/feed?kind=all"' in body


# ---------------------------------------------------------------------------
# Status classes — fail visibly distinct, complete ok, spawn/fire idle.
# ---------------------------------------------------------------------------


def test_fail_event_row_carries_fail_class(anna_home: Path) -> None:
    today = _today()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.schedule.fail",
                f"{today}T07:00:10.000Z",
                schedule_id="morning-brief",
                kind="timeout",
                consecutive_failures=2,
            ),
        ],
    )

    body = _client(anna_home).get("/activity/feed").text

    assert 'class="feed-row feed-row-fail"' in body
    # The failure kind surfaces in the meta line.
    assert "morning-brief · fail · timeout" in body


def test_subagent_fail_row_carries_fail_class(anna_home: Path) -> None:
    today = _today()
    _seed_audit_events(
        anna_home,
        [
            _audit_record(
                "audit.subagent.fail",
                f"{today}T10:10:00.000Z",
                slug="researcher",
                kind="timeout",
                duration_seconds=300.0,
            ),
        ],
    )

    body = _client(anna_home).get("/activity/feed").text

    assert 'class="feed-row feed-row-fail"' in body
    assert "feed-row-ok" not in body


def test_complete_and_spawn_rows_carry_ok_and_idle_classes(anna_home: Path) -> None:
    today = _today()
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
                duration_seconds=12.5,
            ),
        ],
    )

    body = _client(anna_home).get("/activity/feed").text

    assert 'class="feed-row feed-row-idle"' in body  # spawn
    assert 'class="feed-row feed-row-ok"' in body  # complete
    assert "feed-row-fail" not in body
