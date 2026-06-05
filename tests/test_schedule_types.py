"""Tests for the Phase 2 schedule Pydantic models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from anna.runtime.schedule_types import Schedule, ScheduleDestination, ScheduleState


def _make_schedule(**overrides: object) -> Schedule:
    base: dict[str, object] = {
        "id": "morning-brief",
        "cron": "0 6 * * *",
        "prompt": "Compose a morning brief.",
        "destination": ScheduleDestination(transport="slack", channel="C0AFD2LM38R"),
        "created_at": datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return Schedule(**base)


def test_destination_accepts_slack() -> None:
    d = ScheduleDestination(transport="slack", channel="C0AFD2LM38R")
    assert d.transport == "slack"


def test_destination_accepts_telegram() -> None:
    d = ScheduleDestination(transport="telegram", channel="993947726")
    assert d.transport == "telegram"


def test_destination_rejects_unknown_transport() -> None:
    with pytest.raises(ValidationError):
        ScheduleDestination(transport="discord", channel="123")  # type: ignore[arg-type]


def test_destination_requires_channel() -> None:
    with pytest.raises(ValidationError):
        ScheduleDestination(transport="slack")  # type: ignore[call-arg]


def test_state_defaults() -> None:
    s = ScheduleState()
    assert s.last_fired_at is None
    assert s.last_status is None
    assert s.consecutive_failures == 0


def test_state_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        ScheduleState(last_status="weird")  # type: ignore[arg-type]


def test_schedule_minimum_fields() -> None:
    s = _make_schedule()
    assert s.id == "morning-brief"
    assert s.cron == "0 6 * * *"
    assert s.timezone == "America/New_York"
    assert s.timeout_seconds == 300
    assert s.enabled is True
    assert s.natural_language is None
    assert s.state.consecutive_failures == 0
    assert s.ephemeral is False


def test_schedule_ephemeral_opt_in() -> None:
    s = _make_schedule(ephemeral=True)
    assert s.ephemeral is True


def test_schedule_ephemeral_defaults_false_when_omitted() -> None:
    """A record with no ``ephemeral`` key (legacy schedules.yaml) loads as
    non-ephemeral, preserving the existing behavior."""
    rebuilt = Schedule.model_validate(
        {
            "id": "legacy",
            "cron": "0 6 * * *",
            "prompt": "x",
            "destination": {"transport": "slack", "channel": "C1"},
            "created_at": datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc),
        }
    )
    assert rebuilt.ephemeral is False


def test_schedule_with_natural_language_preserves_both() -> None:
    s = _make_schedule(natural_language="every morning at 6am")
    assert s.natural_language == "every morning at 6am"
    assert s.cron == "0 6 * * *"


def test_schedule_requires_cron() -> None:
    with pytest.raises(ValidationError):
        Schedule(  # type: ignore[call-arg]
            id="x",
            prompt="x",
            destination=ScheduleDestination(transport="slack", channel="c"),
            created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )


def test_schedule_round_trip_serializes_cleanly() -> None:
    """Schedule -> dict -> Schedule preserves every field."""
    s = _make_schedule(
        natural_language="every morning at 6am",
        state=ScheduleState(
            last_fired_at=datetime(2026, 6, 8, 6, 0, 14, tzinfo=timezone.utc),
            last_status="complete",
            consecutive_failures=0,
        ),
    )
    dumped = s.model_dump()
    rebuilt = Schedule.model_validate(dumped)
    assert rebuilt == s


def test_schedule_disabled_stays_disabled() -> None:
    s = _make_schedule(enabled=False)
    assert s.enabled is False
