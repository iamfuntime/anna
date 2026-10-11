# Trial readiness (branch `iamfuntime/anna-reliability`)

Verdict: **ready for an isolated, offline trial.** Not verified against any live Slack, Telegram, Google or model
service; that is the point of the trial. Nothing here touches `~/anna`, an installed `uv tool`, systemd, a live bot, real
credentials, a paid fallback, or `main`.

Evidence at the commit that contains this file (see `git log -1`; run on 2026-10-10, Python 3.13, temp `ANNA_HOME` and
`HOME`, fake transports, no services): see "Evidence" at the end of `docs/anna-implementation-status.md`
("Independent reconciliation review fixes") for the exact commit, suite count and the finding-by-finding disposition of
`/tmp/anna-reconciliation-review-fb4865a.md`.

| check | result |
| --- | --- |
| full suite | recorded in the status document at the commit that contains this file |
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
| `memory.enabled` | `false` if the trial must not create/open a memory DB | kill switch; default is `true` for compatibility |

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
# expect: 1840 passed, 1 skipped
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
| `last_fired_at` more than 60 s in the future (clock step, hand edit) | ignored and audited (`cursor_future_stamp_ignored`); it can never pin the cursor |
| ledger-pre-v6 database (never released) | has no recorded stamps, so the first YAML stamp newer than the cursor is treated as external once; at worst one tick is skipped, never repeated |

**Policy for work queued *before* the rollback (M4), conservative on purpose.** When an external stamp advances the cursor, then
*in the same transaction*: never-started `cron` occurrences due at or before the stamp are closed `skipped_misfire`
("superseded"), and outbox rows still `pending` that were created at or before the stamp are `cancelled` ("superseded"); both are
listed in the audit row (`superseded_occurrences`, `superseded_outbox`). Not touched: `claimed`/`running` occurrences and
`sending`/`uncertain` rows (owned by recovery), manual occurrences, and everything belonging to `google_heartbeat` schedules
(main cannot run those, so nothing was superseded). Work queued *by this build* after its own last stamp (same-build crash,
`overlap: queue`) is recovered once as before. The cost of the policy: a message that was queued but never sent before the
rollback is dropped rather than risked as a duplicate of what the older build did; the audit row says so.

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
  switch; an existing pre-v6 ledger (never released) can skip one tick once.
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
