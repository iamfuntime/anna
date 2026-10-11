# Google connect: isolated trial guide

Everything below uses a **dedicated `ANNA_HOME`**, an **alternate dashboard port**, **no Slack/Telegram tokens**
and **no production files**. Nothing here touches `~/anna`, the production service, or the main branch.

```bash
export FEATURE_WORKTREE=/home/funtime/orca/workspaces/anna/anna-reliability
export TRIAL_HOME="$HOME/anna-google-trial"          # NOT ~/anna
```

## 0. Offline / mock first (no network, no Google)

```bash
ANNA_HOME="$(mktemp -d)" uv run --directory "$FEATURE_WORKTREE" --extra dev \
  pytest -q tests/test_google_connect.py tests/test_doctor.py tests/test_web_ops.py tests/test_heartbeat.py
```
These drive Google's real `InstalledAppFlow` against a loopback socket with the token exchange and the identity lookup
mocked. Do not continue until they pass.

## 1. Create the trial home (no bot collision)

```bash
mkdir -p "$TRIAL_HOME/state" && chmod 700 "$TRIAL_HOME" "$TRIAL_HOME/state"
cat > "$TRIAL_HOME/anna.yaml" <<EOF
transports:
  slack: {enabled: false}
  telegram: {enabled: false}
  cli: {enabled: true, socket_path: $TRIAL_HOME/anna.sock}
web: {enabled: true, port: 8799}
google: {enabled: true, accounts: []}
heartbeat: {enabled: false}
EOF
chmod 600 "$TRIAL_HOME/anna.yaml"
```
Do **not** create a `.env` with Slack/Telegram/Anthropic keys in this home.
Check the offline report first:
```bash
ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" anna doctor
```
Expect `unknown` for the daemon/SDK/Google rows (nothing is verified yet) and `disabled` for transports.

## 2. Start the trial dashboard (and, optionally, the trial daemon)

```bash
ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" python -m anna_web      # http://127.0.0.1:8799/google
# optional, second terminal; needed for Verify and cache invalidation. All chat transports are disabled, so no bot polling:
ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" anna
```
Without the daemon, Connect still saves a token; Verify and "drop cached session" simply wait (the page says so).
Use the isolation settings in `docs/trial-readiness.md` ("Isolation rules"): explicit `scheduler.state_path`, `vault.path`,
`web.target_unit: ""`. The daemon refuses to start if those point into the real `~/anna`, and the dashboard Restart buttons are
disabled in a trial home. Nothing restarts the daemon for you: adding an account or changing services edits `anna.yaml` and shows "restart required".

## 3. Live test prerequisites (only with the owner's explicit go-ahead; this guide never runs them)

* A **throwaway Google account** you control (not production mail). Workspace user OAuth works only if the admin allows the app.
* Your own Google Cloud project with the Gmail API and Google Calendar API enabled, an OAuth consent screen
  (External, *Testing*, your account added as a test user) and **one Desktop app OAuth client**. ANNA does **not** register
  this for you and does **not** make the app Google-verified: expect the "unverified app" warning, and note that in Testing
  mode refresh tokens expire after 7 days.
* Download the client JSON into your home folder, then import it once:
  `ANNA_HOME="$TRIAL_HOME" uv run --directory "$FEATURE_WORKTREE" python -m anna.setup.google_auth import-client ~/Downloads/client_secret_XXXX.json`
  (or use the Import form on `/google`). Every mailbox shares this client.
* Browser on the same machine as the dashboard. Over SSH, set `google.oauth_callback_port: 49321` in the trial `anna.yaml` and
  forward both ports: `ssh -L 8799:127.0.0.1:8799 -L 49321:127.0.0.1:49321 you@host`. With the default random callback port the
  remote flow is unsupported (the page says so).

Then, on `/google`: add the account (slug, email, services) -> restart the trial daemon -> Connect -> consent in the browser ->
Verify. A different Google account than the configured email is refused and nothing is saved.

## 4. Rollback (production never touched)

* Dashboard `/google`: **Revoke at Google** (asks Google to cancel the grant) or **Remove locally** (token file only; the grant
  at Google stays valid until revoked, see https://myaccount.google.com/permissions).
* `rm -rf "$TRIAL_HOME"` deletes the shared client copy, tokens, ledger and config of the trial.
* In Google Cloud delete the OAuth client / project if you no longer want it.
* Stop the trial processes (Ctrl-C). The feature branch itself is unchanged by any of this.

## Status: tested vs deferred

| area | status |
| --- | --- |
| loopback `InstalledAppFlow` with `state` + PKCE, one active session, TTL, cancel, single use | tested (real library, mocked token endpoint) |
| state mismatch / replay / expiry / cancel cleanup (listener closed, flow dropped) | tested |
| mailbox identity hard-mismatch, partial scopes, missing refresh token: nothing persisted, no wrong-slug token | tested |
| token + client file permissions (0600/0700), atomic write, no temp leftovers | tested |
| shared client across accounts; client import validation (Desktop only, path jail, size, Google hosts, no secret echo) | tested |
| CSRF / foreign Host / non-loopback browser / banner forgery | tested |
| secrets absent from HTML, stdout/stderr and audit | tested |
| daemon-owned invalidate + verify via queued controls; degradation when no ledger/daemon | tested (fake clients) |
| service-scope-aware scopes, tool and heartbeat enforcement | tested |
| add/remove account (restart-required notice), local remove vs revoke | tested (revoke HTTP mocked) |
| real Google consent, real token exchange, real Gmail/Calendar identity call | **deferred** (needs owner authorization) |
| Workspace-admin-restricted apps, service-account delegation via UI | **deferred** (service account stays config-only) |
| remote browser without a fixed callback port | **explicitly unsupported** |
| hot-reload of new accounts/services into a running daemon | **deferred**: restart required (never automatic) |
| Stage 5 own browser / multi-agent workflows | **deferred** pending the owner's trial |
