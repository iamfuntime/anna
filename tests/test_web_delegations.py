"""Tests for the delegation & cost view (MC-07).

Done conditions from the plan
(Inbox/2026-06-10-anna-web-mission-control-plan.md, subtask 7):

* ``GET /delegations`` renders the four data sections — summary strip,
  per-model split, per-agent run history, daily cost strip — from real
  :class:`DelegationReader` output, with nav highlighting.
* Bars are pure CSS: fill widths/heights arrive as inline percentages,
  no chart lib, no JS.
* On a dir-less fixture (fresh install: no ``transcripts/``) every
  section degrades to the shared ``_panel_empty.html`` state — zero
  500s.
* The 30s htmx poll target (``/delegations/panels/data``) serves the
  data sections as a partial.

Fixture strategy mirrors :mod:`tests.test_web_dashboard`: copy
``anna.yaml.example`` into a tmp home, build a fresh app via
``create_app``, exercise through :class:`fastapi.testclient.TestClient`.
Transcript trailer shapes are copied from the writer-side conventions
the reader tests pin (:mod:`tests.test_web_delegation_reader`).
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.app import create_app
from anna_web.readers.delegation_reader import DEFAULT_WINDOW_DAYS

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` — deliberately dir-less.

    No ``transcripts/``: the fresh-install shape every section must
    degrade gracefully on.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


def _make_cfg(anna_home: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_dashboard.py — the derived
    # anna_home field is forced onto the tmp home so
    # subagent_transcript_dir resolves under it.
    object.__setattr__(cfg, "anna_home", anna_home)
    return cfg


def _client(anna_home: Path) -> TestClient:
    return TestClient(create_app(_make_cfg(anna_home)))


# ---------------------------------------------------------------------------
# Seed helpers — writer-shaped transcript trailer fixtures.
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    """Local date, matching DelegationReader's default ``today`` anchor."""
    return date.today().isoformat()


def _seed_runs(
    cfg: AnnaConfig,
    slug: str,
    specs: list[dict[str, Any]],
    *,
    day: date | None = None,
) -> None:
    """Append outbound transcript trailers under a slug's day-file.

    Each spec may override ``ts`` / ``model`` / ``cost`` / ``duration``
    / ``tools`` / ``audit_id``; defaults mirror the runner's on-disk
    shape pinned by tests/test_web_delegation_reader.py. ``model: None``
    omits the field (a pre-model-trailer line → "unknown" tier).
    """
    day = day or date.today()
    slug_dir = cfg.subagent_transcript_dir / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{day.isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as fp:
        for spec in specs:
            line: dict[str, Any] = {
                "ts": spec.get("ts", datetime.now(timezone.utc).isoformat()),
                "direction": "outbound",
                "conv_key": f"subagent:{slug}",
                "text": "done",
                "audit_id": spec.get("audit_id", "aid-1"),
                "cost_usd": spec.get("cost", 1.0),
                "duration_seconds": spec.get("duration", 10.0),
                "tool_calls": spec.get("tools", ["Read", "Write"]),
            }
            model = spec.get("model", "claude-fable-5")
            if model is not None:
                line["model"] = model
            fp.write(json.dumps(line) + "\n")


# ---------------------------------------------------------------------------
# Shell: page + nav.
# ---------------------------------------------------------------------------


def test_delegations_page_200_with_sections_and_nav_highlight(
    anna_home: Path,
) -> None:
    """GET /delegations renders all four section panels, highlights its
    own nav entry, and wires the 30s data poll."""
    response = _client(anna_home).get("/delegations")

    assert response.status_code == 200
    body = response.text
    for panel_id in (
        "panel-delegation-summary",
        "panel-model-split",
        "panel-delegation-history",
        "panel-daily-cost",
    ):
        assert f'id="{panel_id}"' in body, f"missing section panel {panel_id}"
    # Data sections live-refresh via htmx, per the plan's
    # polling-not-SSE decision.
    assert 'hx-get="/delegations/panels/data"' in body
    assert 'hx-trigger="every 30s"' in body
    # Active-page highlighting: only the Delegations link is current.
    assert '<a href="/delegations" aria-current="page">Delegations</a>' in body
    assert body.count('aria-current="page"') == 1


# ---------------------------------------------------------------------------
# Empty states — the dir-less fresh-install fixture.
# ---------------------------------------------------------------------------


def test_every_section_degrades_to_no_data_yet(anna_home: Path) -> None:
    """Dir-less home: all four sections render the shared
    _panel_empty.html state and nothing 500s."""
    response = _client(anna_home).get("/delegations")

    assert response.status_code == 200
    assert response.text.count("No data yet.") == 4
    assert "delegation-history-table" not in response.text


def test_poll_partial_empty_on_dirless_home(anna_home: Path) -> None:
    response = _client(anna_home).get("/delegations/panels/data")

    assert response.status_code == 200
    assert "No data yet." in response.text
    # It's a partial, not a full page.
    assert "<html" not in response.text


# ---------------------------------------------------------------------------
# Data-bearing sections.
# ---------------------------------------------------------------------------


def test_history_table_lists_runs_newest_first(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home)
    today = _today_iso()
    _seed_runs(
        cfg,
        "researcher",
        [
            {
                "ts": f"{today}T08:00:00+00:00",
                "cost": 1.0,
                "audit_id": "r-am",
            },
            {
                "ts": f"{today}T16:00:00+00:00",
                "cost": 2.0,
                "duration": 42.5,
                "tools": ["Read", "Edit", "Write"],
                "model": "claude-opus-4-1",
                "audit_id": "r-pm",
            },
        ],
    )
    _seed_runs(
        cfg,
        "code-writer",
        [{"ts": f"{today}T12:00:00+00:00", "cost": 0.5, "audit_id": "cw-1"}],
    )

    body = TestClient(create_app(cfg)).get("/delegations").text

    # Every run renders a row, newest first.
    for audit_id in ("r-pm", "cw-1", "r-am"):
        assert f'data-audit-id="{audit_id}"' in body
    assert (
        body.index('data-audit-id="r-pm"')
        < body.index('data-audit-id="cw-1"')
        < body.index('data-audit-id="r-am"')
    )
    # Row fields: slug, tier with raw model in the title attr, mono
    # ts, duration, cost, tool-call count.
    assert "<code>researcher</code>" in body
    assert "<code>code-writer</code>" in body
    assert 'title="claude-opus-4-1">opus</span>' in body
    assert f"{today}T16:00:00+00:00" in body
    assert "42.5s" in body
    assert "$2.00" in body
    # Transcript path renders as mono text, not a link (files aren't
    # served).
    transcript = str(cfg.subagent_transcript_dir / "researcher" / f"{today}.jsonl")
    assert transcript in body
    assert f'href="{transcript}"' not in body


def test_model_split_renders_tiers_with_bar_widths(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home)
    today = _today_iso()
    _seed_runs(
        cfg,
        "code-writer",
        [
            {"ts": f"{today}T09:00:00+00:00", "model": "claude-fable-5", "cost": 2.0, "audit_id": "f-1"},
            {"ts": f"{today}T10:00:00+00:00", "model": "claude-fable-5", "cost": 1.0, "audit_id": "f-2"},
            {"ts": f"{today}T11:00:00+00:00", "model": "claude-opus-4-1", "cost": 1.0, "audit_id": "o-1"},
        ],
    )

    body = TestClient(create_app(cfg)).get("/delegations").text

    # One row per tier, costliest first (the reader's ordering).
    assert 'data-tier="fable"' in body
    assert 'data-tier="opus"' in body
    assert body.index('data-tier="fable"') < body.index('data-tier="opus"')
    # CSS-only bars: inline widths as % of the costliest tier.
    assert 'style="width: 100.0%"' in body
    assert 'style="width: 33.3%"' in body
    # Raw-model breakdown surfaces via the row's title attr.
    assert "claude-fable-5: 2 runs, $3.00" in body
    assert "claude-opus-4-1: 1 runs, $1.00" in body
    # Totals per tier.
    assert "$3.00" in body


def test_summary_strip_numbers(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home)
    today = _today_iso()
    _seed_runs(
        cfg,
        "researcher",
        [
            {"ts": f"{today}T09:00:00+00:00", "cost": 1.25, "audit_id": "r-1"},
            {"ts": f"{today}T10:00:00+00:00", "cost": 0.75, "audit_id": "r-2"},
        ],
    )
    _seed_runs(
        cfg,
        "code-writer",
        [{"ts": f"{today}T11:00:00+00:00", "cost": 0.5, "audit_id": "cw-1"}],
    )

    body = TestClient(create_app(cfg)).get("/delegations").text

    # All runs are today, so today == this week == $2.50 across 3 runs.
    assert 'id="summary-today-cost">$2.50</span>' in body
    assert 'id="summary-week-cost">$2.50</span>' in body
    assert 'id="summary-run-count">3</span>' in body
    # researcher ran twice, code-writer once.
    assert 'id="summary-busiest">researcher</code>' in body


def test_daily_strip_renders_window_bars(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home)
    today = _today_iso()
    _seed_runs(
        cfg,
        "researcher",
        [
            {"ts": f"{today}T09:00:00+00:00", "cost": 1.0, "audit_id": "d-1"},
            {"ts": f"{today}T10:00:00+00:00", "cost": 1.0, "audit_id": "d-2"},
        ],
    )

    body = TestClient(create_app(cfg)).get("/delegations").text

    # One cell per day of the window, zero-filled days included.
    assert body.count('class="day-col"') == DEFAULT_WINDOW_DAYS
    # Today is the costliest day → full-height CSS bar; the exact
    # figures live in the cell's title.
    assert 'style="height: 100.0%"' in body
    assert f"{today} — $2.00 across 2 runs" in body


def test_poll_partial_serves_data_sections(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home)
    today = _today_iso()
    _seed_runs(
        cfg,
        "researcher",
        [{"ts": f"{today}T09:00:00+00:00", "cost": 1.0, "audit_id": "p-1"}],
    )

    response = TestClient(create_app(cfg)).get("/delegations/panels/data")

    assert response.status_code == 200
    body = response.text
    assert "<html" not in body
    for panel_id in (
        "panel-delegation-summary",
        "panel-model-split",
        "panel-delegation-history",
        "panel-daily-cost",
    ):
        assert f'id="{panel_id}"' in body
    assert 'data-audit-id="p-1"' in body
    assert 'data-tier="fable"' in body
