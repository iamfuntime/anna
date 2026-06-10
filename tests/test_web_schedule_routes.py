"""Tests for ``anna_web.routes.schedule_routes`` + the MC-06 run board.

The original eight CRUD cases from the Phase 2.5 buildout are kept
verbatim in intent (only markup pins moved with the board rebuild):

1. ``GET /schedules`` renders a row for every existing schedule.
2. ``GET /schedules`` on an empty store shows the empty-state copy.
3. ``GET /schedules/new`` returns 200 + a form.
4. ``POST /schedules`` with a valid payload creates the schedule
   and the new entry appears on disk.
5. ``POST /schedules`` with an invalid payload (bad cron) returns
   422 and re-renders the form with an inline error.
6. ``PUT /schedules/{id}`` updates a field and the YAML reflects it.
7. ``DELETE /schedules/{id}`` removes the entry (204).
8. ``DELETE`` against a missing id returns 404.

MC-06 adds the schedule run board on the read side:

* ``readers.schedule_board`` unit coverage — next-fire computation for
  a known cron/timezone fixture (mirroring the daemon scheduler's
  semantics: baseline = last_fired_at or created_at, per-schedule tz,
  naive baselines assumed UTC), disabled → ``None``, bad cron / bad
  timezone → ``None``, row shaping.
* Board route coverage — all columns render, the bad-cron row degrades
  to an em dash instead of raising, failing rows carry the
  ``schedule-row-failing`` flag, disabled rows are dimmed with no next
  fire, the ``/schedules/board`` poll partial returns the rows, and a
  mangled ``schedules.yaml`` fail-softs to the empty board.

Fixture strategy: tmp_path-backed anna_home with a freshly seeded
``schedules.yaml`` and a TestClient over an app whose
``schedule_store`` adapter is rebound to that home. Mirrors the
fixture style in :mod:`tests.test_web_config_routes`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna.runtime.schedule_types import Schedule
from anna_web.app import create_app
from anna_web.readers.schedule_board import (
    EMPTY_VALUE,
    build_row,
    compute_next_fire,
)
from anna_web.schedule_store_adapter import ScheduleStoreAdapter


def _seed_schedules_yaml(home: Path, records: list[dict]) -> None:
    """Write a v1 schedules.yaml with the given records into ``home``."""
    home.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "schedules": records}
    (home / "schedules.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def _record(
    *,
    id: str = "morning-brief",
    cron: str = "0 6 * * *",
    transport: str = "slack",
    channel: str = "C0AFD2LM38R",
    prompt: str = "Compose a morning brief.",
    enabled: bool = True,
    timezone_name: str = "America/New_York",
    natural_language: str | None = None,
    last_fired_at: str | None = None,
    last_status: str | None = None,
    consecutive_failures: int = 0,
) -> dict:
    """Build a single schedule record matching ScheduleStore's on-disk shape."""
    return {
        "id": id,
        "natural_language": natural_language,
        "cron": cron,
        "timezone": timezone_name,
        "prompt": prompt,
        "destination": {"transport": transport, "channel": channel},
        "timeout_seconds": 300,
        "enabled": enabled,
        "created_at": datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        "state": {
            "last_fired_at": last_fired_at,
            "last_status": last_status,
            "consecutive_failures": consecutive_failures,
        },
    }


def _schedule(**overrides) -> Schedule:
    """Build an in-memory Schedule for direct reader unit tests."""
    record = _record(**overrides)
    return Schedule.model_validate(record)


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake $ANNA_HOME with the audit dir + scheduler state dir."""
    home = tmp_path / "anna_home"
    home.mkdir()
    (home / "audit").mkdir()
    return home


def _make_client(anna_home: Path, *, seed: list[dict] | None = None) -> TestClient:
    """Build a TestClient pointed at ``anna_home`` with optional seed data."""
    if seed is not None:
        _seed_schedules_yaml(anna_home, seed)
    cfg = AnnaConfig()
    # Match the override pattern used by the config-routes tests so the
    # AnnaConfig instance carries our tmp anna_home for downstream
    # derivations (audit_dir, scheduler.resolved_state_path, etc.).
    object.__setattr__(cfg, "anna_home", anna_home)
    cfg.scheduler.state_path = str(anna_home / "schedules.yaml")
    cfg.logging.audit.fsync_on_write = False
    app = create_app(cfg)
    # Defensive: rebind the schedule_store to one freshly pointed at the
    # tmp anna_home, matching the pattern test_web_config_routes uses for
    # config_store. Without this, any future create_app refactor that
    # stops pulling anna_home into the adapter would silently route
    # writes at the operator's real ~/anna/schedules.yaml.
    app.state.schedule_store = ScheduleStoreAdapter(anna_home=anna_home, config=cfg)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. GET /schedules renders the board with every schedule.
# ---------------------------------------------------------------------------


def test_get_schedules_lists_all(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[
            _record(id="morning-brief", cron="0 6 * * *"),
            _record(id="weekly-roundup", cron="0 9 * * 1", channel="C123ROUNDUP"),
        ],
    )
    response = client.get("/schedules")
    assert response.status_code == 200
    body = response.text
    assert "morning-brief" in body
    assert "weekly-roundup" in body
    assert "0 6 * * *" in body
    assert "0 9 * * 1" in body


# ---------------------------------------------------------------------------
# 2. Empty store shows empty-state.
# ---------------------------------------------------------------------------


def test_get_schedules_empty_state(anna_home: Path) -> None:
    client = _make_client(anna_home, seed=[])
    response = client.get("/schedules")
    assert response.status_code == 200
    assert "No schedules yet" in response.text


# ---------------------------------------------------------------------------
# 3. GET /schedules/new returns 200 + form.
# ---------------------------------------------------------------------------


def test_get_new_schedule_form(anna_home: Path) -> None:
    client = _make_client(anna_home, seed=[])
    response = client.get("/schedules/new")
    assert response.status_code == 200
    body = response.text
    # Required form fields are present.
    assert 'name="id"' in body
    assert 'name="prompt"' in body
    assert 'name="destination_transport"' in body
    assert 'name="destination_channel"' in body
    assert 'name="cron"' in body
    assert 'name="timezone"' in body
    # And the form posts back at /schedules for create.
    assert 'hx-post="/schedules"' in body


# ---------------------------------------------------------------------------
# 4. POST /schedules creates a new schedule on disk.
# ---------------------------------------------------------------------------


def test_post_creates_schedule(anna_home: Path) -> None:
    client = _make_client(anna_home, seed=[])
    response = client.post(
        "/schedules",
        data={
            "id": "new-brief",
            "prompt": "Send the brief.",
            "destination_transport": "slack",
            "destination_channel": "C0BRIEFCHN",
            "cron": "0 7 * * *",
            "natural_language": "every day at 7am",
            "timezone": "America/New_York",
            "timeout_seconds": "180",
            "enabled": "on",
        },
    )
    assert response.status_code == 201, response.text
    # The board-row partial is returned; the id and the computed
    # destination display should appear.
    assert "new-brief" in response.text
    assert "slack:C0BRIEFCHN" in response.text
    # And the YAML file on disk reflects the new entry.
    on_disk = yaml.safe_load(
        (anna_home / "schedules.yaml").read_text(encoding="utf-8")
    )
    ids = [s["id"] for s in on_disk["schedules"]]
    assert "new-brief" in ids
    new = next(s for s in on_disk["schedules"] if s["id"] == "new-brief")
    assert new["cron"] == "0 7 * * *"
    assert new["destination"]["transport"] == "slack"
    assert new["destination"]["channel"] == "C0BRIEFCHN"
    assert new["natural_language"] == "every day at 7am"


# ---------------------------------------------------------------------------
# 5. Invalid cron returns 422 with inline error.
# ---------------------------------------------------------------------------


def test_post_invalid_cron_returns_422(anna_home: Path) -> None:
    client = _make_client(anna_home, seed=[])
    response = client.post(
        "/schedules",
        data={
            "id": "bad-cron",
            "prompt": "Doesn't matter.",
            "destination_transport": "slack",
            "destination_channel": "C0BADCHN",
            "cron": "this-is-not-cron",
            "natural_language": "",
            "timezone": "UTC",
            "timeout_seconds": "300",
            "enabled": "on",
        },
    )
    assert response.status_code == 422, response.text
    body = response.text
    # The form re-rendered with the operator's values preserved.
    assert 'value="bad-cron"' in body
    assert "this-is-not-cron" in body
    # And the cron error message lands as inline copy.
    assert "cron" in body.lower()
    # The schedule was not persisted.
    assert not (anna_home / "schedules.yaml").exists() or "bad-cron" not in (
        anna_home / "schedules.yaml"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. PUT /schedules/{id} updates the entry.
# ---------------------------------------------------------------------------


def test_put_updates_schedule(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[_record(id="brief", prompt="Original prompt.")],
    )
    response = client.put(
        "/schedules/brief",
        data={
            "prompt": "Updated prompt.",
            "destination_transport": "slack",
            "destination_channel": "C0AFD2LM38R",
            "cron": "0 6 * * *",
            "natural_language": "",
            "timezone": "America/New_York",
            "timeout_seconds": "300",
            "enabled": "on",
        },
    )
    assert response.status_code == 200, response.text
    on_disk = yaml.safe_load(
        (anna_home / "schedules.yaml").read_text(encoding="utf-8")
    )
    brief = next(s for s in on_disk["schedules"] if s["id"] == "brief")
    assert brief["prompt"] == "Updated prompt."


# ---------------------------------------------------------------------------
# 7. DELETE /schedules/{id} removes.
# ---------------------------------------------------------------------------


def test_delete_removes_schedule(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[
            _record(id="brief"),
            _record(id="roundup", cron="0 9 * * 1", channel="C123ROUNDUP"),
        ],
    )
    response = client.delete("/schedules/brief")
    assert response.status_code == 204
    on_disk = yaml.safe_load(
        (anna_home / "schedules.yaml").read_text(encoding="utf-8")
    )
    ids = [s["id"] for s in on_disk["schedules"]]
    assert "brief" not in ids
    assert "roundup" in ids


# ---------------------------------------------------------------------------
# 8. DELETE nonexistent returns 404.
# ---------------------------------------------------------------------------


def test_delete_missing_schedule_returns_404(anna_home: Path) -> None:
    client = _make_client(anna_home, seed=[])
    response = client.delete("/schedules/nonexistent")
    assert response.status_code == 404


# ===========================================================================
# MC-06: schedule_board reader unit coverage.
# ===========================================================================


def test_compute_next_fire_first_fire_known_cron_tz() -> None:
    """First fire baselines on created_at, in the schedule's timezone.

    created_at = 2026-06-01 00:00 UTC = 2026-05-31 20:00 EDT; the next
    "0 6 * * *" tick in America/New_York is 2026-06-01 06:00 EDT,
    which is 10:00 UTC (June → EDT, UTC-4).
    """
    schedule = _schedule(cron="0 6 * * *", timezone_name="America/New_York")
    next_fire = compute_next_fire(schedule)
    assert next_fire == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def test_compute_next_fire_baselines_on_last_fired() -> None:
    """After a fire, the next tick computes from last_fired_at —
    mirroring ScheduleStore.due_schedules exactly."""
    schedule = _schedule(
        cron="0 6 * * *",
        timezone_name="America/New_York",
        last_fired_at=datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc).isoformat(),
        last_status="complete",
    )
    next_fire = compute_next_fire(schedule)
    assert next_fire == datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)


def test_compute_next_fire_naive_baseline_assumed_utc() -> None:
    """A naive last_fired_at (hand-edited YAML) is treated as UTC,
    matching the daemon's due_schedules behavior."""
    schedule = _schedule(
        cron="0 6 * * *",
        timezone_name="America/New_York",
        last_fired_at="2026-06-03T10:00:00",
        last_status="complete",
    )
    next_fire = compute_next_fire(schedule)
    assert next_fire == datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)


def test_compute_next_fire_disabled_is_none() -> None:
    schedule = _schedule(enabled=False)
    assert compute_next_fire(schedule) is None


def test_compute_next_fire_bad_cron_is_none() -> None:
    # Schedule.model_validate does not validate cron (only the store's
    # create/update paths do), so a hand-edited bad expression loads.
    schedule = _schedule(cron="not a cron at all")
    assert compute_next_fire(schedule) is None


def test_compute_next_fire_bad_timezone_is_none() -> None:
    schedule = _schedule(timezone_name="Mars/Olympus_Mons")
    assert compute_next_fire(schedule) is None


def test_build_row_shapes_display_fields() -> None:
    row = build_row(
        _schedule(
            id="brief",
            natural_language="every day at 6am",
            consecutive_failures=2,
            last_status="fail",
            last_fired_at=datetime(
                2026, 6, 8, 10, 0, tzinfo=timezone.utc
            ).isoformat(),
        )
    )
    assert row.id == "brief"
    assert row.destination == "slack:C0AFD2LM38R"
    assert row.natural_language == "every day at 6am"
    assert row.failing is True
    assert row.last_fired_display == "2026-06-08 10:00 UTC"
    assert row.next_fire_display == "2026-06-09 10:00 UTC"


def test_build_row_never_fired_shows_empty_values() -> None:
    row = build_row(_schedule(enabled=False))
    assert row.last_fired_at is None
    assert row.last_fired_display == EMPTY_VALUE
    assert row.next_fire is None
    assert row.next_fire_display == EMPTY_VALUE


# ===========================================================================
# MC-06: board route coverage.
# ===========================================================================


def test_board_renders_all_columns(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[
            _record(
                id="morning-brief",
                cron="0 6 * * *",
                natural_language="every day at 6am",
                last_fired_at="2026-06-08T10:00:00+00:00",
                last_status="complete",
            )
        ],
    )
    response = client.get("/schedules")
    assert response.status_code == 200
    body = response.text
    # Column headers.
    for header in (
        "ID",
        "Cron",
        "Timezone",
        "Destination",
        "Enabled",
        "Last fired",
        "Status",
        "Failures",
        "Next fire",
    ):
        assert f"<th>{header}</th>" in body
    # Row values: identity + destination + run state.
    assert "morning-brief" in body
    assert "0 6 * * *" in body
    assert "every day at 6am" in body
    assert "America/New_York" in body
    assert "slack:C0AFD2LM38R" in body
    assert "badge-enabled" in body
    assert "badge-ok" in body
    assert "2026-06-08 10:00 UTC" in body
    # Computed next fire: last fired 10:00 UTC == 06:00 EDT, so the
    # next "0 6 * * *" New York tick is 06-09 06:00 EDT == 10:00 UTC.
    assert "2026-06-09 10:00 UTC" in body
    # The row links to the existing edit form.
    assert 'href="/schedules/morning-brief/edit"' in body
    # And the tbody carries the 10s poll back to the partial.
    assert 'hx-get="/schedules/board"' in body
    assert 'hx-trigger="every 10s"' in body


def test_board_bad_cron_row_degrades_to_dash(anna_home: Path) -> None:
    """A single bad cron renders its row with an em dash, not a 500."""
    client = _make_client(
        anna_home,
        seed=[
            _record(id="broken", cron="not a cron at all"),
            _record(
                id="healthy",
                cron="0 6 * * *",
                last_fired_at="2026-06-08T10:00:00+00:00",
                last_status="complete",
            ),
        ],
    )
    response = client.get("/schedules")
    assert response.status_code == 200
    body = response.text
    # Both rows render; the broken one shows the em dash for next fire
    # while the healthy one still computes.
    assert "broken" in body
    assert "healthy" in body
    assert EMPTY_VALUE in body
    assert "2026-06-09 10:00 UTC" in body


def test_board_failing_row_is_flagged(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[
            _record(
                id="flaky",
                last_status="fail",
                consecutive_failures=2,
            )
        ],
    )
    response = client.get("/schedules")
    assert response.status_code == 200
    body = response.text
    assert "schedule-row-failing" in body
    assert "badge-fail" in body
    # The failure count renders inside the fail badge.
    assert ">2</span>" in body


def test_board_disabled_row_dimmed_with_no_next_fire(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[
            _record(
                id="paused",
                enabled=False,
                last_fired_at="2026-06-08T10:00:00+00:00",
                last_status="complete",
            )
        ],
    )
    response = client.get("/schedules")
    assert response.status_code == 200
    body = response.text
    assert "schedule-row-disabled" in body
    assert "badge-disabled" in body
    # Disabled → next fire is never computed, even with a valid cron.
    assert EMPTY_VALUE in body
    assert "2026-06-09 10:00 UTC" not in body


def test_board_poll_partial_returns_rows(anna_home: Path) -> None:
    client = _make_client(
        anna_home,
        seed=[
            _record(id="morning-brief"),
            _record(id="weekly-roundup", cron="0 9 * * 1", channel="C123ROUNDUP"),
        ],
    )
    response = client.get("/schedules/board")
    assert response.status_code == 200
    body = response.text
    # Bare tbody rows — no page chrome.
    assert '<tr id="schedule-row-morning-brief"' in body
    assert '<tr id="schedule-row-weekly-roundup"' in body
    assert "<table" not in body
    assert "<html" not in body.lower()


def test_board_poll_partial_empty_state(anna_home: Path) -> None:
    client = _make_client(anna_home, seed=[])
    response = client.get("/schedules/board")
    assert response.status_code == 200
    assert "No schedules yet" in response.text


def test_board_missing_yaml_renders_empty_board(anna_home: Path) -> None:
    """No schedules.yaml at all (fresh install) → empty board, not a 500."""
    client = _make_client(anna_home, seed=None)
    response = client.get("/schedules")
    assert response.status_code == 200
    assert "No schedules yet" in response.text


def test_board_invalid_yaml_fails_soft(anna_home: Path) -> None:
    """A hand-mangled schedules.yaml degrades to the empty board."""
    client = _make_client(anna_home, seed=None)
    (anna_home / "schedules.yaml").write_text(
        "schedules: [unclosed", encoding="utf-8"
    )
    response = client.get("/schedules")
    assert response.status_code == 200
    assert "No schedules yet" in response.text
    # The poll partial fail-softs the same way.
    partial = client.get("/schedules/board")
    assert partial.status_code == 200
    assert "No schedules yet" in partial.text
