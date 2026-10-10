# Anna reliability implementation status

## Checkpoint: paused before implementation

This worktree is on `iamfuntime/anna-reliability`, based on
`e7a701594d7839ef66959e496d1b9a2905361b32`.

The requested implementation is intentionally paused at the mandatory resource
guardrail.  The handoff supplied a Codex rolling-session balance of **4%** and
a weekly balance of **13%**; both are below the required 20% minimum.  Starting
database migrations or reliability changes without enough capacity to run their
targeted and full regression suites would leave an unsafe, partially validated
slice.  No application code, configuration, credentials, services, live
accounts, or external channels were touched.

The copied plan at `docs/anna-reliability-plan.md` was read in full.  It remains
unmodified and is currently untracked because it was supplied with this
worktree.

## Repository checkpoint

- Existing baseline reported by the handoff (not rerun during this low-quota
  checkpoint): `uv run --extra dev pytest -q` on Python 3.13.15: 1,388 passed,
  2 failed, 1 skipped.
- Known failures to resolve before weakening any tests:
  `tests/test_worker_periodic_flush.py::test_voice_only_never_drips` and
  `tests/test_worker_periodic_flush.py::test_disabled_interval_never_drips`.
  Investigate the watchdog/consolidation interaction instead of changing their
  expectations.
- Orca account inspection was attempted with `orca account list --json` and
  produced no account/rate-limit records.  Updating the worktree card was also
  attempted, but the local Orca runtime was unavailable; no restart was
  attempted.
- Git worktree had no tracked local modifications when this checkpoint began.
  The copied plan and this status file are the only intended untracked files.

## Completed milestones

None.  This is a resource-safety checkpoint, not a claim that any phase is
implemented.

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

Then inspect the existing worker/router/checkpoint contracts, add focused tests
before each implementation increment, run relevant lint/type checks available
in `pyproject.toml`, and run the full suite only after targeted tests pass.

## Migration and rollback guidance

No migration was created.  When Phase 1 starts, create a versioned SQLite
migration ledger with backup-before-upgrade and a tested read-compatible
rollback path.  Keep the existing markdown/transcript data authoritative until
import is proven; do not delete or reinterpret legacy checkpoints as fully
covered conversation history.  Each durable outbox/run schema change should be
additive and preserve pending records across downgrade/restart.

## Unresolved blockers

- Resource budget must be restored to at least 20% for both relevant limits
  before implementation/testing resumes.
- The two baseline periodic-flush regressions require diagnosis.
- The local Orca runtime was unavailable for worktree-status updates; this is
  non-blocking for code work once resources permit.
