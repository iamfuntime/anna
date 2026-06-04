"""Tests for ``anna_web.schedule_store_adapter.ScheduleStoreAdapter``.

The load-bearing claim covered here (subtask 1) is the stale-schedules
fix: the adapter must reload ``schedules.yaml`` from disk on every read
rather than latching the first load forever. The dashboard is one of
several writers to that file — the daemon mutates state fields on its
poll cycle, the MCP ``schedule_create`` tool adds rows, and the
operator may hand-edit it — so a list that never re-reads goes stale
the moment anything changes the file out of band.

This test fails against the pre-fix adapter (which set ``_loaded =
True`` after the first load and returned the cached snapshot forever)
and passes once the read paths reload every call.

Fixture strategy mirrors :mod:`tests.test_web_schedule_routes`:
tmp_path-backed anna_home with a hand-seeded ``schedules.yaml`` in the
daemon's on-disk shape, and an adapter rebound to that home.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from anna.config import AnnaConfig
from anna_web.schedule_store_adapter import ScheduleStoreAdapter


def _record(
    *,
    id: str,
    cron: str = "0 6 * * *",
    transport: str = "slack",
    channel: str = "C0AFD2LM38R",
    prompt: str = "Compose a morning brief.",
    enabled: bool = True,
    timezone_name: str = "America/New_York",
) -> dict:
    """Build one schedule record in ScheduleStore's on-disk shape."""
    return {
        "id": id,
        "natural_language": None,
        "cron": cron,
        "timezone": timezone_name,
        "prompt": prompt,
        "destination": {"transport": transport, "channel": channel},
        "timeout_seconds": 300,
        "enabled": enabled,
        "created_at": datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        "state": {
            "last_fired_at": None,
            "last_status": None,
            "consecutive_failures": 0,
        },
    }


def _write_schedules_yaml(home: Path, records: list[dict]) -> None:
    """Write a v1 schedules.yaml with the given records into ``home``.

    Stands in for an out-of-band writer (the daemon's MCP
    ``schedule_create`` or an operator hand-edit) — it touches the YAML
    directly, never going through the adapter.
    """
    home.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "schedules": records}
    (home / "schedules.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake $ANNA_HOME with the audit dir + scheduler state dir."""
    home = tmp_path / "anna_home"
    home.mkdir()
    (home / "audit").mkdir()
    return home


def _make_adapter(anna_home: Path) -> ScheduleStoreAdapter:
    """Build an adapter pointed at ``anna_home``'s schedules.yaml."""
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", anna_home)
    cfg.scheduler.state_path = str(anna_home / "schedules.yaml")
    cfg.logging.audit.fsync_on_write = False
    return ScheduleStoreAdapter(anna_home=anna_home, config=cfg)


async def test_list_all_reflects_out_of_band_append(anna_home: Path) -> None:
    """A schedule added to schedules.yaml out of band appears on the next read.

    Sequence:
      1. Seed one schedule and list it (this performs the first load).
      2. Append a second schedule by rewriting the YAML directly —
         simulating the daemon's schedule_create or a hand-edit, with
         no web restart and no call through the adapter's write surface.
      3. list_all() again must reflect BOTH schedules.

    Against the pre-fix adapter, step 3 returns only the first schedule
    because the cache was latched after step 1.
    """
    _write_schedules_yaml(anna_home, [_record(id="morning-brief")])
    adapter = _make_adapter(anna_home)

    first = await adapter.list_all()
    assert {s.id for s in first} == {"morning-brief"}

    # Out-of-band append: a second writer adds a row directly to disk.
    _write_schedules_yaml(
        anna_home,
        [
            _record(id="morning-brief"),
            _record(id="weekly-roundup", cron="0 9 * * 1", channel="C123ROUNDUP"),
        ],
    )

    second = await adapter.list_all()
    assert {s.id for s in second} == {"morning-brief", "weekly-roundup"}


async def test_get_reflects_out_of_band_append(anna_home: Path) -> None:
    """get() also reloads: a freshly-added id resolves without a restart."""
    _write_schedules_yaml(anna_home, [_record(id="morning-brief")])
    adapter = _make_adapter(anna_home)

    # Prime the cache with the first load.
    assert await adapter.get("weekly-roundup") is None

    _write_schedules_yaml(
        anna_home,
        [
            _record(id="morning-brief"),
            _record(id="weekly-roundup", cron="0 9 * * 1", channel="C123ROUNDUP"),
        ],
    )

    found = await adapter.get("weekly-roundup")
    assert found is not None
    assert found.id == "weekly-roundup"
    assert found.cron == "0 9 * * 1"
