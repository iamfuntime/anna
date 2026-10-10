# Anna reliability implementation status

## Checkpoint: Phase 0 routing and checkpoint safety milestone

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
- The original watchdog regression is fixed and the pending focused rerun is
  green (43 passed).
- Coordinator verified the full suite at `3390c54`: **1,392 passed, 1 skipped,
  1 warning** in 31.29 seconds.
- The user-supplied copied plan is intentionally untracked until this
  documentation milestone commit, where it will be included.

## Verified Phase 0 changes

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
watchdog's hard-threshold flush. The direct watchdog tests now explicitly
select text/incremental delivery. The pending rerun is green:

```text
ANNA_HOME="$(mktemp -d)" uv run --extra dev pytest -q \
  tests/test_worker_periodic_flush.py tests/test_turn_watchdog.py
43 passed
```

The continuation added these tested safety fixes:

- Per-event reply addresses are captured before owner aliasing. A shared
  `user:<canonical>` worker routes final, periodic, and watchdog sends to the
  transport and conversation key that supplied the queued event, rather than
  retaining the first channel callback.
- Checkpoint files use unique IDs and atomic replacement. Same-clock-tick
  writes cannot overwrite one another, and readers cannot observe a partial
  markdown file.
- Failed/empty closeout summary attempts are audited but are not written as
  fake summaries. Resume no longer treats checkpoint mtime or bounded excerpt
  output as a coverage cursor; it replays the bounded transcript tail. This
  can repeat context, but it cannot silently hide transcript events. Exact
  event-ID coverage remains follow-up work.

Targeted validation after these changes:

```text
uv run --extra dev pytest -q \
  tests/test_checkpoint.py tests/test_transcript_resume.py \
  tests/test_worker_periodic_checkpoint.py tests/test_worker_resume_context.py \
  tests/test_worker_closeout.py tests/test_router_identity_alias.py \
  tests/test_transport_outbound_alias.py tests/test_worker_periodic_flush.py \
  tests/test_turn_watchdog.py
114 passed
```

`ruff check` was run on the touched files. It reports pre-existing strict-lint
findings in these long-lived modules (including old import/UTC annotations);
the new route callback closure and unused local finding were corrected. No
full suite has been rerun yet.

## Completed milestones

No phase exit is complete. Watchdog behavior, event-addressed replies, and
basic checkpoint write/replay safety are complete slices; receiver liveness,
safe Google isolation, exact checkpoint event coverage, and later phases
remain.

## Continuation after review

Independent read-only review of `e7a7015..3390c54` reported an idle-send
regression and unsafe shared callback mutation. Commit `750c17d` fixes both by
using task-local reply addresses; it also makes Slack/Telegram startup fail if
their receiver task dies immediately and makes health fail for stopped receiver
tasks. The reviewer terminal was confirmed idle and closed.

Commit `48f9118` adds a SQLite WAL-backed fact store with a migration ledger,
FTS5 retrieval, owner/scope filtering, provenance event IDs, correction
supersession, and soft forgetting. Workers refresh matching scoped facts per
turn. The first cross-transport policy correction also uses the event transport
for voice-only/drip eligibility. Explicit memory MCP tools, extraction jobs,
legacy import, and UI remain unfinished.

Focused continuation validation:

```text
tests/test_router_identity_alias.py tests/test_transport_token_missing.py
tests/test_worker_periodic_flush.py tests/test_turn_watchdog.py
69 passed

tests/test_memory.py tests/test_worker_tool_wiring.py
tests/test_worker_periodic_flush.py tests/test_router_identity_alias.py
60 passed
```

Migration/rollback: `state/memory.sqlite3` is created automatically with a
version-1 ledger and WAL mode. It is additive; rollback is to stop using the
store while retaining the database and transcripts. Use a temporary `ANNA_HOME`
in tests; no real provider or credential action is required.

## Deferred milestones

1. **Phase 0:** finish exact event coverage/replay, receiver liveness, and
   synchronous Google-call isolation.
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

The current isolated regression command is:

```bash
ANNA_HOME="$(mktemp -d)" uv run --extra dev pytest -q \
  tests/test_worker_periodic_flush.py tests/test_turn_watchdog.py
```

Next add receiver startup/liveness tests and safe per-account Google execution
isolation. Run relevant lint/type checks, then the full suite only after the
remaining Phase 0 targeted tests pass.

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

- Exact checkpoint coverage is deliberately conservative for now: legacy and
  bounded checkpoints do not suppress transcript replay.
- Receiver readiness/liveness and Google `.execute()` thread isolation are
  only partially implemented: receiver liveness is covered, safe Google call
  isolation remains.
- Memory persistence/retrieval is started, but explicit tools, extraction,
  source import, durable tasks/runs/outbox, heartbeat, and repair UX remain.
