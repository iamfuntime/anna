# Anna: reliable personal-agent plan

## Recommendation

Keep the Claude Agent SDK, Python/asyncio, Telegram, Slack, and local dashboard. Do not start with a rewrite or more elaborate prompts. Add a small, durable runtime around the agent so remembering, checking accounts, finishing scheduled work, and delivering results are enforced by code.

The product goal is: **one assistant, persistent knowledge, predictable background work, and recoverable connections**. The model decides what information means; the runtime owns whether work actually happened.

This review uses the repository, not the running installation. No live accounts, credentials, or production logs were inspected. The comparison target is xAI's [Introducing Grok Bot](https://x.ai/news/introducing-grok-bot) announcement. Its descriptions are vendor claims, not independently tested reliability or disclosures of its internal architecture.

## What the Grok Bot comparison changes

The announcement describes more than a chatbot with cron: always-on agents with their own cloud computer, persistent conversations, learned routines, parallel specialists, and end-to-end work with approval requests when needed.

| Grok Bot's stated behavior | Anna today | Proposed equivalent |
|---|---|---|
| Remembers conversations and learns preferences | Checkpoints, core memory, and model-selected file searches | Automatically retrieved facts, provenance, corrections, and task history |
| Takes work by message, without manually building workflows first | Chat, scheduling tools, skills, and delegation primitives | Chat creates a durable task or routine with a clear completion contract |
| Keeps working when you step away | A systemd daemon, on a host that must remain awake and connected | Durable task recovery on an always-on host; optionally an isolated remote runner |
| Finishes work inside actual apps | Google read-only tools, filesystem tools, and MCP integrations | API-first execution; persistent browser sessions for selected API-less workflows |
| Learns a demonstrated workflow and accepts corrections | Editable skill files, but no demonstrated-workflow learning pipeline | Versioned routines with examples, permissions, tests, and correction history |
| Specialists coordinate without making the user the middleman | Delegation with bounded grants and background completion | Anna owns durable tasks; specialists report artifacts and blockers through task state |
| Follows up and knows when to interrupt | Prompt-based schedules and quiet-output suppression | Explicit commitments, due dates, follow-up rules, quiet hours, and approval gates |

**Priority change:** durable task ownership belongs alongside memory and heartbeat, not after adding more tools. A reliable Anna should remember both “how I prefer this done” and “what she owes me.” Browser/computer execution and demonstrated routines are later extensions once the reliability foundation passes.

### Product contract: hand off work, do not supervise a chat session

A message such as “Check both inboxes every morning and flag anything I owe a reply to” should create or update a visible routine. Anna asks only for missing account access, timezone, or ambiguous policy—not for YAML edits or cron expressions.

Every task has an owner, goal, required checks, permissions, destination, due time, and completion evidence. Proposed states: `queued`, `running`, `waiting_for_approval`, `blocked`, `completed`, `failed`, and `cancelled`. Scheduled occurrences are execution runs attached to that task/routine. Conversation-worker eviction must not terminate ownership of the task.

“Done” requires evidence appropriate to the job: checked-account receipts for triage, a saved draft ID for a draft, or a verified application record for an update. Model prose alone is not evidence. Persist artifacts, blockers, and the next action so Anna can resume after restart or answer “What are you working on?” accurately.

### Progressive autonomy and learned routines

Start with read/check/summarize, then prepare drafts, then approved execution. Sending mail, modifying records, purchases, and deletion require explicit scoped policy or approval. Approval is bound to a specific action, account, and payload with an expiry; changed content requires new approval. An approval must survive restart without permitting duplicate execution.

For “Do it this way next time,” save a versioned routine containing inputs, steps, expected outputs, exceptions, examples, and permissions. Corrections update a proposed version, with preview/dry-run and confirmation before activation. Learning a workflow must never silently widen its permissions.

Watching the user demonstrate a workflow would need a separate, consented capture mechanism. Begin with descriptions and examples in chat; add browser recording later, with redaction and no credential capture. The announcement does not establish which recording mechanism Grok Bot uses.

### A computer of her own: optional, not a prerequisite for memory

Run the daemon on an always-on host if it must work while the user's laptop sleeps. For selected websites without suitable APIs, add an isolated browser runner with persistent, encrypted-at-rest profile storage, restricted network/filesystem access, per-account sessions, and action receipts. Keep it separate from the user's daily browser and desktop.

Interactive website login can reduce API-setup friction for some workflows, but browser sessions expire, MFA can require the user, and UI changes can break automation. Browser access is not a way to bypass Google OAuth policy or organizational restrictions. Prefer the existing Gmail/Calendar APIs for dependable polling; evaluate browser access only as an explicit, supported fallback. Never ask users to send passwords or session cookies through Telegram or Slack.

### Specialist coordination without unnecessary complexity

Keep Anna as the single coordinator initially. Specialists take bounded task assignments and return structured results; Anna owns follow-through and delivery. Share task/project context only within granted scopes, not all private conversation history. Build durable handoff and blocker tracking before arbitrary agent-to-agent chat or multi-agent group conversations.

Acceptance scenario: ask for a recurring two-account briefing in Telegram, approve access in the dashboard, then ask in Slack what is pending. Restart Anna mid-run. She recovers the task, reports which checks completed, remembers briefing preferences, and delivers a single final briefing to the chosen destination without repeating previously verified side effects.

## What already works as a foundation

- Channel adapters and per-conversation SDK workers already exist.
- Inbound/outbound transcripts, checkpoint resume, periodic checkpoints, audit trails, and supervisor locks exist.
- Scheduling includes concurrency limits, timeouts, failure accounting, and cancellation on deletion.
- Google supports multiple accounts, OAuth refresh tokens, and read-only Gmail/Calendar tools.
- A localhost dashboard and setup wizard already exist.
- Sub-agent grants separate operator policy from agent-selected capabilities.

Validation: `uv run --extra dev pytest -q` on Python 3.13.15 produced **1,388 passed, 2 failed, 1 skipped**. Both failures are in `tests/test_worker_periodic_flush.py`: `test_voice_only_never_drips` and `test_disabled_interval_never_drips`. These show conflicting expectations about consolidation during long turns; investigate watchdog versus drip behavior before changing tests. Unit-test success does not establish live channel/account reliability.

## Findings grounded in the code

### 1. Conversation continuity is not durable, automatically retrieved memory

`src/anna/runtime/worker.py:_assemble_system_prompt` loads core files and two checkpoints for the current conversation at worker startup. `core/MEMORY.md` is included, but adding facts depends on the model calling `memory_md_append`, as instructed by `src/anna/core_files/CLAUDE.md`.

The prompt mentions nightly `memory-curator` maintenance of Daily/Episodic/Topics notes. The repository does not ship an automatic curator pipeline or installer-created curator schedule. `scripts/seed_vault.sh` creates directories; it does not implement extraction or retrieval. A deployment could have externally created skills and schedules, but that cannot be verified here.

Consequences:

- Saving a conversation does not guarantee that a durable preference is extracted.
- Older and cross-conversation knowledge is available primarily if the model elects to search files.
- Core-file changes are not automatically refreshed into already-running workers.
- Daily and topic notes are not automatically retrieved for each turn.

### 2. Checkpoint coverage can hide information that was never preserved

`worker.py:_maybe_periodic_checkpoint` creates a mechanical checkpoint from a bounded tail. `src/anna/vault/transcript_resume.py:render_tail_block` truncates individual messages to 400 characters. Resume then uses checkpoint filesystem mtime as the cutoff for transcript replay.

A long message or a turn outside the bounded window can be omitted from the checkpoint while the newer checkpoint mtime makes it look covered. `_ask_checkpoint_summary` also returns failure placeholders, and closeout writes those as normal checkpoints. A placeholder can therefore advance the replay cutoff without preserving the conversation.

`src/anna/vault/checkpoint.py:write_checkpoint` uses second-granularity filenames and direct writes. Same-second writes can collide; interrupted writes can leave partial files.

**Fix:** explicit event coverage, atomic files, and a distinction between a valid summary and a failed summary attempt. Never use file modification time as a correctness cursor.

### 3. Shared identity currently also shares a channel-bound worker

`src/anna/runtime/router.py:_normalize_conv_key` can map Slack and Telegram to the same `user:<canonical>` key. `_get_or_spawn_worker` then reuses that worker, but its send callback and visibility callbacks were bound to the first transport at creation through `_send_factory`.

Thus an aliased conversation arriving through another channel can use the first channel's reply path. This is a code-level routing defect when cross-channel aliases are enabled, not evidence that it happened in the current deployment.

**Fix:** share knowledge by person, not a transport-bound worker. Preserve an explicit reply destination for every inbound event.

### 4. Health checks do not prove the system can do useful work

- `src/anna/runtime/watchdog.py:_check_sdk_session` constructs an SDK client without connecting or querying. This does not verify authentication, model availability, or quota.
- Slack `health_check` calls `auth_test`; this does not prove Socket Mode is connected and receiving events.
- Telegram `health_check` calls `get_me`; this does not prove the polling loop is alive.
- Telegram startup creates a task for `start_polling` and logs `channel.connected` without awaiting polling readiness.

**Fix:** separate credentials/API reachability from receiver liveness, recent useful work, and readiness. A lack of incoming messages alone is not a failed receiver; use connection/poll state and task errors.

### 5. Scheduled output is judged by prose rather than a task contract

`src/anna/runtime/scheduler.py` uses model-written text, a quiet sentinel, and a tool-call-count guard. These are useful mitigations, but:

- One tool call does not establish that all accounts were checked.
- Quiet or blank output can be marked successful without verified checks.
- A no-tool reminder can be valid, unlike an account-check task.
- `_poll_once` advances persisted `last_fired_at` before the fire task runs. A crash after that write can lose an occurrence without a recoverable run record.
- `_guarded_fire` applies a timeout while awaiting the completion future; there is no explicit per-run cancellation contract here tying that timeout to stopping the underlying worker execution.
- Sending the result and executing the task are not independently persisted. A send failure should retry delivery, not repeat the entire account check or other side effects.

**Fix:** durable run records, job-type-specific requirements, verified results, and a separate delivery queue.

### 6. Google tools can block the entire event loop

`src/anna/tools/google_server.py` defines async tools but calls synchronous `.execute()` directly, including sequential metadata requests. Network stalls can delay unrelated conversations, timers, and watchdogs.

`src/anna/__main__.py` shares a `GoogleClients` instance across workers. Moving calls into threads without changing ownership would introduce unsafe concurrent use of shared httplib2 service transports.

**Fix:** per-account serialized execution with owned clients in bounded executor workers, or a properly asynchronous connector. Apply network deadlines, pagination limits, and bounded retries. Refresh-token writes need serialization and atomic replacement too.

## Target architecture

```text
Telegram / Slack / CLI
        |
        v
Inbound journal + deduplication
        |
        v
Conversation router -- explicit per-event reply destination
        |
        +--> Memory service: identity scope + retrieval + extraction
        |
        v
Claude Agent SDK runner
        |
        v
Validated task result --> durable outbound queue --> channel adapter

Scheduler --> durable job runs --> deterministic account collectors
                                   |
                                   +--> model triage / summary when needed

Supervisor --> receiver state, jobs, accounts, runner health --> dashboard
```

Use **SQLite in WAL mode** for transactional runtime state. This is a proposed dependency-free storage addition via Python's standard library, not an existing feature. Keep markdown as the human-readable memory view and JSONL for transcripts/audit. No Redis, distributed queue, or vector database is needed initially.

The daemon should own runtime mutations; the dashboard should request them through a local authenticated/permission-restricted interface rather than introducing another independent schedule writer. Add schema migrations and backup/restore commands.

## Workstream A: memory that gets used

### Memory layers

1. **Working context:** recent turns and valid conversation summaries, scoped to a conversation.
2. **Durable facts:** preferences, people, projects, commitments, and decisions, scoped to an owner or permitted group.
3. **Episodic records:** searchable conversation and task summaries with source links.
4. **Operational state:** cursors, jobs, account status, and delivery receipts. Never make this model-authored memory.

Suggested fact record:

```text
id, owner_id, visibility_scope, kind, subject, content,
source_event_ids, confidence, created_at, updated_at,
valid_from, valid_until, supersedes_id, status
```

### Required behavior

- Resolve the authenticated/authorized sender to an owner; do not infer identity from names in messages.
- Before every turn, inject a bounded set of pinned facts plus relevant records from SQLite FTS5 and recent task state. Refresh even for an already-running worker.
- Provide `memory_search`, `memory_remember`, `memory_correct`, and `memory_forget` tools.
- After turns, enqueue structured extraction. Validate schema, retain evidence, and deduplicate. Extraction failure cannot delay the reply or lose the extraction job.
- An explicit “remember this” must commit successfully before Anna claims it was remembered. Inferred facts can be background candidates.
- Corrections supersede earlier facts. Forgetting removes facts from retrieval and generated views; explain separately retained transcript/audit history and offer an explicit purge policy.
- Do not auto-promote email bodies, fetched pages, model guesses, or credentials into authoritative facts. Treat external content as untrusted evidence, not instructions.
- Nightly curation is optional compaction, not the only mechanism that saves or recalls facts.
- The dashboard exposes remembered facts, sources, and edit/delete actions. “Why do you remember that?” returns provenance.

Start with lexical retrieval, pinned preferences, and entity tags. Add embeddings only if a measured recall test shows the need; do not add a paid embedding API as a prerequisite.

### Checkpoint contract

Give transcript events stable IDs and ordered per-conversation sequence numbers. A summary records exactly which events it represents. A bounded raw excerpt is not a declaration that every earlier event is summarized. Preserve retrievability of older unsummarized events and run catch-up summarization when needed.

Write summaries atomically with unique IDs. Failed summaries leave coverage unchanged. Import existing checkpoints as legacy summaries without inventing exact event coverage.

### Acceptance

- Remember a preference in Telegram; recall it in Slack and after restart, without manual searching.
- Update it; the next turn uses the correction, including an already-live worker.
- A fact at the end of a long message remains retrievable.
- Kill the process between turns or during checkpoint creation; persisted events remain recoverable.
- Group participants cannot retrieve the owner's private account data or private memories.

## Workstream B: communication that recovers

- Keep conversation IDs channel-local; map them to shared owner IDs for private memory retrieval.
- Add a reply address: transport, channel/chat ID, thread/topic ID, and originating message ID. Use it for send and visibility instead of a worker's creation transport.
- Persist accepted inbound events with platform IDs and deduplicate before execution. Slack acknowledgment should follow durable acceptance where the adapter's protocol allows it; persist Telegram acceptance promptly and document polling-offset crash limits.
- Persist pending outbound messages separately from execution, with destination, chunk index, attempts, provider receipt, and next retry time.
- Retry transient network failures and rate limits with bounded backoff and provider `Retry-After`. Flag uncertain sends rather than promising exactly-once delivery where the provider cannot support it.
- Supervise receiver tasks and connection state. Await actual polling/connection readiness before reporting connected.
- Default to one final response and a thinking signal. Use sparse, predictable progress updates for long work; make watchdog overrides explicit and resolve the two failing consolidation tests.
- Report failures in plain language, preserve pending work, and expose a retry action.

Acceptance: alternate channels without misrouted replies; retain thread/topic destinations; recover after a connection drop; retry a failed send without re-running the task; suppress duplicate execution for repeated platform events.

## Workstream C: dependable heartbeat and schedules

Separate **runtime health** from **proactive account work**. The existing watchdog is the former; a scheduled prompt is not a complete implementation of the latter.

### Job records

```text
run_id, schedule_id, scheduled_for, job_type,
status, attempt, lease_expires_at, deadline,
required_checks, check_results, output, error
```

Enforce a unique `(schedule_id, scheduled_for)` occurrence. Claim jobs transactionally; recover expired leases after restart. Define overlap policy, timezone/DST behavior, and missed-run policy: skip, coalesce, or catch up. Avoid replaying dozens of old heartbeat ticks after downtime.

Cancellation must propagate to the worker/runner, not only the waiting future. Track unknown outcomes for external side effects; do not blindly retry non-idempotent actions.

### First built-in heartbeat

1. Enumerate enabled accounts.
2. Fetch new mail and upcoming calendar changes through deterministic collectors.
3. Store provider cursors/checkpoints and checked timestamps after successful processing. Resynchronize when a provider rejects an old cursor.
4. Deduplicate already-reported items by provider IDs and relevant change/version identifiers.
5. Use the model for importance classification and concise summaries only when there is candidate information.
6. Store a structured result, then queue notifications according to quiet hours and urgency.

Suggested result:

```json
{
  "status": "partial",
  "checks": [
    {"account": "personal", "service": "gmail", "status": "ok", "new_items": 0},
    {"account": "work", "service": "calendar", "status": "auth_required"}
  ],
  "notifications": []
}
```

“Nothing needs attention” is allowed only for completed required checks. An auth failure is `partial` or `failed`, never an empty healthy inbox. Notify once on state transition, then rate-limit reminders rather than disabling the entire heartbeat after a few transient errors.

Regular reminders can execute without an LLM or tools. More general agent jobs declare required capabilities/checks; tool-call counts are telemetry, not proof of completion. Quiet behavior is a structured `notifications: []`, not a model-generated magic string.

Acceptance: all configured checks produce receipts; revoked access is visible; one account failure does not prevent checking the others; repeated ticks do not repeat alerts; restarts recover interrupted jobs; delivery retries do not repeat execution.

## Workstream D: account connection without unnecessary ceremony

### Direct answer on Google OAuth

**Anna already uses OAuth**, in `src/anna/tools/google_auth.py:run_oauth_install_flow`, with offline access and refresh tokens. An OAuth client registration is different from an API key. Gmail private-data access needs authorization; an API key cannot replace that.

**One OAuth app/client can serve multiple accounts.** Each account grants consent and gets its own refresh token. You do not need a separate Cloud project/application per mailbox.

Workspace users can also authorize through user OAuth if organization policy permits. Domain-wide delegation/service-account keys are an optional administrator-managed path, not the default requirement for checking your own work email.

### Options

| Approach | User setup | Trade-off |
|---|---|---|
| One private desktop OAuth client | Create one Cloud project/client and enable required APIs once; connect each account in a browser | Best first step for this personal deployment; no account-data intermediary |
| Maintainer-owned distributed OAuth app | Sign in and approve scopes; no per-user Cloud setup | Maintainer owns app registration, distribution policy, consent configuration, and applicable verification obligations |
| Managed integration provider | Connect through provider UI | Potentially easier setup, but added fees, vendor dependency, and access to sensitive data; confirm Gmail support and approval obligations |
| CLI adapter such as gog | Delegate connection/tool operations to a maintained CLI | May reduce code maintenance, but does not inherently eliminate OAuth app registration; evaluate credential setup before adopting |

Recommendation: **reuse one private client now and build a first-class connect/reconnect experience**. Pursue a shared app only if distributing Anna to other people is a real goal.

### Important Google limitations

- External OAuth apps in **Testing** issue refresh tokens that expire after seven days for the scopes used here. Production publishing removes that particular testing limitation, but does not guarantee tokens never expire or eliminate verification requirements.
- `gmail.readonly` is a restricted scope. Public distribution may require verification; server-side handling of restricted data can introduce additional assessment requirements. Confirm the applicable personal-use/local-only exemptions and distribution model before promising a public one-click flow.
- Browser sign-in alone, including generic “Sign in with Google,” does not grant Gmail/Calendar access. The appropriate scopes and OAuth client still matter.
- A remote daemon cannot finish a localhost browser callback on a different machine without an explicit remote-connect design. Support documented SSH forwarding first; if adding a hosted callback, design state validation, PKCE, and secure credential delivery deliberately.

### Connection UX

Add **Accounts → Connect Google** to the existing dashboard:

1. Import/select the reusable client once.
2. Choose Gmail and/or Calendar and request only the necessary scopes.
3. Complete browser consent with state validation and PKCE where supported.
4. Verify the actual mailbox identity; fail mismatches rather than only warning, and retain a separate verified account ID.
5. Store credentials atomically with restrictive permissions or an OS secret store; serialize refreshes. Never expose tokens to model context or logs.
6. Show connected identity, granted scopes, last successful check, and reconnect/remove actions.

Use runtime capability registration so adding/removing accounts need not require restarting the whole daemon. Handle revocation and changed scopes explicitly. Start read-only; introduce sending mail or calendar writes later with separate permissions and confirmations.

## Workstream E: a simple operations surface

Add `anna doctor` and dashboard cards that answer:

- Are Slack and Telegram actually receiving?
- When did each account last complete a check?
- Is Claude ready, rate-limited, unauthenticated, or not recently tested?
- Did the last heartbeat fully complete, partially complete, or fail?
- What jobs and messages are pending retry?
- What does Anna remember, and from which sources?

Use recent real SDK successes/failures as evidence. If idle, a low-frequency, budgeted probe can establish fresh readiness; label untested/stale status honestly. Client construction is not an authentication probe.

Make alerts transition-based and bounded. Preserve the current no-silent-billing-fallback intent. The Agent SDK is an execution interface, not a separate free quota: confirm supported authentication and current subscription/API billing rules for the intended deployment before promising avoidance of extra usage. Reduce costs by making empty account checks and reminders deterministic and limiting background model calls.

## Delivery plan

### Phase 0 — establish the baseline and fix concrete defects

- Add regression tests for cross-channel aliases, failed-checkpoint replay, same-second writes, and dead receivers with valid credentials.
- Fix per-event reply routing and checkpoint coverage/atomicity.
- Isolate blocking Google requests safely.
- Resolve the two consolidation failures and supervise receiver startup/liveness.

**Exit:** green baseline plus the new regressions; no misrouted cross-channel replies or falsely healthy dead receivers.

### Phase 1 — memory MVP

- Add SQLite migrations, owner/conversation separation, memory records, and FTS5 retrieval.
- Add per-turn context refresh, explicit remember/correct/forget, and durable extraction jobs.
- Import existing core facts conservatively and index existing markdown/transcripts with provenance.
- Add a small memory management screen.

**Exit:** the restart, correction, long-message, privacy, and cross-channel memory acceptance tests pass.

### Phase 2 — durable execution and built-in heartbeat

- Add occurrence/run records, leases, misfire/overlap policy, cancellation, and outbox delivery.
- Implement deterministic Google collectors and structured check receipts.
- Migrate existing schedules without running old jobs twice; keep a legacy agent-prompt job type for custom skills.
- Keep YAML as human-editable configuration, not the run ledger.

**Exit:** crash/restart and provider-failure tests show recoverable work and honest partial outcomes.

### Phase 3 — connection and repair UX

- Add the reusable-client Google connect/reconnect flow.
- Add `anna doctor`, status cards, retry controls, and scope-aware permissions.
- Put normal setup in the UI; leave advanced configuration in YAML.

**Exit:** a clean installation can connect accounts and channels, receive a message, remember a fact, and run a heartbeat using the documented flow.

### Phase 4 — seven-day soak

Exercise real Slack/Telegram and explicitly authorized test accounts. Include daemon restarts, brief network outages, token revocation, rate limits, long conversations, and quiet hours. Record check completion, pending delivery age, recovery time, memory recall correctness, and model usage. Redact message/account content from shared diagnostics.

Do not add more channels or autonomous write permissions until this passes.

### Phase 5 — Grok Bot-style workflow execution

- Add the user-visible task/routine layer above execution runs, with completion evidence, commitments, and status queries from any authorized channel. The minimum durable task ownership should begin in Phase 2; this phase expands workflow support.
- Add versioned routines authored and corrected through chat; support preview/dry-run before activation.
- Add narrowly scoped draft/write capabilities with persisted action approvals.
- Pilot one isolated browser workflow only where APIs cannot do the job; verify login recovery and application-side results.
- Extend specialist handoffs through shared task state, not unlimited delegation or private-memory sharing.

**Exit:** a demonstrated or described multi-step workflow can be saved, corrected, resumed after interruption, and completed with receipts; approval gates remain effective across retries and restarts.

## Suggested first implementation slice

For the shortest path to a noticeably better Anna: **fix reply routing and checkpoint loss, add explicit memory storage plus retrieval on every turn, then ship one verified Gmail/Calendar heartbeat**. That proves the central product promise before taking on public OAuth distribution or broader agent features.

## Sources checked

- Repository source and test suite referenced above.
- Grok Bot product announcement (vendor-stated behavior, not an independent reliability test): https://x.ai/news/introducing-grok-bot
- Google installed-app OAuth, client registration, loopback redirects, and PKCE: https://developers.google.com/identity/protocols/oauth2/native-app
- Google refresh-token expiration, including external Testing apps: https://developers.google.com/identity/protocols/oauth2#expiration
- Gmail scopes and verification distinctions: https://developers.google.com/workspace/gmail/api/auth/scopes
- gog overview (an option to evaluate, not proof of zero-setup authorization): https://gogcli.sh
- Claude Agent SDK Python overview and client/tool/hook interfaces: https://github.com/anthropics/claude-agent-sdk-python
