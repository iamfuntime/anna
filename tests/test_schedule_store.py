"""Tests for the ScheduleStore persistence layer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from anna.config import AnnaConfig
from anna.runtime.schedule_store import ScheduleStore, ScheduleValidationError
from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState
from anna.runtime.supervisor import Supervisor


def _make_config(tmp_path: Path, *, admin_slack: str = "", admin_telegram: str = "") -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    cfg.admin.slack_channel_id = admin_slack
    cfg.admin.telegram_chat_id = admin_telegram
    cfg.scheduler.state_path = str(tmp_path / "schedules.yaml")
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _make_schedule(
    *,
    id: str = "morning-brief",
    cron: str = "0 6 * * *",
    transport: str = "slack",
    channel: str = "C0AFD2LM38R",
    created_at: datetime | None = None,
    enabled: bool = True,
    state: ScheduleState | None = None,
) -> Schedule:
    return Schedule(
        id=id,
        cron=cron,
        prompt="Compose a morning brief.",
        destination=ScheduleDestination(transport=transport, channel=channel),  # type: ignore[arg-type]
        created_at=created_at or datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
        enabled=enabled,
        state=state or ScheduleState(),
    )


def _read_audit_records(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not audit_dir.exists():
        return out
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _events(records: list[dict], name: str) -> list[dict]:
    return [r for r in records if r.get("event") == name]


@pytest.mark.asyncio
async def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.load()
    assert store.list() == []


@pytest.mark.asyncio
async def test_create_persists_to_yaml(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.load()

    await store.create(_make_schedule(), actor_conv="slack:dm:UTEST")
    assert len(store.list()) == 1

    on_disk = yaml.safe_load((tmp_path / "schedules.yaml").read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert len(on_disk["schedules"]) == 1
    assert on_disk["schedules"][0]["id"] == "morning-brief"

    audits = _read_audit_records(cfg.audit_dir)
    created = _events(audits, "audit.schedule.created")
    assert created and created[0]["schedule_id"] == "morning-brief"
    assert created[0]["creator_conv"] == "slack:dm:UTEST"


@pytest.mark.asyncio
async def test_create_duplicate_id_rejected(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    with pytest.raises(ScheduleValidationError, match=r"already exists"):
        await store.create(_make_schedule())


@pytest.mark.asyncio
async def test_create_rejects_reserved_slack_channel(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, admin_slack="CADMIN123")
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    with pytest.raises(ScheduleValidationError, match=r"admin channel"):
        await store.create(_make_schedule(channel="CADMIN123"))


@pytest.mark.asyncio
async def test_create_rejects_reserved_telegram_chat(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, admin_telegram="993947726")
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    with pytest.raises(ScheduleValidationError, match=r"admin chat"):
        await store.create(_make_schedule(transport="telegram", channel="993947726"))


@pytest.mark.asyncio
async def test_create_rejects_invalid_cron(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    with pytest.raises(ScheduleValidationError, match=r"valid 5-field cron"):
        await store.create(_make_schedule(cron="not a cron"))


@pytest.mark.asyncio
async def test_update_merges_fields(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    updated = await store.update("morning-brief", timeout_seconds=600, enabled=False)
    assert updated.timeout_seconds == 600
    assert updated.enabled is False
    assert updated.cron == "0 6 * * *"
    audits = _read_audit_records(cfg.audit_dir)
    updates = _events(audits, "audit.schedule.updated")
    assert updates and set(updates[0]["changed_fields"]) == {"timeout_seconds", "enabled"}


@pytest.mark.asyncio
async def test_update_rejects_id_change(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    with pytest.raises(ScheduleValidationError, match=r"immutable"):
        await store.update("morning-brief", id="renamed")


@pytest.mark.asyncio
async def test_delete_removes_and_audits(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    await store.delete("morning-brief")
    assert store.list() == []
    audits = _read_audit_records(cfg.audit_dir)
    assert _events(audits, "audit.schedule.deleted")


@pytest.mark.asyncio
async def test_mark_fired_resets_consecutive_failures(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule(state=ScheduleState(consecutive_failures=2)))
    await store.mark_fired(
        "morning-brief",
        status="complete",
        when=datetime(2026, 6, 8, 10, 0, 0, tzinfo=timezone.utc),
    )
    s = store.get("morning-brief")
    assert s is not None
    assert s.state.consecutive_failures == 0
    assert s.state.last_status == "complete"


@pytest.mark.asyncio
async def test_mark_failed_increments_returns_count(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    n1 = await store.mark_failed("morning-brief", reason="r1", when=datetime.now(timezone.utc))
    n2 = await store.mark_failed("morning-brief", reason="r2", when=datetime.now(timezone.utc))
    n3 = await store.mark_failed(
        "morning-brief", reason="r3", when=datetime.now(timezone.utc), kind="timeout"
    )
    assert (n1, n2, n3) == (1, 2, 3)
    s = store.get("morning-brief")
    assert s is not None
    assert s.state.last_status == "timeout"


@pytest.mark.asyncio
async def test_mark_disabled_flips_enabled_and_audits(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    await store.mark_disabled("morning-brief", reason="three strikes")
    s = store.get("morning-brief")
    assert s is not None
    assert s.enabled is False
    audits = _read_audit_records(cfg.audit_dir)
    disabled = _events(audits, "audit.schedule.disabled")
    assert disabled and disabled[0]["reason"] == "three strikes"


@pytest.mark.asyncio
async def test_due_schedules_first_fire(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    # Created at 5am UTC, cron 0 6 * * * in UTC, check at 7am — should be due.
    created = datetime(2026, 6, 8, 5, 0, 0, tzinfo=timezone.utc)
    await store.create(
        Schedule(
            id="morning-brief",
            cron="0 6 * * *",
            timezone="UTC",
            prompt="x",
            destination=ScheduleDestination(transport="slack", channel="C1"),
            created_at=created,
        )
    )
    due = store.due_schedules(datetime(2026, 6, 8, 7, 0, 0, tzinfo=timezone.utc))
    assert len(due) == 1
    assert due[0].id == "morning-brief"


@pytest.mark.asyncio
async def test_due_schedules_not_due_yet(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    last_fired = datetime(2026, 6, 8, 6, 0, 0, tzinfo=timezone.utc)
    await store.create(
        Schedule(
            id="morning-brief",
            cron="0 6 * * *",
            timezone="UTC",
            prompt="x",
            destination=ScheduleDestination(transport="slack", channel="C1"),
            created_at=last_fired - timedelta(days=1),
            state=ScheduleState(last_fired_at=last_fired, last_status="complete"),
        )
    )
    # Check 30 minutes later — next fire is tomorrow at 6am.
    due = store.due_schedules(last_fired + timedelta(minutes=30))
    assert due == []


@pytest.mark.asyncio
async def test_due_schedules_ignores_disabled(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule(enabled=False))
    due = store.due_schedules(datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert due == []


@pytest.mark.asyncio
async def test_round_trip_load_after_save(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    await store.create(_make_schedule(id="weekly", cron="0 9 * * MON"))
    await store.mark_failed("morning-brief", reason="x", when=datetime.now(timezone.utc))

    # Build a fresh store against the same path and confirm state survives.
    store2 = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store2.load()
    ids = sorted(s.id for s in store2.list())
    assert ids == ["morning-brief", "weekly"]
    mb = store2.get("morning-brief")
    assert mb is not None
    assert mb.state.consecutive_failures == 1


@pytest.mark.asyncio
async def test_ephemeral_round_trips_through_yaml(tmp_path: Path) -> None:
    """A schedule with ephemeral=True survives save -> load unchanged."""
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(
        Schedule(
            id="heartbeat",
            cron="*/30 * * * *",
            prompt="heartbeat",
            destination=ScheduleDestination(transport="slack", channel="C1"),
            created_at=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
            ephemeral=True,
        )
    )

    on_disk = yaml.safe_load((tmp_path / "schedules.yaml").read_text(encoding="utf-8"))
    assert on_disk["schedules"][0]["ephemeral"] is True

    store2 = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store2.load()
    reloaded = store2.get("heartbeat")
    assert reloaded is not None
    assert reloaded.ephemeral is True


@pytest.mark.asyncio
async def test_legacy_yaml_without_ephemeral_defaults_false(tmp_path: Path) -> None:
    """A schedules.yaml entry predating the ephemeral field (no ``ephemeral``
    key) loads as non-ephemeral, preserving backward compatibility."""
    cfg = _make_config(tmp_path)
    path = tmp_path / "schedules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "schedules": [
                    {
                        "id": "legacy",
                        "cron": "0 6 * * *",
                        "timezone": "America/New_York",
                        "prompt": "x",
                        "destination": {"transport": "slack", "channel": "C1"},
                        "timeout_seconds": 300,
                        "enabled": True,
                        "created_at": "2026-06-01T06:00:00+00:00",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.load()
    legacy = store.get("legacy")
    assert legacy is not None
    assert legacy.ephemeral is False


@pytest.mark.asyncio
async def test_atomic_save_writes_through_tmpfile(tmp_path: Path) -> None:
    """Confirm save writes via the .tmp + os.replace pattern."""
    cfg = _make_config(tmp_path)
    store = ScheduleStore(config=cfg, supervisor=Supervisor(config=cfg))
    await store.create(_make_schedule())
    # After save, the tmpfile should not linger.
    assert not (tmp_path / "schedules.yaml.tmp").exists()
    assert (tmp_path / "schedules.yaml").exists()
