"""Tests for the natural-language cron parser."""

from __future__ import annotations

import pytest
from croniter import croniter

from anna.runtime.nl_cron import parse_natural_language


@pytest.mark.parametrize(
    "phrase, expected",
    [
        # every morning
        ("every morning at 6am", "0 6 * * *"),
        ("every morning at 7:30am", "30 7 * * *"),
        ("every morning at 8", "0 8 * * *"),
        # every weekday
        ("every weekday at 9am", "0 9 * * 1-5"),
        ("every weekday at 17:30", "30 17 * * 1-5"),
        # every <day>
        ("every Monday at 10am", "0 10 * * MON"),
        ("every Friday at 5pm", "0 17 * * FRI"),
        ("every Sunday at 9:15am", "15 9 * * SUN"),
        # every N hours
        ("every 2 hours", "0 */2 * * *"),
        ("every 1 hour", "0 */1 * * *"),
        # every N minutes
        ("every 15 minutes", "*/15 * * * *"),
        ("every 5 minute", "*/5 * * * *"),
        # daily
        ("daily at 6am", "0 6 * * *"),
        ("daily at 23:45", "45 23 * * *"),
        # weekly on DAY
        ("weekly on Sunday at 9am", "0 9 * * SUN"),
        ("weekly on Wednesday at 14:00", "0 14 * * WED"),
    ],
)
def test_supported_patterns_parse(phrase: str, expected: str) -> None:
    assert parse_natural_language(phrase) == expected


@pytest.mark.parametrize(
    "phrase",
    [
        "sometime tomorrow",
        "kinda often",
        "when the mood strikes",
        "",
        "   ",
    ],
)
def test_unrecognized_raises(phrase: str) -> None:
    with pytest.raises(ValueError):
        parse_natural_language(phrase)


def test_every_n_hours_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match=r"out of range"):
        parse_natural_language("every 100 hours")


def test_every_n_minutes_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match=r"out of range"):
        parse_natural_language("every 60 minutes")


def test_bad_time_format_raises() -> None:
    with pytest.raises(ValueError):
        parse_natural_language("every morning at noonish")


def test_unknown_day_in_weekly_raises() -> None:
    with pytest.raises(ValueError):
        parse_natural_language("weekly on Funday at 9am")


def test_parser_output_round_trips_through_croniter() -> None:
    """Every supported pattern produces a cron expression croniter accepts."""
    samples = [
        "every morning at 6am",
        "every weekday at 9am",
        "every Monday at 10am",
        "every 2 hours",
        "every 15 minutes",
        "daily at 6am",
        "weekly on Sunday at 9am",
    ]
    for phrase in samples:
        cron = parse_natural_language(phrase)
        # croniter raises if the expression is malformed.
        assert croniter.is_valid(cron), f"croniter rejected '{cron}' from '{phrase}'"
