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

## Web Dashboard

ANNA ships a localhost-only FastAPI dashboard as a separate user systemd
unit (`anna-web.service`). It binds `http://127.0.0.1:8765` by default and
gives the operator a form-based editor for `anna.yaml`, a masked editor
for `.env`, full CRUD over `schedules.yaml`, and a one-button Restart of
the main daemon. Every config write, secret write, schedule mutation, and
restart request lands in the same `audit/` JSONL the daemon writes to,
visible through `anna-logs --audit`.

The dashboard is installed and enabled by default. The setup wizard
prompts `Disable web dashboard? [y/N]` during interactive install; the
default keeps it on. Scripted installs can pass `anna-setup --disable-web`
to skip the prompt and leave the unit installed-but-stopped. Flipping back
later is a one-line YAML edit plus a systemctl call:

```bash
# turn it off without uninstalling
sed -i 's/^  enabled: true$/  enabled: false/' ~/anna/anna.yaml  # under `web:`
systemctl --user disable --now anna-web.service

# turn it back on
sed -i 's/^  enabled: false$/  enabled: true/' ~/anna/anna.yaml   # under `web:`
systemctl --user enable --now anna-web.service
```

Remote access is not built in. The bind is pinned to `127.0.0.1`; if you
need to reach the dashboard from outside the host, front it with a reverse
proxy (Caddy, Tailscale serve, an SSH tunnel) and let the proxy enforce
auth. The dashboard does not ship its own login UI in v1.

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

### Checkpointing and resume

ANNA persists every turn to a JSONL transcript and writes a markdown
*checkpoint* (a compact session summary) to
`<vault>/Conversations/<conv-key>/`. On worker spawn she rebuilds working
memory from the two newest checkpoints. Two behaviors keep that working
memory in sync with what was actually said, even across an ungraceful
restart:

- **Resume from transcript.** If the conversation's transcript tail is
  newer than its latest checkpoint, a bounded RAW excerpt of that tail is
  folded into the resume block on spawn. This covers the gap left by a
  hard crash, OOM, or `kill -9` that never ran graceful closeout, because
  it keys off transcript-vs-checkpoint mtime rather than a clean shutdown.
- **Periodic checkpoint.** During an active conversation a lightweight
  checkpoint (`checkpoint_kind: periodic`) is written between turns every
  `every_turns` turns or `every_minutes` minutes, decoupled from eviction,
  so a restart never loses more than a few turns of context.

The `checkpoint:` block in `anna.yaml` tunes both (defaults shown):

```yaml
checkpoint:
  periodic_enabled: true       # write periodic checkpoints during a conversation
  every_turns: 6               # turns since last checkpoint before a periodic write
  every_minutes: 10            # minutes since last checkpoint before a periodic write
  resume_from_transcript: true # fold the unsaved transcript tail into the resume block
  tail_max_turns: 8            # max turns of tail injected on resume
  tail_max_tokens: 1500        # max tokens of tail injected on resume (newest-trimmed-first)
```

There is no hot-reload — `checkpoint` edits take effect on the next
`systemctl --user restart anna`.

### Per-agent permissions and the MCP registry

Sub-agents do not inherit ANNA's full tool surface. Each delegation runs
with an *effective grant* — the concrete set of writable directories and
MCP servers it can reach — resolved at spawn time from two trust tiers:

- **Policy (trusted).** `subagents.dir_pool` (named write directories) and
  `subagents.mcp_registry` (named MCP server specs) live in `anna.yaml`,
  which the `anna_self_edit` MCP cannot rewrite. They are the operator's
  blessed set of capabilities. Both are restart-gated — there is no hot
  reload.
- **Grants (untrusted).** `subagents.agents.<slug>` in `anna.yaml` and a
  persona file's `grants:` frontmatter. A grant may only *reference* a
  pool or registry name; an unknown name is dropped and logged with a
  warning, never invented. A persona file therefore cannot widen its own
  reach past what the operator already blessed.

Resolution layers global fallback (`subagents.extra_dirs` /
`subagents.allowed_tools`) under the `agents.<slug>` grant under the
persona frontmatter grant. Precedence is **replace, not union**: a higher
layer that specifies a field replaces the lower layer's value for that
field; a field left empty passes the lower layer through.

The security rationale is that policy and grants are kept on opposite
sides of the trust boundary. Capability *definitions* live in `anna.yaml`
(operator-only); capability *selections* live in grants (which ANNA can
edit via `anna_self_edit`, or which a persona author can write), but a
selection can only ever name an already-blessed pool entry. The forbidden
builtins (`anna_self_edit`, `anna_google`, `anna_delegate`) are
structurally unreachable: they are absent from the registry's builtin
dispatch table, and a registry entry naming one is dropped at resolution.
That is the same mechanism that enforces one-level-only delegation — a
sub-agent can never be handed the delegate server.

See the `subagents:` block in `anna.yaml.example` for `dir_pool`,
`mcp_registry`, `agents`, and persona-frontmatter examples.

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
  src/anna/setup/templates/anna.service       daemon user unit template
  src/anna/setup/templates/anna-web.service   dashboard user unit template
  src/anna_web/                               Phase 2.5 web dashboard package
  install.sh                 curl-pipe-bash installer
  tests/                     pytest suite
```
