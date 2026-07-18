"""Interactive-turn watchdog.

Guards against a long INTERACTIVE turn silently holding the operator's
channel hostage. The daemon's buffered transports (Slack/Telegram) only
flush when a turn terminates, and inbound operator messages queue behind
the running turn — so when ANNA does heavy work inline instead of
backgrounding it to a sub-agent, the channel goes dead. We guard on the
measurable HARM (the turn is held too long WITHOUT having yielded), not on
any attempt to classify "deep reasoning."

This module is a small, pure state machine with an INJECTABLE clock so the
threshold/telemetry logic is deterministically unit-testable without the
real wall-clock or an event loop. The worker owns the async ticker that
drives :meth:`TurnWatchdog.poll` on the existing drip/flush cadence and acts
on the returned :class:`WatchdogAction` (flush narration, inject a forcing
system-reminder, record a breach, fire one admin alert).

Design note (single-owned-stream drain): the forcing reminder is injected
by PREPENDING it to ANNA's NEXT turn — the moment she yields — rather than
steered into the live SDK session mid-turn. The worker's owned-stream
consumer drains one turn to its ResultMessage, and a mid-turn ``query()``
would be processed by the CLI as a SEPARATE turn whose output routes to the
operator via the idle path (leaking a reminder meant for ANNA). Deferred
prepend is therefore the safe realization here; true mid-turn interruption
(``client.interrupt()``, which aborts and loses work) is a product call.
"""

from __future__ import annotations

import enum
from typing import Any, Callable


class WatchdogAction(enum.Enum):
    """Outcome of a single :meth:`TurnWatchdog.poll`.

    ``NONE`` — no threshold newly crossed (or the turn has already
    backgrounded its work, so the watchdog stays silent).
    ``SOFT`` — the soft threshold was crossed for the first time on an
    un-yielded turn: flush narration + inject the forcing reminder.
    ``HARD`` — the hard threshold was crossed for the first time on an
    un-yielded turn: escalate (flush + stronger reminder + record breach +
    one admin alert).
    """

    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


# SDK-namespaced tool name for ANNA's delegate (sub-agent spawn). Mirrors the
# ``mcp__anna_delegate__delegate`` convention the worker builds from
# ``DELEGATE_TOOL_NAMES``; kept as a literal here to avoid importing the
# delegate server (and its heavy deps) into this pure module.
_DELEGATE_TOOL_NAME = "mcp__anna_delegate__delegate"


def is_background_spawn(tool_name: str, tool_input: Any = None) -> bool:
    """True when a tool call moved work OFF the current turn.

    Two shapes count as backgrounding:

    * The ``delegate`` tool (``mcp__anna_delegate__delegate``) — ANNA spawned
      a one-shot sub-agent.
    * The builtin ``Bash`` tool invoked with ``run_in_background: true`` — a
      detached background command.

    Anything else (a Read, an Edit, a foreground Bash, a web fetch) is
    inline work that keeps holding the turn, so it does NOT count.
    """
    if tool_name == _DELEGATE_TOOL_NAME:
        return True
    if tool_name == "Bash" and isinstance(tool_input, dict):
        return bool(tool_input.get("run_in_background"))
    return False


class TurnWatchdog:
    """Per-turn threshold + telemetry state machine (injectable clock).

    Construct one per qualifying interactive turn. Feed it every tool call
    (:meth:`note_tool_call`) so it can track the tool count and notice when
    work has been backgrounded, and drive :meth:`poll` on a wall-clock
    cadence. ``poll`` fires each level (soft, then hard) AT MOST ONCE, and
    never fires once work has been backgrounded — so a turn that acked and
    delegated early stays silent.

    ``clock`` is any zero-arg callable returning a monotonically increasing
    float (production passes ``loop.time``; tests pass a list-backed fake).
    ``start_time`` defaults to ``clock()`` at construction.
    """

    def __init__(
        self,
        *,
        soft_seconds: float,
        hard_seconds: float,
        clock: Callable[[], float],
        start_time: float | None = None,
    ) -> None:
        self._soft = float(soft_seconds)
        self._hard = float(hard_seconds)
        self._clock = clock
        self._start = clock() if start_time is None else float(start_time)
        self._end: float | None = None

        self.tool_call_count = 0
        self.backgrounded = False
        self.soft_breached = False
        self.hard_breached = False

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def note_tool_call(self, tool_name: str, tool_input: Any = None) -> None:
        """Record one tool call. Marks the turn backgrounded when applicable."""
        self.tool_call_count += 1
        if is_background_spawn(tool_name, tool_input):
            self.backgrounded = True

    def mark_ended(self) -> None:
        """Freeze the elapsed clock at turn end so telemetry is stable.

        Idempotent — a second call is ignored so a teardown that runs on
        multiple exit paths cannot rewind the recorded duration.
        """
        if self._end is None:
            self._end = self._clock()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """Seconds since turn start (frozen once :meth:`mark_ended` ran)."""
        end = self._end if self._end is not None else self._clock()
        return end - self._start

    def poll(self) -> WatchdogAction:
        """Evaluate thresholds at the current clock reading.

        Returns the newly-crossed level, or ``NONE``. Fires HARD before SOFT
        when both are already past (a stalled/slow tick), marking soft
        breached too so the escalation is recorded coherently. Once work has
        been backgrounded the watchdog goes quiet: the point is to catch
        UN-yielded long turns, not a turn that already handed work off and is
        winding down.
        """
        if self.backgrounded:
            return WatchdogAction.NONE
        elapsed = self.elapsed_seconds()
        if elapsed >= self._hard and not self.hard_breached:
            # Escalation implies the soft line was also crossed; record both
            # so telemetry never shows a hard breach without its soft.
            self.soft_breached = True
            self.hard_breached = True
            return WatchdogAction.HARD
        if elapsed >= self._soft and not self.soft_breached:
            self.soft_breached = True
            return WatchdogAction.SOFT
        return WatchdogAction.NONE

    def telemetry(self) -> dict[str, Any]:
        """Structured per-turn record for the audit log."""
        return {
            "duration_seconds": round(self.elapsed_seconds(), 3),
            "tool_call_count": self.tool_call_count,
            "backgrounded": self.backgrounded,
            "soft_breached": self.soft_breached,
            "hard_breached": self.hard_breached,
        }


# ---------------------------------------------------------------------------
# Forcing reminders
# ---------------------------------------------------------------------------
#
# Prepended (as a ``<system-reminder>`` block) to ANNA's NEXT turn — the moment
# she yields — mirroring how the cadence reminder is prepended in the worker.
# Kept here next to the state machine so the wording lives with the guard it
# serves.


def soft_reminder(soft_seconds: int) -> str:
    """Forcing reminder injected at the soft threshold."""
    return (
        "<system-reminder>\n"
        f"You have been working inline on a single interactive turn for over "
        f"{soft_seconds}s without yielding. On buffered transports "
        "(Slack/Telegram) the operator's channel is BLOCKED until this turn "
        "ends — their messages cannot reach you while you keep working. Post "
        "one short status line, move the remaining work off this turn (the "
        "delegate tool for a sub-agent, or Bash with run_in_background=true "
        "for a detached command), and END this turn now. Do not keep doing "
        "heavy work inline.\n"
        "</system-reminder>"
    )


def hard_reminder(hard_seconds: int) -> str:
    """Stronger forcing reminder injected at the hard threshold."""
    return (
        "<system-reminder>\n"
        f"STOP — this turn has held the operator's channel for over "
        f"{hard_seconds}s. End the turn IMMEDIATELY: post a brief status line "
        "and background ALL remaining work to a sub-agent now. Continuing to "
        "work inline is a hard cadence violation that leaves the operator "
        "unable to reach you.\n"
        "</system-reminder>"
    )
