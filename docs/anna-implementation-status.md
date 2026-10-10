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
legacy import (both now done below), and UI remain.

Commit `b07ea37` mounts `anna_memory` in every worker with explicit
`memory_remember`, `memory_search`, `memory_correct`, and `memory_forget`
tools. Successful remember responses are emitted only after the SQLite commit;
the current inbound provider event ID is stored as provenance. Corrections and
forget operations refuse facts outside the caller's owner/scope; restart and
private-group isolation are covered by `tests/test_memory.py`. Commit
`e6c01cf` attaches the per-event provenance binding.

Focused continuation validation:

```text
tests/test_router_identity_alias.py tests/test_transport_token_missing.py
tests/test_worker_periodic_flush.py tests/test_turn_watchdog.py
69 passed

tests/test_memory.py tests/test_worker_tool_wiring.py
tests/test_worker_periodic_flush.py tests/test_router_identity_alias.py
60 passed
```

Full validation after `eb0fef0`:

```text
uv run --extra dev pytest -q
1,395 passed, 1 skipped, 1 warning in 26.88s
```

The warning is the existing Starlette `TestClient`/httpx deprecation warning.

## Exact transcript/checkpoint coverage follow-up

Commit `8e458f1` implements this coverage contract.
The former mtime-based resume heuristic has been replaced with durable
per-conversation transcript records. Each newly appended inbound or outbound
JSONL event has an opaque `event_id` and a monotonically allocated `sequence`;
the small `.cursor.json` allocation hint is atomically replaced, while JSONL
is authoritative and repairs the hint after a mid-write crash. Sequence order
survives daily rotation and copied checkpoint mtimes.

Only a successful closeout summary can carry `coverage_version: 1`,
`coverage_end_sequence`, and `coverage_end_event_id` frontmatter. Periodic
mechanical excerpts, failed/missing summaries, malformed coverage metadata,
and legacy checkpoints have no coverage and therefore suppress no raw replay.
The worker advances a candidate only after a normally completed inbound turn;
if a newly spawned worker finds older uncovered events, it declines to advance
coverage rather than hiding them. Inbound events journaled while closeout is
stopping but not processed have higher sequences and remain replayable.

Focused isolated validation:

```text
uv run --extra dev pytest -q tests/test_transcript_coverage.py \
  tests/test_checkpoint.py tests/test_transcript_resume.py \
  tests/test_worker_resume_context.py tests/test_worker_periodic_checkpoint.py \
  tests/test_worker_closeout.py tests/test_router_identity_alias.py
73 passed in 0.58s

uv run --extra dev pytest -q
1,410 passed, 1 skipped, 1 warning in 22.57s
```

Import-order lint is clean for the touched code. The project-wide strict lint
still reports existing UTC-annotation, legacy typing, and unrelated router
findings in long-lived modules; no broad formatting-only rewrite was made.

Migration/rollback: this is an additive transcript/checkpoint format change,
not a SQLite migration. Existing JSONL and markdown remain readable. Rollback
is safe because older builds ignore the new JSONL fields and checkpoint
frontmatter; newer builds treat legacy files as unvalidated and replay their
bounded raw tail. Isolated manual acceptance: set `ANNA_HOME=$(mktemp -d)`,
send two mocked events, stop/restart the worker, verify the JSONL event IDs and
sequences are stable, then copy/touch the checkpoint and verify only explicit
coverage—not its mtime—changes the recovered tail.

Full validation after the memory MCP additions:

```text
uv run --extra dev pytest -q
1,397 passed, 1 skipped, 1 warning in 22.67s
```

Google isolation was strengthened after review of the initial executor design.
Commit `6b7300d` contains this bounded per-account ownership follow-up.
One single-worker owner lane is now allocated only after a configured account
slug is validated, and that lane is shared by Gmail and Calendar for the same
account. Discovery, OAuth refresh, request construction, cached resources,
and synchronous `execute()` therefore remain on that account owner thread;
different accounts can still progress in parallel. Each lane accepts at most
two calls (one running and one asyncio-queued), so the executor's unbounded
submission queue is not exposed. A caller deadline returns a timeout without
claiming an uncertain remote result, but intentionally retains lane ownership
until the already-running synchronous call exits; Python cannot safely kill a
blocked provider thread.

`httplib2.Http(timeout=30)` is newly owned per account/API resource and wrapped
in `google_auth_httplib2.AuthorizedHttp`, so discovery, API I/O, and automatic
credential refresh receive the bounded transport. Explicit OAuth refresh also
wraps its request callable with the same deadline. Refreshed and newly
authorized OAuth token JSON is atomically replaced with mode `0600`. Public
synchronous `gmail()`/`calendar()` access now rejects rather than leaking a
thread-owned transport. Invalidation records an owner-thread eviction; close
rejects new calls and queues one owner-thread cached-resource cleanup after any
in-flight I/O. `wait=False` remains non-blocking and cannot forcibly terminate
an existing socket operation.

Focused validation for this follow-up was:

```text
uv run --extra dev pytest -q tests/test_google_clients_async.py \
  tests/test_google_auth.py tests/test_google_server.py tests/test_worker_tool_wiring.py
69 passed in 0.82s

uv run --extra dev ruff check src/anna/tools/google_clients.py \
  src/anna/tools/google_auth.py tests/test_google_clients_async.py \
  tests/test_google_auth.py
All checks passed
```

The focused regressions cover heartbeat progress, same-account Gmail/Calendar
serialization, cross-account progress, bounded queue rejection, cancelled
queued calls, timeout ownership retention, configured HTTP timeout, unknown
slug rejection before allocation, and invalidate/close during in-flight work.
Targeted `mypy` still reports pre-existing missing third-party Google typing
metadata and existing Any-return warnings in `google_auth.py`; no dependency
or ignore was added just to silence them.

Full isolated validation after this follow-up:

```text
uv run --extra dev pytest -q
1,405 passed, 1 skipped, 1 warning in 22.61s
```

The warning is the existing Starlette `TestClient`/httpx deprecation warning.

Migration/rollback: `state/memory.sqlite3` is created automatically with a
version-1 ledger and WAL mode. It is additive; rollback is to stop using the
store while retaining the database and transcripts. Use a temporary `ANNA_HOME`
in tests; no real provider or credential action is required.

## Durable background memory extraction and legacy import

Opt-in (`memory.extraction.enabled`, default **false**). After an interactive,
non-ephemeral turn the worker durably enqueues only the *user's own message*
into `state/memory.sqlite3` (`extraction_jobs`, idempotent per
owner+scope+source event, queue cap). The enqueue runs after the reply path and
any failure is logged, audited (`audit.memory.enqueue_failed`) and counted in
`worker.memory_enqueue_failures`; it never fails the reply. A daemon-level
`MemoryExtractionRunner` claims jobs with a lease (expired claims are reclaimed
after a crash/restart; stale claimants cannot commit), applies bounded
concurrency, a per-day job cap, per-job retry with exponential backoff
(`max_attempts`, then `dead`) and global backoff after repeated failures.

- **Extractor isolation:** `SdkMemoryExtractor` uses the existing Agent SDK in a
  single turn with `tools=[]`, no MCP/skills/plugins/hooks/settings,
  `max_turns=1`, `max_budget_usd`, and no `fallback_model`; a non-text block is
  a violation. Tests use a fake extractor; no model call is made in the suite.
- **Billing/trust:** off unless enabled; with `auth.mode: api_key` it also needs
  `memory.extraction.allow_api_key_billing: true`, otherwise the runner logs
  `anna.memory_extraction.blocked` and does not start.
- **Schema:** strict `{"candidates":[{kind,subject,content,evidence,source,confidence}]}`;
  schema violations are malformed → retry (bounded). Semantics are recorded, not
  retried: only `source=user_stated`, verbatim `evidence` present in the source,
  `confidence >= min_confidence`, non-instruction-like candidates are stored
  (`origin=auto_extracted`, evidence + source event id + owner/scope kept).
  `user_inferred`/`assistant`/`tool`/`instruction` are rejected; assistant and
  tool output is never sent to the extractor.
- **Protections:** a candidate differing from an active fact on the same subject
  is a `conflict` candidate (never overrides; use `memory_correct`); wording
  that was forgotten or superseded, or a subject that was forgotten, is `held`
  and cannot resurrect. Forgetting purges retained evidence. Source text is
  purged when a job finishes. Explicit `memory_remember` is unchanged and
  synchronously committed; an extraction for the same event defers to it.
- **Legacy import:** `anna-admin memory-import PATH --owner O --scope S
  [--kind K] [--recursive] [--apply]`: dry-run by default; bullets from a
  MEMORY.md-style file or a directory of `.md` notes (non-recursive by default,
  no transcripts); owner and scope are mandatory, group scope requires
  owner == conversation key (no fabricated user owner); idempotent ledger
  (`legacy_imports`), provenance `legacy:<file>#<hash>`, never edits/deletes the
  source, never revives forgotten facts; DB backup before `--apply`.
- **Migration:** memory schema v2 is additive (fact provenance columns,
  `extraction_jobs`, `memory_candidates`, `legacy_imports`); an existing v1 DB
  is copied to `memory.sqlite3.bak-v1-<ts>` first. Rollback: stop enabling
  extraction and keep/restore the backup; v1 code ignores the added columns.
- **Fixes found on the way:** per-turn retrieval passed raw user text to FTS5
  MATCH (punctuation like `?` raised and silently disabled retrieval) — now
  tokenized to a safe OR-query; and the cadence reminder overwrote the
  `<durable-memory>` block on Slack/Telegram — now preserved.

Commit `49a4fb6`. Validation: `uv run --extra dev pytest -q` → 1,455 passed, 1 skipped (45 new in
`tests/test_memory_extraction.py`); `ruff` clean on new files, no new findings
in touched files; `mypy` clean on the new/changed memory modules. Still
deferred: operator UI/CLI to review/accept `conflict`/`held` candidates, any
live-model quality evaluation of the extraction prompt, and shared-task state.

## Durable scheduler ledger (occurrences, runs, outbox)

`schedules.yaml` still holds the schedule *definitions* (and a mirrored legacy
`state` the dashboard reads). Execution truth now lives in
`state/runtime.sqlite3` (`RuntimeLedger`, WAL, `ledger_migrations`), written
only by the daemon:

| table | purpose |
| --- | --- |
| `schedule_cursors` | last materialized tick per schedule (seeded from legacy `state.last_fired_at`/`created_at`) |
| `schedule_occurrences` | `UNIQUE(schedule_id, scheduled_for)`; `exec_status` (pending, claimed, running, succeeded, failed, timeout, cancelled, unknown, blocked, skipped_overlap, skipped_misfire) separate from `delivery_status` (none, suppressed, pending, partial, delivered, failed, uncertain, cancelled); lease owner/expiry |
| `schedule_runs` | one row per attempt: status, `side_effects` (none/possible/unknown), typed `JobResult` JSON |
| `schedule_outbox` | one row per destination chunk: attempts, `next_attempt_at`, `provider_outcome`, status (pending, sending, sent, failed, uncertain, cancelled) |

Flow per poll: reload YAML (picks up dashboard edits) -> recover expired leases
-> reconcile schedules deleted by another process -> materialize due ticks
(occurrence + cursor advance in one transaction; no optimistic `last_fired`) ->
claim and run (lease = 2*timeout + `lease_grace_seconds`) -> run result and
outbox rows commit in ONE transaction -> deliver outbox.

- **Misfire:** `run_latest` (default; legacy behavior) runs only the newest due
  tick after downtime and records older ones `skipped_misfire` (bounded);
  `skip_late` also skips it if later than `misfire_grace_seconds`. Cron ticks are
  never replayed. **Overlap:** `skip` (default; records `skipped_overlap`) or
  `queue` (one waiter). Per-schedule overrides: `misfire_policy`,
  `misfire_grace_seconds`, `overlap_policy`; defaults in `scheduler.*`.
- **Recovery (lease expiry):** claimed-not-started -> re-pended (nothing ran);
  running + `run_safety: read_only` -> retried up to `max_run_attempts`;
  running + `side_effecting` (default, all legacy agent prompts) -> `unknown`,
  counted as a failure, **never re-run**. A send in flight at crash/lease expiry
  -> `uncertain`, **never auto-resent**.
- **Delivery vs execution:** delivery retries touch only outbox rows with
  backoff (`delivery_*` settings); chunked at `delivery_chunk_chars`, chunk i+1
  only after chunk i is sent. Only errors that prove "not sent" (connection
  errors, HTTP 429, `retry_safe`, missing adapter) retry; other send errors are
  `uncertain` (opt-in `retry_unknown_send_errors`). Terminal delivery
  failure/uncertainty counts toward `failure_threshold` and audits.
- **Cancellation:** `InboundEvent.cancel` reaches the worker. Timeout, schedule
  deletion (same process via callback, other process via poll reconcile) and
  shutdown set it; the worker drops a queued turn or calls the SDK
  `interrupt()` on a running one and reports `turn_meta["turn_state"]`. Verdict:
  dropped/no tools -> `timeout`/`cancelled` (`side_effects: none`); tools ran ->
  `unknown`; worker did not acknowledge within `cancel_grace_seconds` ->
  `blocked`. Unsent output of a deleted schedule is dropped.
- **Typed jobs:** `Schedule.job_kind` (only `agent_prompt` today) dispatches
  through `Scheduler._run_job` to a `JobResult(outcome ok|quiet|failed, output,
  data, tool_call_count, side_effects)` - the seam for the deterministic
  heartbeat. Legacy quiet/blank/markup/no-tool-call heuristics stay inside the
  agent-prompt job.
- **`schedules.yaml` safety:** every mutation is a read-modify-write under the
  in-process lock plus an `flock` on `schedules.yaml.lock`, re-reading the file
  first, so the dashboard and daemon no longer overwrite each other's rows or
  fields. New optional Schedule fields default, so old files load unchanged.

Migration/rollback: first ledger creation copies `schedules.yaml` to
`schedules.yaml.bak-pre-ledger-<ts>` (once). The ledger is new and additive;
rollback = run the previous build: it ignores the ledger and the new YAML
fields (it will resume from the mirrored `state.last_fired_at`). Keep
`runtime.sqlite3` for forensics; ledger upgrades use the same backup-before-
migrate pattern as the memory store (`RuntimeLedger.backup`).

Validation: full suite 1,482 passed, 1 skipped (adds `tests/test_scheduler_ledger.py`,
24 tests: uniqueness/no replay, misfire, overlap, crash after claim, lease-expiry
unknown vs read-only retry, timeout/cancel propagation incl. real worker drop and
interrupt, send failure + restart retaining outbox, uncertain sends, chunk retry,
delete cancel incl. cross-process, store no-clobber, YAML backup/legacy load).
Manual: a subprocess killed mid-run (`os._exit`) left a running claim; a second
process recovered it as `unknown` without re-running. `ruff`/`mypy`: no new
finding categories in touched legacy files; new files clean. Not done: dashboard
view of occurrences/outbox, operator command to resolve `unknown`/`uncertain`
rows, the heartbeat job itself.

## Review fixes (foundation review `10ff5ba`)

Source: `/tmp/anna-foundation-review-10ff5ba.md`. Regressions: `tests/test_review_fixes.py`
(27 tests; 23 fail against the pre-fix tree, the 4 that pass pre-fix are guard/hardening
checks: H2 router-event shape, M6 stale-cursor, M6 early-stop tail read, L4 claim recheck).

| finding | status | fix |
| --- | --- | --- |
| H1 outbound split across dirs | fixed | `OutboundMessage.transcript_conversation_key`; worker sets its canonical key, router transcribes there; delivery address unchanged |
| H2 sub-agent text extracted | fixed | positive allow flag `InboundEvent.human_origin`, set only by the Slack/Telegram/CLI adapters (plus DM-only, interactive, writable-scope); recalled facts are escaped, fenced and labelled untrusted data (`render_memory_block`) |
| M1 coverage frozen | fixed | closeout coverage extends over the replies that follow the last processed inbound (`extend_coverage_through_outbound`, stops at the next inbound so queued messages stay uncovered); events shown in the session's own resume tail count as seen, so one failed closeout no longer freezes coverage |
| M2 coverage over failed turn | fixed | a query/receive failure freezes coverage for that worker; later turns cannot hide the unseen one (the next worker's resume tail shows it) |
| M3 correct revive/fork | fixed | `correct` is one `BEGIN IMMEDIATE` transaction, only for an ACTIVE fact in the caller's owner/scope; forgotten/superseded rejected (`FactNotActive`) |
| M4 delayed resurrection | fixed | explicit and extraction use `_event_source_id` (transcript event id first); any candidate from a source event whose fact was forgotten/superseded is held; an active explicit fact from the same event makes the candidate a duplicate, regardless of wording |
| M5 unreachable remember | fixed | schedule / one-shot / ephemeral workers return an explicit "nothing was saved" error for remember/correct/forget (no fake success) |
| M6 per-message history scan | fixed | sequence allocation trusts the cursor plus a bounded tail read of the newest day files; full scan only when the cursor is missing and is per-line tolerant (a malformed line no longer resets sequences); tail reads go newest-day-first and stop when the budget is full; coverage validation scans newest first |
| L1 weak evidence | fixed | quote must be >= 10 chars and share a 5-char stem with subject/content, else `insufficient_evidence` |
| L2 billing env | fixed | extraction refuses to start when `ANTHROPIC_API_KEY`/`AUTH_TOKEN`/Bedrock/Vertex/Foundry selectors are in the environment unless `allow_api_key_billing`; in max mode the extractor also blanks them in its CLI env. Not verified against the live CLI (no credentials used) |
| L3 existence oracle | fixed | unknown id and other-scope id return one message |
| L4 claim recheck | fixed (hardening) | `complete_extraction` runs under `BEGIN IMMEDIATE` and the final update carries the `claim_token` predicate |
| L5 connections / stalls | fixed | memory store opens lazily (ephemeral workers never open it) and is closed in `stop()`; busy timeout 1s (ledger 2s) instead of 10s |
| L6 retention of forgotten text | fixed | candidates held for forgotten/superseded wording keep no subject/content/evidence (only the dedup hash and reason) |
| new (security-review) | fixed | `state/*.sqlite3`, backups: 0600, state dir 0700 (`private_file`) |

Deferred (by the review itself): per-thread Slack scopes (design choice); paraphrase of a
forgotten fact in a LATER, different message can be re-learned (wording-based dedup
limit); real-extractor behaviour (code-fenced JSON etc.) and the extractor CLI session
files under `CLAUDE_CONFIG_DIR` need a live model/credentials and were not run.

Retention / purge boundaries (explicit): forgetting a fact deletes its stored evidence
and any retained candidate text; the source job's text is purged when the job reaches a
terminal state (a still-pending job keeps its text until it runs). NOT purged by forget:
the transcript JSONL, checkpoints and audit log (history is retained by design), the
dedup hash that blocks resurrection, and any copy the extractor's CLI session wrote.

Validation: full suite 1,509 passed, 1 skipped; ruff clean on new/changed memory files
and tests, mypy clean except pre-existing `dict` generics in `transcript_resume.py`.

## Built-in Gmail + Calendar heartbeat (`job_kind: google_heartbeat`)

Read-only, deterministic, **opt-in** (`heartbeat.enabled: false` by default). No model is
used for collection, empty checks or reminders; it dispatches no worker turn, so nothing from
mail/calendar can reach the model, memory extraction or `InboundEvent.human_origin` paths.

- **Transport:** the existing serialized `GoogleClients.run(account, api, op)` lanes only
  (busy lane -> `busy` receipt; no new keys, vendors or sync clients). Only
  `getProfile`, `messages.list/get` (metadata), `history.list`, `events.list` are called;
  the fake-provider tests fail on any other method.
- **Receipts:** every configured `(account, service)` yields a typed receipt (`ok`,
  `auth_required`, `rate_limited`, `api_error`, `busy`, `partial`, `backpressure`) with
  `checked_at`, cursor before/after, items seen/new, pages, `resynced`, `more_pending`, error
  class/message and a sha256 `receipt_hash`; stored in `heartbeat_receipts` and in the run
  result. One failing account/service never stops the others; exceptions and malformed
  provider responses are `api_error`, never an empty inbox.
- **Job outcome** (occurrence `exec_status`): `succeeded` only if every required check is `ok`
  and nothing was withheld; `partial` (some usable, some not / more pending / backpressure);
  `auth_required` (all checks need re-authorization); `failed` (otherwise, or heartbeat disabled,
  no accounts). A heartbeat is never auto-disabled by failures or delivery problems.
- **Cursors/resync:** Gmail keeps a `historyId`; none or HTTP 404 -> bounded resync (newest unread
  inbox mail of the last `gmail_bootstrap_hours`, <= `gmail_max_pages` pages; older mail past
  that cap is not alerted; message-id dedup makes it safe). Incremental history is page-bounded;
  if pages remain the cursor only advances to the last record processed (`partial`). Calendar
  uses a rolling window in the schedule timezone (a sync token cannot be combined with a window);
  dedup key = event id + `updated`; "starting soon" reminders dedup on event id + start.
- **Atomicity:** cursors, notified keys, alert state and receipts commit in the SAME transaction
  as the run result and outbox rows (`finish_run(heartbeat=...)`). Crash -> read-only check repeats
  (`run_safety: read_only` recovery), never loses or duplicates an alert. Delivery retries touch
  only the outbox; they never re-run the checks.
- **Alerts:** state transitions only: one alert on entering `auth_required`; transient failures
  (rate limit/API/busy) alert after `transient_failures_before_alert` consecutive ticks; bounded
  reminders (`reminder_interval_hours`, `max_reminders`); one "working again" notice. Quiet hours
  (schedule timezone, wrap-around aware) hold non-urgent notices via the outbox's `next_attempt_at`;
  reminders within `urgent_minutes` bypass them. If pending notifications exceed
  `max_pending_notifications`, new items/alerts are withheld **without** advancing cursors or
  marking items notified (status `backpressure`/`partial`) and re-read later.
- **Optional classifier:** a `Classifier` protocol (`annotate(items)->labels`) can be injected into
  `Scheduler(heartbeat_classifier=...)`; it runs only on already-collected candidates, is
  time-bounded and failure-isolated, and can never affect receipts or status. No SDK-backed
  implementation is shipped (deferred).
- **Registration:** `Schedule.job_kind` accepts `google_heartbeat` (forces `run_safety: read_only`);
  `schedule_create` takes optional `job_kind` (prompt ignored); the store refuses to create one
  unless `heartbeat.enabled`, `google.enabled` and an account exist. Legacy agent jobs unchanged.
- **Migration/rollback:** ledger schema v2 (additive tables `heartbeat_state`, `heartbeat_notified`,
  `heartbeat_receipts`; `schedule_outbox` rebuilt with a per-message `msg_index` so one occurrence
  can carry several independent messages). An existing ledger is copied to
  `runtime.sqlite3.bak-v1-<ts>` first. Rollback: set `heartbeat.enabled: false`, delete heartbeat
  schedules, and restore the backup if reverting the code (the older build ignores the new tables
  but cannot read the rebuilt outbox column set safely - restore the backup rather than mixing).
- **Operator setup (isolated, no live provider needed to validate):**
  1. `ANNA_HOME=$(mktemp -d) uv run --extra dev pytest -q tests/test_heartbeat.py` (fake provider).
  2. Real accounts: `google.enabled: true`, accounts added with
     `python -m anna.setup.google_auth add|verify <slug>` (existing flow).
  3. `heartbeat.enabled: true`, restart; create a schedule with `job_kind: google_heartbeat`
     (e.g. `*/15 * * * *`) via `schedule_create` or `schedules.yaml`; the config block in
     `anna.yaml.example` documents every knob.

Validation: full suite 1,547 passed, 1 skipped (`tests/test_heartbeat.py`: 38 tests); ruff clean on
new files; mypy clean except the existing missing `croniter` stubs. Not exercised: any live
Google account (by design). Deferred: SDK-backed classifier, dashboard view of receipts, Gmail
push/watch, calendar sync tokens.

## Phase 3: `anna doctor`, health/repairs/memory dashboard pages

**Doctor** (`anna doctor [--json]`, also `/health` and `/health.json`): offline and read-only. It
reads only local files: the daemon's status snapshot (`state/runtime-status.json`, new,
0600, atomic), a *copy* of `runtime.sqlite3` / `memory.sqlite3` (copying avoids the WAL side
files even a read-only SQLite connection would create next to the originals) and config. It never
reads secret values (only "is variable X set"), never calls Google/Slack/Telegram, never runs a
model, and never creates or modifies anything unless the operator uses an explicit repair.
Exit code: 0 ok, 1 warn or unverified, 2 failure. Statuses: `ok` (observed working), `warn`,
`FAIL`, `??` (not verified; **never assumed healthy**), `--` (disabled).

| area | source of truth | honest rules |
| --- | --- | --- |
| daemon | snapshot age + pid | missing = `??`; dead pid without clean stop = FAIL; stale snapshot = FAIL (hung) |
| transports | watchdog records `receiver_alive` and `api_ok` separately (`health_detail()`) | config-enabled + creds present but never verified = `??`; **dead receiver with a valid token = FAIL ("receiver is NOT running")**; stale check = warn |
| Claude SDK | worker records the last REAL completed turn / failed turn | untested = `??`; failure newer than success = FAIL; success older than 24h = warn (stale). The watchdog's "construct a client" check is reported as not verifying credentials |
| Google per account/service | latest `heartbeat_receipts` row | no receipt = `??` (listing in config proves nothing); `auth_required` = FAIL with the setup command; stale = warn |
| heartbeat | latest finished `google_heartbeat` occurrence | complete / PARTIAL / FAILED / auth required, with age |
| runs | ledger occurrences | unknown/blocked runs = warn with "never re-run automatically"; runs past lease; queue ages |
| deliveries | ledger outbox | pending age, uncertain/failed counts and oldest age; message bodies never shown |
| memory | memory DB + runner state | active/forgotten/superseded counts, extraction queue (pending/running/dead + redacted recent errors), conflict/held candidates; "enabled in config but runner NOT running" is a warn with the reason |

**Dashboard** (`/health`, `/repairs`, `/memory`; localhost-only unchanged, `SameOriginMiddleware`
unchanged and covering every new POST; the surface-contract test pins the three new mutating routes):
- `/repairs` lists unknown/blocked runs and uncertain/failed deliveries (ids, ages, sizes, destinations; no bodies).
  Actions: runs - verified completed / verified nothing happened / cannot tell (closed as
  `resolved_unknown`); uncertain deliveries - verified delivered / verified NOT delivered, resend /
  abandon; failed deliveries - retry / abandon. There is no rerun and no blind resend. Each needs the
  exact typed phrase (e.g. `RESEND OUTBOX 12`) and pins the status the operator saw.
- **Daemon-owned mutation:** the dashboard only INSERTs an intent into `repair_requests`
  (ledger schema v3, additive); the scheduler poll in the daemon validates it against the row's
  CURRENT status and applies it (`RuntimeLedger.apply_repairs`, audited as
  `audit.schedule.repair_applied`). Stale or illegal intents are rejected with a reason; a stopped
  daemon leaves requests visibly waiting ("restart the daemon").
- `/memory` requires an explicit owner + scope selection (never lists across scopes) and calls
  `MemoryStore.correct/forget` with that owner/scope: same `BEGIN IMMEDIATE` invariants (active facts
  only, atomic supersede, no revive/fork). This is safe SQLite coordination, not a competing
  scheduler writer. Audit rows carry fact ids and scope only, never fact text.

Isolated setup: `ANNA_HOME=$(mktemp -d) uv run --extra dev pytest -q tests/test_doctor.py tests/test_web_ops.py`;
real use: restart the daemon once (it writes the status file and migrates the ledger to v3 after
backing it up as `runtime.sqlite3.bak-v2-<ts>`), then `anna doctor` / open `/health`.
Rollback: stop using the pages; the new tables/file are additive and ignored by older builds
(restore the ledger backup if reverting the code; `runtime-status.json` can simply be deleted).

Review fixes folded in here (heartbeat commit `cf72508`): heartbeat output goes only to destinations in
`heartbeat.allowed_destinations` (checked on create, update and every run - an agent tool cannot redirect
mail summaries), Slack control syntax in untrusted mail text is escaped, and heartbeat messages are
sent `plain=True` so the Slack adapter's Markdown converter cannot turn `[x](url)` in a subject into a
live link. The review notice listed a third finding that was truncated; it could not be addressed.

Validation: full suite 1,589 passed, 1 skipped; ruff clean on new files; mypy clean on new modules.
Not done (by scope): Google connect/reconnect UI, live provider verification, dashboard polling of
the status file for push updates.

## Deferred milestones

1. **Phase 0:** finish exact event coverage/replay.
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

Next implement exact transcript event coverage/replay. Run relevant lint/type
checks, then the full suite after its targeted tests pass.

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
  implemented for the current read-only Google tools. Google shutdown cannot
  make a hung third-party socket exactly-once or forcibly stop its thread; it
  bounds accepted work, preserves serialization, and cleans up when it exits.
- Memory persistence/retrieval and explicit fact tools are implemented, but
  extraction jobs, source import, durable tasks/runs/outbox, heartbeat, and
  repair UX remain.
