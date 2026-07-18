# Turn watchdog, and the deferred hard-interrupt

Decision record — 2026-07-18. Owner: Seth. Status: v1 shipped; hard interrupt deferred.

## Problem

ANNA's Slack/Telegram transport is **buffered**: an interactive turn only flushes
when it terminates, and inbound operator messages **queue behind the running turn**.
When ANNA does long work *inline* in her main loop instead of backgrounding it to a
sub-agent, the operator's channel goes effectively dead until the turn ends. This has
recurred and is the failure this work targets. The guard keys on the measurable *harm*
(a turn held the channel too long), not on trying to classify "deep reasoning."

## What shipped (v1) — `runtime.turn_watchdog`

- `src/anna/runtime/turn_watchdog.py` — a pure state machine with an injectable clock
  (`note_tool_call`, `mark_ended`, `poll -> WatchdogAction`, `telemetry`). Each level
  fires at most once and goes permanently silent once work is backgrounded.
- Worker wiring in `src/anna/runtime/worker.py`, armed beside the existing timed-drip
  flush task; `note_tool_call` in the `ToolUseBlock` branch; teardown + telemetry in the
  same `finally`.
- Config (default-on): `runtime.turn_watchdog.enabled`, `soft_threshold_seconds` (60),
  `hard_threshold_seconds` (120, validated `> soft`). Scoped to buffered (Slack/Telegram)
  interactive turns; the CLI streams live so its channel never goes dead.

Behavior at soft / hard threshold (only while the turn has NOT backgrounded its work):
1. **Flush** pending narration to the channel (operator sees progress, not silence).
2. **Telemetry** for every interactive turn: duration, tool-call count, whether a
   sub-agent/background task was spawned, soft/hard breach flags → audit log.
3. **One admin alert** on hard breach (not per drip tick) → a regression is *loud*.
4. **Deferred forcing reminder**: a `<system-reminder>` prepended to ANNA's *next* turn
   telling her to background remaining work and end the turn.

### What v1 is, and is NOT

v1 is **visibility + accountability + a next-turn correction loop**. It makes the failure
impossible to hide and actively nudges ANNA out of it. It does **not** physically stop a
long turn from blocking the operator's queued messages — that is still bounded by ANNA's
own discipline (which this reinforces) or by the deferred option below.

## Why hard interrupt was deferred (the fork)

Under the current architecture the worker is a **single-owned-stream consumer** that
drains one turn to its `ResultMessage`. Two consequences:

1. **Mid-turn `client.query(reminder)` does not steer the live turn.** The CLI treats it
   as a *separate* turn whose output routes to the operator via the idle/unsolicited path
   — i.e. a reminder meant for ANNA would **leak to the operator**. Hence the v1
   deferred-prepend realization instead.
2. **`client.interrupt()` is the only true mid-turn stop, but it aborts the turn and
   discards in-flight work.** That is a product/architecture call with real downside and
   was not taken.

## The deferred option (revisit trigger)

**Option 3 — true mid-turn interruption / inbound preemption.** Let an operator message
soft-interrupt a running turn so the channel never fully dies, and/or hard-interrupt at
the hard threshold. Prerequisite work: a way to **checkpoint/preserve in-flight work**
before abort (so an interrupt doesn't throw away a half-finished delegation or edit), and
a notion of safe interrupt points.

**Revisit ONLY if channel-blocking recurs despite v1** — i.e. the admin alerts show
repeated hard breaches, or Seth observes the channel going dead again. Until then, v1's
accountability + next-turn nudge is the intended guard.

## Verify

- `cd /home/funtime/git/anna && uv run pytest tests/test_turn_watchdog.py tests/test_worker_flush.py -q`
- Live smoke: temporarily set `soft_threshold_seconds: 5`, `hard_threshold_seconds: 10`,
  restart, and hold a DM turn inline past those thresholds; expect narration flush, a
  `audit.turn.watchdog_breach` row, and exactly one admin alert.
