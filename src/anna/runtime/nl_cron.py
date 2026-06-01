"""Natural-language cron parser for the Phase 2 scheduler.

Covers the small set of phrases an operator actually types when
creating a schedule. The parser is deliberately narrow: anything not
matched raises ``ValueError`` pointing at the ``cron:`` explicit path.
There is no fuzzy matching, no LLM call, no third-party NL library
to audit. If a phrase falls outside the supported patterns, the
operator passes a 5-field cron expression instead.

Supported patterns (case-insensitive):

* ``every morning at HH(:MM)`` or ``every morning at H(am|pm)``
* ``every weekday at HH:MM``
* ``every (Monday|Tuesday|...) at HH:MM``
* ``every N hours``
* ``every N minutes``
* ``daily at HH:MM``
* ``weekly on (Monday|...) at HH:MM``
"""

from __future__ import annotations

import re

_DAY_NAMES: dict[str, str] = {
    "monday": "MON",
    "mon": "MON",
    "tuesday": "TUE",
    "tue": "TUE",
    "tues": "TUE",
    "wednesday": "WED",
    "wed": "WED",
    "thursday": "THU",
    "thu": "THU",
    "thurs": "THU",
    "friday": "FRI",
    "fri": "FRI",
    "saturday": "SAT",
    "sat": "SAT",
    "sunday": "SUN",
    "sun": "SUN",
}

_TIME_RE = re.compile(
    r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?$",
    re.IGNORECASE,
)

_SUPPORTED_PATTERNS = (
    "'every morning at HH:MM' (or 'every morning at 6am')",
    "'every weekday at HH:MM'",
    "'every Monday at HH:MM' (any weekday)",
    "'every N hours' (1-23)",
    "'every N minutes' (1-59)",
    "'daily at HH:MM'",
    "'weekly on Monday at HH:MM' (any weekday)",
)


def _unsupported(phrase: str) -> ValueError:
    return ValueError(
        f"Could not parse '{phrase}' as a recurrence. Supported patterns: "
        + "; ".join(_SUPPORTED_PATTERNS)
        + ". For anything more complex, pass cron: explicitly."
    )


def _parse_time(s: str, source_phrase: str) -> tuple[int, int]:
    """Return (hour, minute) for a time fragment like '6am', '7:30', '17:45'."""
    m = _TIME_RE.match(s.strip())
    if not m:
        raise ValueError(
            f"Could not parse time '{s}' in '{source_phrase}'. Try '6am', '14:30', or '9:00'."
        )
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"Time '{s}' in '{source_phrase}' is out of range.")
    return hour, minute


def parse_natural_language(phrase: str) -> str:
    """Return a 5-field cron expression for the given NL phrase.

    Raises ValueError with a helpful message on anything unrecognized.
    Caller is expected to round-trip the result through croniter to
    confirm it parses; this function does basic shape validation but
    does not validate that the cron itself is syntactically perfect.
    """
    if not phrase or not phrase.strip():
        raise ValueError("Empty recurrence phrase. " + "; ".join(_SUPPORTED_PATTERNS))

    p = phrase.strip().lower()

    # every morning at HH(:MM)
    m = re.match(r"^every morning at (.+)$", p)
    if m:
        h, mm = _parse_time(m.group(1), phrase)
        return f"{mm} {h} * * *"

    # every weekday at HH:MM
    m = re.match(r"^every weekday at (.+)$", p)
    if m:
        h, mm = _parse_time(m.group(1), phrase)
        return f"{mm} {h} * * 1-5"

    # every <DayName> at HH:MM
    m = re.match(r"^every (\w+) at (.+)$", p)
    if m:
        day_token = m.group(1)
        if day_token in _DAY_NAMES:
            h, mm = _parse_time(m.group(2), phrase)
            return f"{mm} {h} * * {_DAY_NAMES[day_token]}"
        # fall through to other patterns

    # every N hours
    m = re.match(r"^every (\d+) hours?$", p)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 23):
            raise ValueError(
                f"'every {n} hours' is out of range (1-23). For daily, use 'daily at HH:MM'."
            )
        return f"0 */{n} * * *"

    # every N minutes
    m = re.match(r"^every (\d+) minutes?$", p)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 59):
            raise ValueError(
                f"'every {n} minutes' is out of range (1-59). For hourly, use 'every 1 hours'."
            )
        return f"*/{n} * * * *"

    # daily at HH:MM
    m = re.match(r"^daily at (.+)$", p)
    if m:
        h, mm = _parse_time(m.group(1), phrase)
        return f"{mm} {h} * * *"

    # weekly on <DayName> at HH:MM
    m = re.match(r"^weekly on (\w+) at (.+)$", p)
    if m:
        day_token = m.group(1)
        if day_token not in _DAY_NAMES:
            raise ValueError(
                f"Unknown day '{day_token}' in '{phrase}'. Use Monday, Tuesday, ..., Sunday."
            )
        h, mm = _parse_time(m.group(2), phrase)
        return f"{mm} {h} * * {_DAY_NAMES[day_token]}"

    raise _unsupported(phrase)
