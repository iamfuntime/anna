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

The intended path is the one-line installer:

```bash
curl -fsSL https://anna.funtime.dev/install.sh | bash
```

The script clones this repository, creates a venv, runs `pip install -e .`, and hands
off to the setup wizard. The wizard interrogates the operator for credentials, writes
`.env` at `chmod 600`, installs the systemd user unit, and runs a health check against
every enabled transport.

### Manual install

```bash
git clone https://github.com/iamfuntime/anna ~/anna
cd ~/anna
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
anna-setup
```

## Systemd

The wizard installs `systemd/anna.service` to `~/.config/systemd/user/anna.service` and
runs `systemctl --user enable --now anna`. To do it by hand:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/anna.service ~/.config/systemd/user/
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
  systemd/anna.service       user unit template
  install.sh                 curl-pipe-bash installer
  tests/                     pytest suite
```
