# Trial readiness (branch `iamfuntime/anna-reliability`)

Verdict: **ready for an isolated, offline trial.** Not verified against any live Slack, Telegram, Google or model
service; that is the point of the trial. Nothing here touches `~/anna`, an installed `uv tool`, systemd, a live bot, real
credentials, a paid fallback, or `main`.

## Independent final verdict (HEAD `11b688a`)

An independent read-only review of the repairs `fb4865a..11b688a` (`/tmp/anna-reconciliation-review-11b688a.md`, run from
`git archive` copies with a scrubbed environment, temp `HOME`/`ANNA_HOME`, fake router, no systemd/provider/credential/production
home) concluded: **approve for the isolated trial.** H1, H2 and M1-M9 could not be reproduced at HEAD. L8 (legacy Slack intake)
was out of scope and stays deferred. The reviewer **independently reproduced 1904 passed, 1 skipped, exit 0** at `11b688a`.

**Status of this approval.** Approved for the isolated trial only. A live swap, a merge to `main`, a copy of production data and
any touch of services or accounts are **not approved and not authorized**; nothing in this branch has done any of them.

**Mandatory trial condition: use a fresh, isolated `ANNA_HOME`.** A normal trial starts from a new trial home (section 2) or
from a copy of main's `schedules.yaml` in a new home. **Do not reuse a trial home whose ledger was created by an earlier build of
this branch (schema v5 or older) if it holds queued, unsent output** - see N1.

### Remaining reproducible issues (all Low; none blocks a trial from main or a fresh home)

| id | issue | consequence | status |
| --- | --- | --- | --- |
| N1 | v5 -> v6 upgrade of an existing branch ledger has no recorded stamps, so the build's own last completion stamp is taken for an older build's and `_supersede` runs (`ledger.py:329-349`, `:614-630`) | **every** `pending` outbox row and queued cron occurrence of that schedule is cancelled/skipped as "superseded" (audited with row ids), not just one tick. Reproduced with a real two-build run (fb4865a then HEAD, same home). Medium if a pre-v6 trial home is reused | **open**; avoid by using a fresh home. Production upgrades from main are unaffected (fresh ledger, cursor seeded directly) |
| N2 | after a rollback, an in-flight `read_only` run is re-run by recovery and its stale report is posted (`ledger.py:632-660`); a `side_effecting` one ends `unknown` and is not re-run | a report from a tick the older build already superseded can be delivered minutes later; no remote side effect is repeated (the run is declared read-only) | **open**; documented below |
| N3 | a future `last_fired_at` on **first sight** (no cursor yet, i.e. the upgrade from main) seeds the cursor from it (`ledger.py:569`); the future check lives only in `_adopt` | cursor can be pinned hours ahead, so later ticks do not run until it passes, and correcting the YAML does not unpin it. Needs a bad clock or a hand edit | **open**; the 60 s future-stamp rule protects an *existing* cursor only |

Reviewer nits (N4, none functional): the `cursor_future_stamp_ignored` audit row is written on every poll while the stamp stays in
the YAML; a crash-window replay writes no second transcript row but does emit a second `delivered_post_recorded` audit row; a
`memory-import` dry run over an existing DB leaves `memory.sqlite3-shm`/`-wal` beside it (main file byte-identical); the wizard
plan in a trial home still prints "Web dashboard: disabled (unit installed but stopped)" though no unit is installed;
`scheduler.py` `_execute_started` has a bare `return` in a `-> bool` function (falsy, behaves correctly); a brand-new home gets a
`schedules.yaml.bak-pre-ledger-*` copy on first start.

**Not verified by the review:** anything against a real home, systemd, transport or provider; main's full `Scheduler` loop was
never driven (the rollback evidence uses main's real `ScheduleStore` plus the vendored `MainBuild` mock); L2 and L7 were checked by
reading and the suite's tests; concurrency of two live daemons on one home; the `settled_unrecorded` 7-day window edge.

### What the upgrade / rollback tests do and do not show

* **Fixture tests (what is tested):** `tests/test_upgrade_rollback_acceptance.py` alternates this build with a *vendored mock* of
  main `59b946a` (`tests/fixtures/main_59b946a/`, golden `schedules.yaml`; `schedule_types.py` is byte-identical to
  `git show 59b946a:` apart from the stubbed `validate_model_string` import) on one temp home with a fake clock. The reviewer
  separately drove main's real `ScheduleStore` through HEAD -> main -> `restore_schedule_fields.py` -> HEAD and saw the same
  result: main's tick not replayed, main's `state` kept, erased fields restored.
* **Not tested (live main scheduler):** main's real daemon/`Scheduler` poll loop has never been run against a home that this
  build also used. The rollback behaviour of a *live* main daemon is inferred from its store code and the mock, not observed.
* **Acceptance use:** any rollback/retrial rehearsal must use mocks or no transports (no Slack/Telegram/Google, no provider, no
  systemd). Do not rehearse against a real home.
* **Stale output across rollback (N2):** if the build is stopped mid-run of a `read_only` schedule, an older build then runs a newer
  tick, and this build is started again, recovery re-runs the interrupted `read_only` occurrence and may emit its now-stale
  report. A `side_effecting` interrupted run ends `unknown` and emits nothing. Treat output from the first poll after a rollback
  as possibly stale.

### User-feedback outcomes preserved in this branch

* `gate_command` and the per-schedule `model` pin from main are preserved (gate skip closes as `skipped_gate`; model validated
  and carried on the event).
* Main's features were restored on the reconciliation merge: delivered-post recording in the destination DM, notification leakage
  guard, Slack file intake, per-run token accounting, outbound sanitization, unknown schedule keys kept.
* Safer default paths and restart controls: scheduler/vault/socket default under `$ANNA_HOME`; entry points refuse a non-real home
  that points into `~/anna`; restart disabled for an isolated home under any spelling of a live unit; `anna setup` never touches
  live units.
* Configurable memory: durable sqlite memory is **opt-in** (`memory.enabled`, default `false`; absent config = file memory only), documented below.
* Interruptions are separate from failures: `audit.schedule.interrupted`, own streak, `scheduler.interruption_threshold`.

Evidence at the commit that contains this file (see `git log -1`; run on 2026-10-10, Python 3.13, temp `ANNA_HOME` and
`HOME`, fake transports, no services): see "Evidence" at the end of `docs/anna-implementation-status.md`
("Independent reconciliation review fixes") for the exact commit, suite count and the finding-by-finding disposition of
`/tmp/anna-reconciliation-review-fb4865a.md`.

| check | result |
| --- | --- |
| full suite | 1,904 passed, 1 skipped (was 1,840 at `fb4865a`); also run from a `git archive` copy with a scrubbed environment |
| earlier reviews (`foundation-review-10ff5ba`, `integration-review-cf72508`) | fixed in `87fa663`, `d1a289d`, `20044d7` |
| reconciliation review `fb4865a` (H1, H2, M1-M9, L1-L7) | fixed; L8 (Slack file intake hardening, identical to main) deliberately deferred |
| two-build upgrade -> rollback -> retrial | `tests/test_upgrade_rollback_acceptance.py`: a vendored mock of main `59b946a` (golden `schedules.yaml`, its pydantic model, its rewrite-on-every-fire store) and this build alternate on one temp home with a fake clock |
| lint / types | `ruff` and `mypy --strict` are **not clean, pre-existing** (import order, `UTC`, SDK/pydantic `Literal` typing); new code was not made to pass them. No type error indicates a runtime bug was identified |
| offline smoke | `anna doctor` against a fresh trial home exits 1 with `??` rows (honest "not verified"); dashboard serves `/health`, `/google`, `/repairs`, `/memory` (200), rejects a foreign `Host` (403) and a cross-origin POST (403) |

## Isolation rules (reconciled with origin/main `59b946a`)

A trial home must be self-contained. Set all of these explicitly in the trial `anna.yaml` (the defaults follow
`$ANNA_HOME`, but be explicit):

| setting | trial value | why |
| --- | --- | --- |
| `scheduler.state_path` | `$TRIAL_HOME/schedules.yaml` | the daemon would otherwise open `~/anna/schedules.yaml` and could run real jobs |
| `vault.path` | `$TRIAL_HOME/vault` | the vault holds checkpoints/conversations |
| `transports.cli.socket_path` | `$TRIAL_HOME/anna.sock` | avoid colliding with the live socket |
| `web.target_unit` | `""` (disabled, recommended) or a dedicated unit such as `anna-trial.service` | the live units are never restartable from a trial |
| `memory.enabled` | `false` if the trial must not create/open a memory DB | opt-in switch; default `false`, so a main-compatible config gets no memory DB, tools, injection or extraction. Set `true` to use durable memory |

Enforced in code (all use the passwd entry for "the real `~/anna`", so overriding `$HOME` does not fool them):

* **Entry points refuse to run** (exit 2, nothing opened) when `ANNA_HOME` is not the real `~/anna` but the scheduler path,
  vault path, CLI socket, Google OAuth client / account credential files, Obsidian paths or the config file
  (`$ANNA_CONFIG_PATH`, or the `./anna.yaml` fallback) point into it: `anna` (daemon), `anna ask`, `anna chat`, `anna-admin`,
  `python -m anna.setup.google_auth` and `anna-web`.
* **Restart is disabled** for an isolated home when `web.target_unit` names a live unit under *any* spelling: the name is
  normalised the way systemd reads it, so `anna`, `anna.service`, ` ANNA.service `, `anna-web` and `anna-web.service` are all the
  live units, and a name with `/` or whitespace is unusable. `POST /restart` returns 403 and no dbus/systemctl call is made.
* **`anna setup` never installs, enables, starts, restarts or disables `anna.service` / `anna-web.service`** for an isolated
  `ANNA_HOME` (`systemctl --user` always reaches your real user manager, and the bundled units hard-code `ANNA_HOME=%h/anna`).
  It writes only the trial `anna.yaml`/`.env`, with `socket_path` under the trial home and `target_unit: ""`, and prints how to
  run the daemon by hand. Do not run `anna setup` against a real home from a script you have not read.
* The test suite no longer has a blanket "every test runs as the real home" stub; the few tests that need a live restart
  control or unit install opt in with the `home_is_the_real_home` fixture (`tests/conftest.py`).

## Prerequisites

* Linux, `uv`, Python >= 3.11, a checkout of this branch (the worktree below).
* A **fresh** `ANNA_HOME` created by `mktemp -d` below. Never reuse an older trial home or a pre-v6 ledger that holds queued output.
* No credentials are needed for the offline steps. Do **not** create a `.env` in the trial home.
* Port 18799 free (any unused port works).

```bash
export FEATURE_WORKTREE=/home/funtime/orca/workspaces/anna/anna-reliability
export TRIAL_ROOT="$(mktemp -d "$HOME/anna-trial.XXXX")"     # NOT ~/anna
export TRIAL_HOME="$TRIAL_ROOT/anna"
```

Always run trial commands (including the daemon) with **both** `ANNA_HOME="$TRIAL_HOME"` and `HOME="$TRIAL_ROOT"`, so nothing under the real
`~/.claude`, `~/.config` or `~/anna` is read or written. (Exception: `uv` itself needs its cache; leave `UV_CACHE_DIR`
alone, or set it explicitly if `uv` complains with the overridden `HOME`.)

## 1. Offline test suite (no network, no services)

```bash
cd "$FEATURE_WORKTREE"
git status --short --branch        # expect branch iamfuntime/anna-reliability, clean
ANNA_HOME="$(mktemp -d)" HOME="$(mktemp -d)" uv run --extra dev pytest -q -p no:cacheprovider
# expect: 1904 passed, 1 skipped
```

Focused groups: `tests/test_review_fixes.py` (foundation review), `tests/test_integration_fixes.py`,
`tests/test_heartbeat.py`, `tests/test_doctor.py`, `tests/test_web_ops.py`, `tests/test_google_connect.py`.

## 2. Isolated trial home

```bash
mkdir -p "$TRIAL_HOME/state" && chmod 700 "$TRIAL_ROOT" "$TRIAL_HOME" "$TRIAL_HOME/state"
cat > "$TRIAL_HOME/anna.yaml" <<EOF
transports:
  slack: {enabled: false}
  telegram: {enabled: false}
  cli: {enabled: true, socket_path: $TRIAL_HOME/anna.sock}
scheduler: {state_path: $TRIAL_HOME/schedules.yaml}
vault: {path: $TRIAL_HOME/vault}
web: {enabled: true, port: 18799, target_unit: ""}
memory: {enabled: false}
google: {enabled: true, accounts: []}
heartbeat: {enabled: false}
EOF
chmod 600 "$TRIAL_HOME/anna.yaml"

HOME="$TRIAL_ROOT" ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" anna doctor
```

Expected: exit code 1, overall `WARN`, `??` for daemon / CLI transport / Claude SDK / ledger / memory, `--` for Slack,
Telegram, Google accounts and heartbeat. `??` means *not verified*, never assumed healthy.

## 3. Dashboard (no daemon, no model)

```bash
HOME="$TRIAL_ROOT" ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" python -m anna_web
# http://127.0.0.1:18799/health  /google  /repairs  /memory   (Ctrl-C to stop)
```

Without the daemon, Verify / drop-cache requests and repair intents stay queued and the pages say so. Starting the
daemon (`anna`) is optional and not needed to evaluate anything below; it would run a model turn only if you send it a
message, and with the overridden `HOME` it has no subscription login to use. Never start it with your real `HOME`:

```bash
HOME="$TRIAL_ROOT" ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" anna     # optional; Ctrl-C to stop
```

## 4. Live Google test (only with your explicit go-ahead; nothing above does this)

Follow `docs/google-connect-trial-guide.md` sections 3-4: a throwaway Google account, your own Cloud project with a
*Testing* consent screen and a Desktop OAuth client, `import-client`, add the account on `/google`, restart the trial
daemon, Connect, Verify. Expect the "unverified app" warning; Testing-mode refresh tokens expire after 7 days.

## User test steps (what to look at)

1. Step 1 passes; step 2 doctor output matches the expectation above.
2. Step 3: open `/health`; it shows the same rows as `anna doctor` and the disabled/unknown distinction.
3. `/google`: the page says the shared client is missing; importing a non-Desktop ("web") JSON, a file outside your
   home, or a file > 64 KB is refused and its content is never echoed.
4. `/repairs` and `/memory`: both render empty; `/memory` requires an explicit owner + scope before showing anything.
5. From another host name (`curl -H 'Host: evil.example' http://127.0.0.1:18799/health`) expect 403; a cross-origin POST
   (`curl -X POST -H 'Origin: http://evil.example' -d path=/x http://127.0.0.1:18799/google/client`) expect 403.
6. Optional, with the go-ahead: the live Google flow in step 4, then check `anna doctor` shows the account only after
   a real receipt, and that a wrong-mailbox login saves nothing.

## Upgrade / rollback / retrial semantics

Builds in play: **main** = `origin/main` `59b946a` (keeps `last_fired_at` in `schedules.yaml`, no ledger) and **this branch**
(ledger `state/runtime.sqlite3`, schema v1-v6, additive).

**What `last_fired_at` means on each side.** Main stamps the dispatch time. This branch stamps *completion / recovery / gate-skip*
time (wall clock, not the cron tick), and records every stamp it writes in the ledger (`schedule_stamps`, written *before* the
YAML). A stamp that is **newer than every stamp this build wrote** can only have been written by an older build (rollback) or by
hand; only such a stamp may advance the ledger cursor. A stamp this build wrote itself never moves the cursor, so a tick that came
due while a run was finishing, or while the daemon was down, still runs once. Details by case:

| situation | behaviour |
| --- | --- |
| first start from main (no ledger) | cursor seeded from `last_fired_at`; nothing main already ran is replayed; `schedules.yaml.bak-pre-ledger-<ts>` is copied once |
| restart / crash with a due tick, recovery stamps `now` | the tick still runs (newest only); older missed ticks are recorded `skipped_misfire` |
| rollback to main, main runs, re-upgrade ("retrial") | an external stamp advances the cursor to it (`audit.schedule.cursor_external_stamp`); ticks main ran are not replayed |
| `last_fired_at` more than 60 s in the future (clock step, hand edit), **cursor already exists** | ignored and audited (`cursor_future_stamp_ignored`, once per poll while it stays); the due tick runs and the run's own stamp heals the YAML. Up to +30 s is clamped to now |
| `last_fired_at` in the future on **first sight** (no cursor yet - the upgrade from main) | **not covered (N3, open):** the cursor is seeded from it and can be pinned ahead (a bad clock or hand edit is required). Check `schedules.yaml` for future stamps before the first start |
| ledger-pre-v6 database (schema v5 or older from an earlier build of this branch) | **do not reuse (N1, open).** It has no recorded stamps, so the build's own last completion stamp is taken for an older build's and **all** `pending` outbox rows and queued cron occurrences of that schedule are cancelled/skipped as superseded, not just one tick. Use a fresh isolated `ANNA_HOME` |

**Policy for work queued *before* the rollback (M4), conservative on purpose.** When an external stamp advances the cursor, then
*in the same transaction*: never-started `cron` occurrences due at or before the stamp are closed `skipped_misfire`
("superseded"), and outbox rows still `pending` that were created at or before the stamp are `cancelled` ("superseded"); both are
listed in the audit row (`superseded_occurrences`, `superseded_outbox`). Not touched: `claimed`/`running` occurrences and
`sending`/`uncertain` rows (owned by recovery), manual occurrences, and everything belonging to `google_heartbeat` schedules
(main cannot run those, so nothing was superseded). Work queued *by this build* after its own last stamp (same-build crash,
`overlap: queue`) is recovered once as before. The cost of the policy: a message that was queued but never sent before the
rollback is dropped rather than risked as a duplicate of what the older build did; the audit row says so.
**Exception (N2, open):** `running` rows are left to recovery, which re-pends an interrupted `read_only` run. That run is executed
and its report posted even though an older build already ran a newer tick, so a stale report can appear after a rollback; a
`side_effecting` interrupted run ends `unknown` and is not re-run. No remote side effect is repeated in either case.

**Ambiguous remote side effects are never repeated automatically**: a run interrupted mid-flight is `unknown` (side-effecting) and
never re-run; a send in flight is `uncertain` and never resent. Since an interrupted multi-part post strands its remaining parts,
that is now visible: `audit.schedule.delivery_interrupted`, an admin notice, and a transcript row for the part that did land
("Partial delivery: k of n parts were sent..."). On `/repairs`, *mark delivered* or *resend* of the uncertain part releases the held
back parts (they go out in order, once); *abandon* keeps them unsent.

**A restart/crash-interrupted run is an infrastructure interruption**, audited as `audit.schedule.interrupted` (not
`audit.schedule.fail`): it advances `last_fired_at` but does not increment `consecutive_failures`. The Nth consecutive
interruption (`scheduler.interruption_threshold`, default 5; timeouts and completed runs end the streak; must be >= 1) and each
later one counts as one failure, so a poison job is disabled after `interruption_threshold + failure_threshold - 1` (default 7)
consecutive interruptions. Graceful shutdown goes through the same cap.

**Delivered scheduled posts** are written to the destination DM transcript once per *message* (all parts joined, so a live DM
worker sees the headline of a long report), against the outbox row's own destination, after the message settled: all parts sent,
or a marked partial. The row carries a `delivery_key` and the ledger remembers it, so a crash cannot duplicate it;
`audit.schedule.delivered_post_recorded` is written only when the transcript write succeeded (`..._record_failed` otherwise).
`audit.schedule.complete` keeps its shape; `recorded_conv_key` is now always `null` there (delivery has not happened yet) and
`delivery` / `destination_conv_key` say what is queued where.

**`memory.enabled: false`.** No tools, injection, enqueue or extraction in the daemon; the dashboard re-reads the flag on every
request (a flip needs no dashboard restart; re-enabling also needs a dashboard that started enabled) and shows a "disabled" page
that opens nothing, `anna doctor` / `/health` report `disabled` without opening the DB, `anna-admin memory-import` refuses even
a dry run (override: `--force`), and a dry run never creates or migrates the DB. **Unaffected on purpose:** `core/MEMORY.md`
(file memory, `memory_md_append`) is a separate mechanism, and an existing `memory.sqlite3` is left as it is. **Queued extraction
jobs** (raw user text) stay in that DB while memory is off; after re-enabling, jobs older than
`memory.extraction.max_job_age_hours` (default 72) are expired unread (text wiped) instead of extracted. To remove the text now,
stop the daemon and delete `state/memory.sqlite3*` (or run `UPDATE extraction_jobs SET source_text=NULL, status='dead'` in `sqlite3`).

### Runbook: isolated rehearsal (what the acceptance test does, with real commands)

```bash
# 0. stop everything that uses the home; this rehearsal never touches ~/anna
ls "$TRIAL_HOME/schedules.yaml" "$TRIAL_HOME/state/runtime.sqlite3" 2>&1
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# 1. BEFORE UPGRADE (home currently used by main): copy the schedule file
cp -p "$TRIAL_HOME/schedules.yaml" "$TRIAL_HOME/schedules.yaml.pre-upgrade-$TS"

# 2. upgrade: start this branch (see section 3); first start also writes schedules.yaml.bak-pre-ledger-<ts>

# 3. BEFORE ROLLBACK (daemon stopped): copy BOTH stores. The YAML copy is the only place the ledger-era fields survive.
cp -p "$TRIAL_HOME/schedules.yaml" "$TRIAL_HOME/schedules.yaml.pre-rollback-$TS"
sqlite3 "$TRIAL_HOME/state/runtime.sqlite3" ".backup '$TRIAL_HOME/state/runtime.sqlite3.pre-rollback-$TS'"
cp -p "$TRIAL_HOME/state/memory.sqlite3" "$TRIAL_HOME/state/memory.sqlite3.pre-rollback-$TS" 2>/dev/null || true

# 4. rollback: run the main build on the same home (it loads schedules.yaml through its own model and rewrites it
#    on every fire, dropping job_kind / run_safety / overlap_policy / misfire_policy / misfire_grace_seconds
#    and any other key it does not know). The ledger and memory databases are not read by main.

# 5. BEFORE RE-UPGRADE (main stopped, ledger build NOT yet started): restore the erased fields, dry run first
cd "$FEATURE_WORKTREE"
python3 scripts/restore_schedule_fields.py "$TRIAL_HOME/schedules.yaml" "$TRIAL_HOME/schedules.yaml.pre-rollback-$TS"
python3 scripts/restore_schedule_fields.py "$TRIAL_HOME/schedules.yaml" "$TRIAL_HOME/schedules.yaml.pre-rollback-$TS" --apply
#    (it only adds missing keys, never edits `state`, and keeps schedules.yaml.bak-before-restore-<ts>.
#     If the ledger build already ran once, add --overwrite-defaults.)

# 6. retrial: start this branch. Check the audit for what was adopted / superseded:
grep -h 'cursor_external_stamp\|cursor_future_stamp' "$TRIAL_HOME"/audit/*.jsonl | tail
```

**What is lost on rollback and cannot be fixed on main:** every field above is erased by main on its first rewrite. After a
rollback + re-upgrade *without* step 5 a `read_only` schedule becomes `side_effecting` (an interrupted run is then never retried),
overlap/misfire policies revert to the config defaults, and a `job_kind: google_heartbeat` schedule is executed by main as an
ordinary agent prompt (it comes back `agent_prompt` and stops being a heartbeat). Step 5 restores them; `schedules.yaml.bak-pre-ledger-*`
is taken only once, before the first ledger, so it cannot do this — the pre-rollback copy in step 3 is required. Main's own fields
(`gate_command`, `model`, `ephemeral`) survive, and this branch keeps unknown keys on every save path.

**Real (non-trial) homes.** The same steps apply with `systemctl --user stop anna` before steps 1/3/5 and `start` after, but
nothing here was run against a real home. Cautions: take step 3 *after* the daemon is stopped (a live ledger copy via
`.backup` is consistent, a plain `cp` of an open WAL database is not); never run the main build and this build on the same home
at the same time; do not delete `state/runtime.sqlite3` to "reset" the cursor (that re-seeds from `last_fired_at`, forgetting
unsent output and unknown runs, which need `/repairs`); schema migrations v2-v6 each take a `runtime.sqlite3.bak-v<N>-<ts>`
copy first and are additive, so an older ledger-era build ignores v6 tables.

## Rollback of the trial itself

* Stop the dashboard (Ctrl-C) and `rm -rf "$TRIAL_ROOT"`. That removes config, ledger, memory DB, tokens and the client copy.
* If a live Google grant was made: `/google` -> **Revoke at Google**, or https://myaccount.google.com/permissions; delete the
  OAuth client in Google Cloud if unwanted.
* The branch is local and unpushed: `git switch main` leaves production untouched.

## Known limitations

* **Deferred (L8, identical to main, not a regression):** Slack file intake hardening - the file-URL host check does not check the
  scheme (a bot token could be sent to an `http://files.slack.com/...` URL taken from Slack's payload), a body without
  `Content-Length` is buffered before the size cap applies, and filenames are truncated by characters not bytes (a long CJK name
  can exceed `NAME_MAX`). No path traversal. Fix it on main first; do not enable Slack file intake for untrusted workspaces.
* **Residual after the reconciliation fixes:** the conservative supersede policy can drop a never-sent message queued just before a
  rollback (audited); a `delivered post` row is a best-effort context aid; the isolation guard checks configured paths, not what a
  tool or skill decides to open; a trial that omits `ANNA_HOME` is the real home; `core/MEMORY.md` is outside the memory kill
  switch; open issues **N1** (reusing a pre-v6 ledger cancels all its queued output, not one tick), **N2** (stale `read_only`
  report after a rollback) and **N3** (future stamp on first sight pins the cursor) - see "Independent final verdict".
* **Unreviewed/unknown:** earlier review notices were truncated ("+1 more", "+2 more") on the heartbeat, dashboard and
  ledger commits and could not be recovered. `a12363e` (Google connect) and `20044d7` have had no independent review;
  a read-through in this pass found no defect, but it is not a substitute for one. Re-run reviews against HEAD.
* **Not exercised live:** Google consent/token exchange, Gmail paging/time limits, Slack/Telegram error classification
  (verified against installed library classes only), real extractor output (opt-in `memory.extraction`), SDK-metered billing
  paths. `auth.mode` set to a metered API key is gated, but an `ANTHROPIC_API_KEY` in the environment under `auth.mode: max`
  may still be inherited by the CLI; keep keys out of the trial environment.
* **Lint/type debt:** see the table above; `mypy --strict` and full `ruff` do not pass and were not made to.
* Wildcard-bind dashboard has no `Host` guard; keep the default loopback bind. The Google loopback listener accepts the first
  request it receives (a local process can end a pending flow early, no token exposure). Remote-browser Google consent
  requires `google.oauth_callback_port` plus an `ssh -L` forward; random ports are unsupported.
* Account/service changes need a manual daemon restart; nothing restarts anything automatically.
* Owner-liveness proof for scheduler recovery needs `/proc` (Linux); other platforms fall back to lease expiry.
* SQLite rowids of pruned occurrences can be reused (pending repairs protect their targets).
* **Out of scope / deferred:** Stage 5 own browser and multi-agent workflows, SDK-backed heartbeat classifier, Gmail push,
  calendar sync tokens, hot-reload of accounts, automatic deploy or installation.
