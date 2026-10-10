# Trial readiness (branch `iamfuntime/anna-reliability`)

Verdict: **ready for an isolated, offline trial.** Not verified against any live Slack, Telegram, Google or model
service; that is the point of the trial. Nothing here touches `~/anna`, an installed `uv tool`, systemd, a live bot, real
credentials, a paid fallback, or `main`.

Evidence at `a12363e` plus the lint fix in this change set (run on 2026-10-10, Python 3.13, isolated `ANNA_HOME` and `HOME`):

| check | result |
| --- | --- |
| full suite | 1,646 passed, 1 skipped, 3 warnings (51 s) |
| `/tmp/anna-foundation-review-10ff5ba.md` (H1-H2, M1-M6, L1-L6) | all fixed in `87fa663`; the integration review re-verified them and found only residual O1 (fails safe), later fixed as L4 in `d1a289d` |
| `/tmp/anna-integration-review-cf72508.md` (H1, M1-M5, L1-L7) | all fixed in `d1a289d` / `20044d7` with `tests/test_integration_fixes.py` |
| `ruff --select F821` | was 2 undefined-name hits (`Path` in `anna/__main__.py`, annotation-only so no runtime error); fixed |
| rest of ruff / mypy | **not clean, pre-existing**: ~296 ruff findings (mostly import order, `UTC`, `ASYNC109`) and 128 mypy errors (strict mode vs. SDK/pydantic `Literal` types, missing stubs). No new type error indicates a runtime bug; none were changed here |
| offline smoke | `anna doctor` against a fresh trial home exits 1 with `??` rows (honest "not verified"); dashboard serves `/health`, `/google`, `/repairs`, `/memory` (200), rejects a foreign `Host` (403) and a cross-origin POST (403) |

## Prerequisites

* Linux, `uv`, Python >= 3.11, a checkout of this branch (the worktree below).
* No credentials are needed for the offline steps. Do **not** create a `.env` in the trial home.
* Port 18799 free (any unused port works).

```bash
export FEATURE_WORKTREE=/home/funtime/orca/workspaces/anna/anna-reliability
export TRIAL_ROOT="$(mktemp -d "$HOME/anna-trial.XXXX")"     # NOT ~/anna
export TRIAL_HOME="$TRIAL_ROOT/anna"
```

Always run trial commands with **both** `ANNA_HOME="$TRIAL_HOME"` and `HOME="$TRIAL_ROOT"`, so nothing under the real
`~/.claude`, `~/.config` or `~/anna` is read or written. (Exception: `uv` itself needs its cache; leave `UV_CACHE_DIR`
alone, or set it explicitly if `uv` complains with the overridden `HOME`.)

## 1. Offline test suite (no network, no services)

```bash
cd "$FEATURE_WORKTREE"
git status --short --branch        # expect branch iamfuntime/anna-reliability, clean
ANNA_HOME="$(mktemp -d)" HOME="$(mktemp -d)" uv run --extra dev pytest -q -p no:cacheprovider
# expect: 1646 passed, 1 skipped
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
web: {enabled: true, port: 18799}
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
message, and with the overridden `HOME` it has no subscription login to use. Do not start it with your real `HOME`.

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

## Rollback

* Stop the dashboard (Ctrl-C) and `rm -rf "$TRIAL_ROOT"`. That removes config, ledger, memory DB, tokens and the client copy.
* If a live Google grant was made: `/google` -> **Revoke at Google**, or https://myaccount.google.com/permissions; delete the
  OAuth client in Google Cloud if unwanted.
* The branch is local and unpushed: `git switch main` leaves production untouched. Ledger schema v1-v5 and memory v1-v2
  migrations take a `*.bak-v<N>-<ts>` copy first; when reverting code against a migrated home, restore the backup instead
  of mixing versions (a throwaway trial home makes this moot).

## Known limitations

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
