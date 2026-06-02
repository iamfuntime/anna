# ANNA

ANNA (Adaptive Neural Network Assistant) is a personal multi-channel agent built on the
[Claude Agent SDK](https://docs.claude.com/en/docs/claude-code/sdk). It speaks Slack and
Telegram in parallel, holds per-conversation context, hires its own sub-agents on
demand, and keeps an auditable record of every sensitive state change.

## Design Rationale

The full Phase 1 buildout plan (v3) lives in the operator's Obsidian vault at
`Brain/Inbox/2026-05-29-ANNA-Phase-1-Buildout-Plan-v3.md`. Read that first if you
want the why behind any of the choices in this repository. The plan covers:

- Five Hermes-style core identity files with hard token caps and supervisor-locked writes
- One async worker per active conversation, one `ClaudeSDKClient` per worker
- A watchdog coroutine that pings every transport and the SDK session on a fixed interval
- A `ChannelAdapter` plugin contract so new transports drop into `transports/`
- Three log streams (operational, audit, transcripts) with stable event names
- A seven-day operator test plan plus two half-days for cross-transport and watchdog drills

## Installation

The one-line installer:

```bash
curl -fsSL https://anna.funtime.dev/install.sh | bash
```

The script verifies `uv` is installed, clones the source tree to a
transient cache (`~/.cache/anna-source/`), runs `uv tool install` to
drop shim binaries into `~/.local/bin/`, and hands off to the
interactive setup wizard. The wizard collects credentials, writes
`.env` at `chmod 600` and `anna.yaml` under `~/anna/`, seeds ANNA's
core identity files, installs and starts the systemd user unit, then
waits and reports per-transport readiness. Press Enter to accept
defaults; pass `--verbose` to the wizard for inline channel walkthroughs.

After install:

```bash
anna --version                # works from anywhere
anna-logs --follow            # tail operational events
systemctl --user status anna  # supervisor state
```

If `~/.local/bin` isn't on your `$PATH`, the installer warns and prints
the rc-file snippet to add it. Open a new shell after editing the rc.

Prerequisites: `uv`, `git`, `curl`, Python 3.11+. Install `uv` with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Manual install (no curl-pipe-bash)

```bash
git clone https://github.com/iamfuntime/anna ~/.cache/anna-source
uv tool install ~/.cache/anna-source
anna-setup
```

### Migrating from a pre-Phase-A install

If you installed ANNA before this migration, you have a `~/anna/.venv/`
directory and a systemd unit pointing at it. Run the migration once:

```bash
cd ~/git/anna   # or wherever your source checkout lives
bash scripts/migrate-to-uv-tool.sh
```

The script stops the daemon, installs the new `uv tool` shape, verifies
the new daemon comes up healthy, then deletes the old venv. State files
(`anna.yaml`, `.env`, `core/`, `audit/`, `transcripts/`,
`schedules.yaml`) are never touched.

### Dev workflow

Editing source, then propagating to the running daemon:

```bash
cd ~/git/anna
edit src/anna/...
git commit -am "..."
make dev-restart    # uv tool install . --reinstall && systemctl --user restart anna
```

## Systemd

The setup wizard installs `anna.service` (bundled inside the wheel at
`anna.setup.templates`) to `~/.config/systemd/user/anna.service` and
runs `systemctl --user enable --now anna`. The unit's `ExecStart=`
points at `%h/.local/bin/anna` (the uv-managed shim) and sets
`Environment=ANNA_HOME=%h/anna`. To install manually:

```bash
mkdir -p ~/.config/systemd/user
uv tool run --from anna python -c \
  "from importlib.resources import files; \
   print(files('anna.setup.templates').joinpath('anna.service').read_text())" \
  > ~/.config/systemd/user/anna.service
systemctl --user daemon-reload
systemctl --user enable --now anna
loginctl enable-linger "$USER"
```

The last line keeps the unit running when the operator is not logged in.

## Logs

Operational events go to the user journal via stdout. The recommended way to read
them is the wrapper:

```bash
anna-logs                          # last 100 lines
anna-logs --follow                 # tail
anna-logs --since 1h --level error
anna-logs --audit --since 1d
anna-logs --transcript slack-dm-U0ABCD123 --since today
```

The wrapper shells out to `journalctl --user -u anna` for the operational stream and
reads JSONL files directly for audit and transcript queries.

On disk:

```
~/anna/
  audit/audit-YYYY-MM-DD.jsonl              # append-only daily
  transcripts/<channel>-<conv_key>/YYYY-MM-DD.jsonl
```

There is no operational log directory on disk. journald handles rotation, persistence,
and remote shipping for that stream.

## Configuration

Two files drive runtime behavior:

- `.env` (chmod 600), holds secrets only. The wizard writes it; `.env.example` lists
  every variable.
- `anna.yaml` (chmod 644), holds non-secret config: log level, retention windows,
  watchdog cadence, transport enable flags. `anna.yaml.example` is the schema.

## Repository Layout

```
anna/
  src/anna/                  package code
    core/                    identity files, eviction
    runtime/                 supervisor, watchdog, router, worker
    transports/              ChannelAdapter base and Slack/Telegram implementations
    agents/                  sub-agent persona registry
    skills/                  skill-as-persona-modifier registry
    vault/                   checkpoint, transcript, audit writers
    setup/                   interactive wizard
    cli/                     anna-logs and anna-admin
    core_files/              SOUL.md, CLAUDE.md, AGENTS.md, MEMORY.md, IDENTITY.md
  src/anna/setup/templates/anna.service       user unit template
  install.sh                 curl-pipe-bash installer
  tests/                     pytest suite
```
