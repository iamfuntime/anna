"""Auth-outage intercept in the scheduler (2026-09-04 incident).

The bundled CLI answers every turn with the synthetic text
"Failed to authenticate: OAuth session expired and could not be refreshed"
once the OAuth refresh token lapses. Before this guard that text was a
tool-free reply, so the no-tool-call backstop retried, failed, and three
ticks later auto-disabled ``heartbeat``. The guard must:

* never count the run toward ``consecutive_failures`` / auto-disable,
* never post the text to the destination,
* alert the operator ONCE per :data:`AUTH_ALERT_INTERVAL_SECONDS`,
* and stamp ``last_status="auth_expired"`` so the outage is visible per row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anna.runtime.scheduler import AUTH_ALERT_INTERVAL_SECONDS, AUTH_EXPIRED_MARKER
from tests.test_scheduler_no_tool_call_guard import (
    _events,
    _make_scheduler,
    _read_audit_records,
)

AUTH_FAIL = f"Failed to authenticate: {AUTH_EXPIRED_MARKER}"
SID = "oem-slide-restock-watch"


@pytest.mark.asyncio
async def test_auth_expired_reply_never_strikes_or_posts(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _make_scheduler(tmp_path, [(AUTH_FAIL, 0)])

    for _ in range(cfg.scheduler.failure_threshold + 2):
        await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]

    # One dispatch per tick: the intercept runs BEFORE the no-tool-call retry.
    assert len(router.dispatched) == cfg.scheduler.failure_threshold + 2
    # The auth text never reaches the destination.
    assert adapter.sent == []

    schedule = store.get(SID)
    assert schedule is not None
    assert schedule.enabled is True
    assert schedule.state.consecutive_failures == 0
    assert schedule.state.last_status == "auth_expired"

    audits = _read_audit_records(cfg.audit_dir)
    assert not _events(audits, "audit.schedule.fail")
    assert not _events(audits, "audit.schedule.disabled")
    assert not _events(audits, "audit.schedule.no_tool_call_retry")
    rows = _events(audits, "audit.schedule.auth_expired")
    assert len(rows) == cfg.scheduler.failure_threshold + 2
    assert rows[0]["level"] == "ERROR"
    assert rows[0]["alerted"] is True
    assert all(r["alerted"] is False for r in rows[1:])


@pytest.mark.asyncio
async def test_auth_expired_alert_is_rate_limited_to_once_per_hour(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _make_scheduler(tmp_path, [(AUTH_FAIL, 0)])
    alerter = sched._alerter

    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]
    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]
    # Let the fire-and-forget alert task run.
    import asyncio

    await asyncio.sleep(0)
    assert len(alerter.warned) == 1  # type: ignore[attr-defined]
    assert "OAuth session expired" in alerter.warned[0]  # type: ignore[attr-defined]

    # Age the last alert past the window: the next tick alerts again.
    sched._last_auth_alert_monotonic -= AUTH_ALERT_INTERVAL_SECONDS + 1  # type: ignore[operator]
    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert len(alerter.warned) == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auth_expired_exception_path_is_intercepted(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _make_scheduler(tmp_path, [("unused", 1)])

    async def _raise(event):  # noqa: ANN001
        if event.completion_future is not None:
            event.completion_future.set_exception(RuntimeError(AUTH_FAIL))

    router.dispatch = _raise  # type: ignore[assignment]
    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]

    schedule = store.get(SID)
    assert schedule is not None
    assert schedule.enabled is True
    assert schedule.state.consecutive_failures == 0
    assert schedule.state.last_status == "auth_expired"
    assert not _events(_read_audit_records(cfg.audit_dir), "audit.schedule.fail")


@pytest.mark.asyncio
async def test_ordinary_no_tool_call_prose_still_strikes(tmp_path: Path) -> None:
    """Regression guard: the intercept is marker-specific, not a blanket
    exemption for tool-free replies."""
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [("I'll read the skill file first", 0)]
    )
    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]
    schedule = store.get(SID)
    assert schedule is not None
    assert schedule.state.consecutive_failures == 1
    assert schedule.state.last_status == "fail"


# ---------------------------------------------------------------------------
# 2026-09-05 false positive: a healthy reply that merely MENTIONS the phrase
# mid-text must post normally. Only the CLI's synthetic failure shape (short,
# marker at the start, no tool calls) may trip the intercept.
# ---------------------------------------------------------------------------

# Real failure shape, verbatim from audit-2026-09-04.jsonl 17:00Z..22:28Z
# (output_length 72, tool_call_count 0, identical every tick).
REAL_FAILURE_REPLY = "Failed to authenticate: OAuth session expired and could not be refreshed"

# Shape of the 2026-09-05 10:00Z morning-brief reply that was wrongly
# swallowed: long, tool-backed, phrase quoted in the middle.
NORMAL_REPLY_MENTIONING_PHRASE = (
    "*Morning Brief — Saturday, September 5, 2026*\n\n"
    "*EMAIL*\n_feldman_seth_ — 15 unread (1 shown, 14 filtered as noise)\n"
    "• Google — *Critical security alert* (cross-notification of the item below)\n"
    "_sefeldma_ — 2 unread\n\n"
    "*CALENDAR*\nNothing on the calendar today.\n\n"
    "*SYSTEM*\nYesterday 17:00Z–22:28Z every scheduled run failed with "
    f"\"{AUTH_EXPIRED_MARKER}\" until Seth re-logged in; heartbeat was "
    "auto-disabled and has since been re-enabled. Credentials are healthy "
    "this morning (refresh token good to 2026-10-04).\n\n"
    "*TODO*\n• Review the security alert\n• Confirm heartbeat ran clean at 12:00Z\n"
)
assert len(NORMAL_REPLY_MENTIONING_PHRASE) > 240
assert AUTH_EXPIRED_MARKER in NORMAL_REPLY_MENTIONING_PHRASE


@pytest.mark.asyncio
async def test_real_failure_shape_is_intercepted(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(REAL_FAILURE_REPLY, 0)]
    )
    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]

    assert adapter.sent == []
    schedule = store.get(SID)
    assert schedule is not None
    assert schedule.state.last_status == "auth_expired"
    assert schedule.state.consecutive_failures == 0
    assert len(_events(_read_audit_records(cfg.audit_dir), "audit.schedule.auth_expired")) == 1


@pytest.mark.asyncio
async def test_normal_reply_mentioning_phrase_mid_text_posts_normally(tmp_path: Path) -> None:
    cfg, store, router, adapter, sched = await _make_scheduler(
        tmp_path, [(NORMAL_REPLY_MENTIONING_PHRASE, 7)]
    )
    await sched._guarded_fire(store.get(SID))  # type: ignore[arg-type]
    import asyncio

    await asyncio.sleep(0)

    # Posted to the destination, recorded as a normal success, no alert.
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == NORMAL_REPLY_MENTIONING_PHRASE
    schedule = store.get(SID)
    assert schedule is not None
    assert schedule.state.last_status == "complete"
    assert schedule.state.consecutive_failures == 0
    audits = _read_audit_records(cfg.audit_dir)
    assert not _events(audits, "audit.schedule.auth_expired")
    assert sched._alerter.warned == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("reply", "tool_calls", "expected"),
    [
        # The real shape, with and without surrounding whitespace / unknown count.
        (REAL_FAILURE_REPLY, 0, True),
        (REAL_FAILURE_REPLY, None, True),
        (f"\n  {REAL_FAILURE_REPLY}\n", 0, True),
        # Marker alone at the start (prefix dropped) still matches.
        (AUTH_EXPIRED_MARKER, 0, True),
        # Phrase mid-text in a long, tool-backed reply: NOT auth-expired.
        (NORMAL_REPLY_MENTIONING_PHRASE, 7, False),
        # Long reply with unknown tool count: still not (length bound).
        (NORMAL_REPLY_MENTIONING_PHRASE, None, False),
        # Short reply that mentions the phrase but does not start with it.
        (f"FYI: yesterday's runs hit '{AUTH_EXPIRED_MARKER}'. All good now.", 0, False),
        # Starts with the prefix but the run executed tools: authenticated fine.
        (REAL_FAILURE_REPLY, 3, False),
        # Prefix without the marker: some other auth error, not this intercept.
        ("Failed to authenticate: invalid API key", 0, False),
        ("", 0, False),
        (None, 0, False),
    ],
)
def test_is_auth_expired_reply_shape_rule(
    reply: str | None, tool_calls: int | None, expected: bool
) -> None:
    from anna.runtime.scheduler import Scheduler

    meta: dict[str, object] = {}
    if tool_calls is not None:
        meta["tool_call_count"] = tool_calls
    assert Scheduler._is_auth_expired_reply(reply, meta) is expected


def test_is_auth_expired_exception_keeps_substring_match() -> None:
    from anna.runtime.scheduler import Scheduler

    assert Scheduler._is_auth_expired_exception(f"RuntimeError: {AUTH_EXPIRED_MARKER} (code 401)")
    assert not Scheduler._is_auth_expired_exception("RuntimeError: connection reset")
    assert not Scheduler._is_auth_expired_exception(None)
