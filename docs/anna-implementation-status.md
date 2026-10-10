# Anna reliability implementation status

## Checkpoint: Phase 0 watchdog fix in progress

This worktree is on `iamfuntime/anna-reliability`, based on
`e7a701594d7839ef66959e496d1b9a2905361b32`.

An earlier checkpoint incorrectly treated supplied **used** percentages as
remaining percentages. The corrected snapshot is Codex 9% session used / 14%
weekly used (91% / 86% remaining), so the mandatory remaining-below-20%
guardrail does not apply. That earlier pause was a documentation error.

Implementation resumed only through the smallest Phase 0 diagnosis requested
before the next stopping point. No credentials, services, live accounts, or
external channels were touched.

The copied plan at `docs/anna-reliability-plan.md` was read in full.  It remains
unmodified and is currently untracked because it was supplied with this
worktree.

## Repository checkpoint

- Original full-suite baseline reported by the handoff: `uv run --extra dev
  pytest -q` on Python 3.13.15: 1,388 passed, 2 failed, 1 skipped.
- The two known failures have a concrete watchdog/consolidation diagnosis;
  current focused validation is pending as described below.
- `orca account list --json` was retried with the correct
  `result.rateLimits` filter, but returned `null`; this is unavailable usage
  data, not a low-quota signal. Updating the worktree card was unavailable
  because the local Orca runtime was not running; no restart was attempted.
- The user-supplied copied plan remains the only intended untracked file.

## Current Phase 0 change

Changed files:

- `src/anna/runtime/worker.py`: watchdog breaches still emit telemetry,
  reminders, and hard-breach alerts, but only flush an intermediate message
  when periodic incremental delivery is active. This prevents a disabled
  periodic flush or voice-only reply policy from being overridden by the
  watchdog and fragmenting a deliberately consolidated response.
- `tests/test_turn_watchdog.py`: watchdog flush tests explicitly select
  text/incremental delivery rather than relying on the default voice-only
  configuration.

The original focused run reproduced the supplied failures exactly:

```text
uv run --extra dev pytest -q tests/test_worker_periodic_flush.py \
  tests/test_router_identity_alias.py tests/test_checkpoint.py \
  tests/test_transcript_resume.py
45 passed, 2 failed
```

The two failures were `test_voice_only_never_drips` and
`test_disabled_interval_never_drips`; both showed two messages caused by the
watchdog's hard-threshold flush. A subsequent focused watchdog run after the
worker change showed the periodic-flush tests passing but two direct watchdog
flush tests failing because their fixture defaults to voice-only output. Those
two tests have now been corrected to explicitly disable voice output; **they
have not been rerun after that final fixture correction** because the user
requested an immediate safe stopping point. Do not claim this checkpoint is
green.

## Completed milestones

No phase exit is complete. The watchdog diagnosis/fix is the only in-progress
Phase 0 slice.

## Deferred milestones

1. **Phase 0:** add and pass regressions for event-bound reply routing,
   checkpoint coverage/atomicity, receiver liveness, synchronous Google-call
   isolation, and periodic-flush behavior.
2. **Phase 1:** SQLite-backed owner-scoped memory, FTS retrieval per turn,
   provenance, explicit remember/correct/forget, and durable extraction jobs.
3. **Phase 2:** durable task/run/outbox records and a mocked, deterministic,
   read-only Google heartbeat with honest partial results.
4. **Phase 3:** connection/repair dashboard and doctor UX.
5. Browser/computer automation, autonomous writes, real-account testing, and
   deployment remain expressly out of scope for this checkpoint.

## Safe isolated continuation setup

Use an isolated home and mocked providers only; do not source production
environment files or bot tokens.  A continuation should begin with:

```bash
git status --short --branch
ANNA_HOME="$(mktemp -d)" uv run --extra dev pytest -q tests/test_worker_periodic_flush.py
```

First run the currently pending focused validation:

```bash
ANNA_HOME="$(mktemp -d)" uv run --extra dev pytest -q \
  tests/test_worker_periodic_flush.py tests/test_turn_watchdog.py
```

If green, run the earlier Phase 0 set, then inspect router/checkpoint contracts
and add focused tests before each implementation increment. Run relevant
lint/type checks from `pyproject.toml`, and run the full suite only after
targeted tests pass.

## Copyable continuation brief

```text
Continue in /home/funtime/orca/workspaces/anna/anna-reliability on
iamfuntime/anna-reliability. Do not deploy, push, create a PR, merge, access
credentials, poll live channels, or operate live Google/Slack/Telegram
accounts. Preserve the user-supplied untracked docs/anna-reliability-plan.md.

First validate the pending Phase 0 watchdog change with:
ANNA_HOME="$(mktemp -d)" uv run --extra dev pytest -q \
  tests/test_worker_periodic_flush.py tests/test_turn_watchdog.py

The worker now suppresses watchdog intermediate sends when periodic delivery is
disabled or voice-only is selected, but preserves watchdog telemetry/reminders/
alerts. The direct watchdog flush tests explicitly disable voice output. Do not
claim green until that command and the broader focused Phase 0 tests pass.

Next implement only staged, reviewable Phase 0 work from
docs/anna-reliability-plan.md: event-bound cross-channel reply routing,
atomic/coverage-safe checkpoints, receiver liveness, and safe Google blocking
call isolation. Then memory MVP and durable run/outbox only after Phase 0 is
validated. Use mocks and temporary ANNA_HOME exclusively.
```

## Migration and rollback guidance

No migration was created.  When Phase 1 starts, create a versioned SQLite
migration ledger with backup-before-upgrade and a tested read-compatible
rollback path.  Keep the existing markdown/transcript data authoritative until
import is proven; do not delete or reinterpret legacy checkpoints as fully
covered conversation history.  Each durable outbox/run schema change should be
additive and preserve pending records across downgrade/restart.

## Unresolved blockers

- Pending focused test rerun after the final watchdog-test fixture correction.
- The local Orca runtime was unavailable for worktree-status updates; this is
  non-blocking for code work.
- The local Orca runtime was unavailable for worktree-status updates; this is
  non-blocking for code work once resources permit.
