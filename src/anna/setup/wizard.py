"""Interactive setup wizard.

Per v3 section 4. Seven steps:

1. Storage: where ANNA's memory (vault root) lives.
2. Channel selection (Slack, Telegram, both).
3. Telegram path with BotFather walkthrough.
4. Slack path with Slack-app and Socket Mode walkthrough.
5. Auth path: MAX subscription or API key.
6. Persona bootstrap: writes SOUL.md and IDENTITY.md.
7. Final wiring: write .env at chmod 600, install systemd unit, health check.

Every completed interview step emits ``audit.setup.step_completed``. Reruns
under ``--reconfigure`` that change an answer emit ``audit.setup.step_changed``.
Credentials are never logged literally; tokens are recorded as their last
four characters only.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from anna.core.identity import ensure_core_files
from anna.log import audit_event, configure_logging, get_logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class WizardState:
    anna_home: Path
    vault_root: Path
    use_slack: bool = False
    use_telegram: bool = False
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_admin_channel: str = ""
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""
    auth_mode: str = "max"
    anthropic_api_key: str = ""
    operator_short_name: str = ""
    addressed_as_examples: list[str] = field(default_factory=list)
    operator_context: str = ""
    operator_values: str = ""
    anna_role: str = ""
    anna_duties: str = ""
    anna_out_of_scope: str = ""
    anna_tone: str = ""
    reconfigure: bool = False
    answers: dict[str, str] = field(default_factory=dict)


def _last4(secret: str) -> str:
    if not secret:
        return ""
    return secret[-4:] if len(secret) > 4 else "****"


def _emit_step(
    state: WizardState,
    *,
    step: str,
    answer: str,
    prior: str | None = None,
    is_secret: bool = False,
) -> None:
    """Audit emission for a wizard step.

    Truncates long answers and reduces secrets to their last four characters.
    """
    audit_dir = state.anna_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if is_secret:
        recorded_answer: Any = {"last4": _last4(answer)}
        recorded_prior: Any = ({"last4": _last4(prior)}) if prior else None
    else:
        recorded_answer = answer if len(answer) <= 200 else answer[:200] + "..."
        recorded_prior = (prior if prior is None or len(prior) <= 200 else prior[:200] + "...")

    event = "audit.setup.step_changed" if prior is not None and prior != answer else "audit.setup.step_completed"
    audit_event(
        event,
        audit_dir=audit_dir,
        actor="operator",
        fsync_on_write=True,
        step=step,
        answer=recorded_answer,
        prior_answer=recorded_prior,
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def step_storage_path(state: WizardState) -> None:
    click.secho("\n[1/7] Storage: where ANNA's memory lives", bold=True, fg="cyan")
    click.echo(
        "ANNA stores her core identity files, conversation transcripts, and\n"
        "session checkpoints under a vault root. Markdown all the way down, so\n"
        "any editor works. Obsidian users typically point this inside their\n"
        "existing vault so the files show up in their graph; everyone else can\n"
        "point it at a standalone directory."
    )
    default_path = str(state.vault_root)
    chosen = click.prompt(
        "Vault root path",
        default=default_path,
        type=click.Path(file_okay=False),
    )
    state.vault_root = Path(os.path.expanduser(chosen))
    state.vault_root.mkdir(parents=True, exist_ok=True)
    _emit_step(state, step="storage.vault_root", answer=str(state.vault_root))


def step_channel_selection(state: WizardState) -> None:
    click.secho("\n[2/7] Channel selection", bold=True, fg="cyan")
    click.echo(
        "ANNA can run on Slack, Telegram, or both. You can change this later by "
        "rerunning `anna-setup --reconfigure`."
    )
    state.use_telegram = click.confirm("Enable Telegram?", default=True)
    state.use_slack = click.confirm("Enable Slack?", default=False)
    if not (state.use_slack or state.use_telegram):
        raise click.UsageError("At least one transport must be enabled.")
    _emit_step(state, step="channels.selected", answer=f"slack={state.use_slack},telegram={state.use_telegram}")


def step_telegram_path(state: WizardState) -> None:
    if not state.use_telegram:
        return
    click.secho("\n[3/7] Telegram path: BotFather walkthrough", bold=True, fg="cyan")
    click.echo(
        "1. Open Telegram and start a chat with @BotFather.\n"
        "2. Send /newbot. Pick a display name and a username ending in `bot`.\n"
        "3. BotFather will reply with an HTTP API token. Copy it.\n"
        "4. Paste it below. The token is stored in .env at chmod 600 and is\n"
        "   never logged. Only the last 4 characters appear in audit events."
    )
    token = click.prompt(
        "Telegram bot token",
        hide_input=True,
        confirmation_prompt=True,
    )
    state.telegram_bot_token = token
    _emit_step(state, step="telegram.bot_token", answer=token, is_secret=True)

    click.echo(
        "\nNext, send your bot a DM from your operator account so it can learn\n"
        "your numeric user ID. ANNA will pin its allowed-users list to that ID\n"
        "to keep strangers out."
    )
    chat_id = click.prompt("Your Telegram numeric user ID (also used as admin alert chat)", type=str)
    state.telegram_admin_chat_id = chat_id
    _emit_step(state, step="telegram.admin_chat_id", answer=chat_id)


def step_slack_path(state: WizardState) -> None:
    if not state.use_slack:
        return
    click.secho("\n[4/7] Slack path: Slack-app and Socket Mode walkthrough", bold=True, fg="cyan")
    click.echo(
        "1. Visit https://api.slack.com/apps and click 'Create New App' -> 'From scratch'.\n"
        "2. Under 'OAuth & Permissions', add bot scopes:\n"
        "     chat:write, chat:write.public, channels:read, groups:read,\n"
        "     app_mentions:read, im:history, im:read, im:write.\n"
        "3. Under 'Socket Mode', enable Socket Mode. Create an app-level token\n"
        "   with the connections:write scope.\n"
        "4. Under 'Event Subscriptions':\n"
        "     a. Toggle 'Enable Events' ON.\n"
        "     b. Under 'Subscribe to bot events', click 'Add Bot User Event'\n"
        "        and add BOTH of the following (you need both — message.im\n"
        "        covers DMs, app_mention covers @anna in channels):\n"
        "          - message.im     (required for DMs)\n"
        "          - app_mention    (required for @anna in channels)\n"
        "     c. Save changes at the bottom of the page.\n"
        "5. Under 'App Home':\n"
        "     - Set a Display Name and Default Username for the bot user.\n"
        "     - Under 'Show Tabs', enable the Messages Tab AND tick the\n"
        "       checkbox 'Allow users to send Slash commands and messages\n"
        "       from the messages tab'. Without this checkbox Slack shows\n"
        "       'Sending messages to this app has been turned off' when\n"
        "       you try to DM the bot.\n"
        "6. Install the app to your workspace.\n"
        "7. Copy the bot token (xoxb-...) and the app token (xapp-...).\n"
        "\n"
        "If you change scopes or events later, you must Reinstall the app\n"
        "from 'OAuth & Permissions' for the changes to take effect."
    )
    bot_token = click.prompt("Slack bot token (xoxb-...)", hide_input=True, confirmation_prompt=True)
    state.slack_bot_token = bot_token
    _emit_step(state, step="slack.bot_token", answer=bot_token, is_secret=True)

    app_token = click.prompt("Slack app token (xapp-...)", hide_input=True, confirmation_prompt=True)
    state.slack_app_token = app_token
    _emit_step(state, step="slack.app_token", answer=app_token, is_secret=True)

    admin_channel = click.prompt(
        "Slack channel ID for admin alerts (e.g., C0AFD2LM38R)",
        type=str,
        default="",
        show_default=False,
    )
    state.slack_admin_channel = admin_channel
    _emit_step(state, step="slack.admin_channel", answer=admin_channel)


def step_auth_path(state: WizardState) -> None:
    click.secho("\n[5/7] Auth path", bold=True, fg="cyan")
    click.echo(
        "ANNA can authenticate to Claude via your MAX subscription (the\n"
        "credentials saved by `claude login`) or via an Anthropic API key.\n"
        "There is no automatic fallback at runtime."
    )
    mode = click.prompt(
        "Auth mode",
        type=click.Choice(["max", "api_key"]),
        default="max",
    )
    state.auth_mode = mode
    _emit_step(state, step="auth.mode", answer=mode)

    if mode == "api_key":
        key = click.prompt(
            "Anthropic API key",
            hide_input=True,
            confirmation_prompt=True,
        )
        state.anthropic_api_key = key
        _emit_step(state, step="auth.api_key", answer=key, is_secret=True)
    else:
        _check_claude_login()


def _check_claude_login() -> None:
    """Soft-check that ``claude login`` has been run on this machine.

    Claude Code stores the OAuth session at ``~/.claude/.credentials.json``
    on Linux and Windows. On macOS the same data lives in the system
    Keychain and no file exists, so the directory's existence is the only
    signal we can read without invoking the CLI.

    The check never blocks the wizard. It just surfaces a clear yellow
    warning when MAX auth will fail at runtime, so the operator can run
    ``claude login`` before the systemd service tries to start.
    """
    claude_home = Path(os.path.expanduser("~/.claude"))
    creds_file = claude_home / ".credentials.json"

    if creds_file.is_file():
        try:
            data = json.loads(creds_file.read_text(encoding="utf-8"))
            oauth = data.get("claudeAiOauth", {}) if isinstance(data, dict) else {}
            sub = oauth.get("subscriptionType", "")
            if oauth.get("accessToken"):
                if sub and sub != "max":
                    click.secho(
                        f"Note: Claude Code session is type '{sub}', not 'max'. "
                        f"ANNA will still authenticate but rate limits reflect that tier.",
                        fg="yellow",
                    )
                else:
                    click.secho("Detected an active Claude Code session.", fg="green")
                return
        except (OSError, json.JSONDecodeError):
            pass
        click.secho(
            "Warning: ~/.claude/.credentials.json exists but could not be parsed. "
            "Run `claude login` again, or pick api_key mode.",
            fg="yellow",
        )
        return

    if sys.platform == "darwin" and claude_home.is_dir():
        # macOS stores credentials in Keychain, not on disk. Presence of
        # ~/.claude/ is the strongest signal we have without invoking
        # `claude` and parsing its output.
        click.secho(
            "Detected ~/.claude on macOS. Credentials live in Keychain; "
            "if MAX auth fails at runtime, run `claude login` to refresh.",
            fg="green",
        )
        return

    click.secho(
        "Warning: ~/.claude/.credentials.json not found. Run `claude login` "
        "before starting ANNA, or pick api_key mode.",
        fg="yellow",
    )


def step_persona_bootstrap(state: WizardState, *, standalone: bool = False) -> None:
    header = "Persona interview" if standalone else "[6/7] Persona bootstrap"
    click.secho(f"\n{header}", bold=True, fg="cyan")
    click.echo(
        "Short interview to populate ANNA's three persona files:\n"
        "  IDENTITY.md  who she's addressing\n"
        "  SOUL.md      operator context, values, what ANNA should weigh\n"
        "  CLAUDE.md    ANNA's role, duties, tone, out-of-scope rules\n"
        "Each file has a hard token cap and will be evicted at session\n"
        "boundaries as it grows. Press Enter to skip any question.\n"
    )

    short_name = click.prompt("What name should ANNA address you as?", type=str)
    state.operator_short_name = short_name
    _emit_step(state, step="persona.short_name", answer=short_name)

    raw_examples = click.prompt(
        "Examples of how you want ANNA to greet you (comma-separated, or skip)",
        type=str,
        default="",
        show_default=False,
    )
    state.addressed_as_examples = [s.strip() for s in raw_examples.split(",") if s.strip()]
    _emit_step(state, step="persona.greetings", answer=raw_examples)

    state.operator_context = click.prompt(
        "Briefly, what do you do? (one or two sentences ANNA uses for context)",
        type=str,
        default="",
        show_default=False,
    )
    _emit_step(state, step="persona.operator_context", answer=state.operator_context)

    state.operator_values = click.prompt(
        "What should ANNA weigh when she has to make a judgment call on your\n"
        "behalf? (e.g., 'reliability over cleverness, candor over comfort,\n"
        "ask before irreversible actions')",
        type=str,
        default="",
        show_default=False,
    )
    _emit_step(state, step="persona.operator_values", answer=state.operator_values)

    state.anna_role = click.prompt(
        "How should ANNA describe her role when asked? (one sentence,\n"
        "e.g., 'personal AI assistant focused on calendar, research, and drafts')",
        type=str,
        default="",
        show_default=False,
    )
    _emit_step(state, step="persona.anna_role", answer=state.anna_role)

    state.anna_duties = click.prompt(
        "What are ANNA's primary duties? (comma-separated,\n"
        "e.g., 'morning briefs, research summaries, draft replies, reminders')",
        type=str,
        default="",
        show_default=False,
    )
    _emit_step(state, step="persona.anna_duties", answer=state.anna_duties)

    state.anna_out_of_scope = click.prompt(
        "What is OUT of scope for ANNA? (comma-separated,\n"
        "e.g., 'production infra changes, financial trades, irreversible\n"
        "actions without explicit confirmation')",
        type=str,
        default="",
        show_default=False,
    )
    _emit_step(state, step="persona.anna_out_of_scope", answer=state.anna_out_of_scope)

    state.anna_tone = click.prompt(
        "Preferred response style? (e.g., 'terse, no fluff, no apologies'\n"
        "or 'thorough with context')",
        type=str,
        default="",
        show_default=False,
    )
    _emit_step(state, step="persona.anna_tone", answer=state.anna_tone)

    ensure_core_files(state.anna_home / "core")
    _seed_identity_file(state)
    _seed_soul_file(state)
    _seed_claude_file(state)


def _seed_identity_file(state: WizardState) -> None:
    path = state.anna_home / "core" / "IDENTITY.md"
    frontmatter = (
        "---\n"
        "name: IDENTITY.md\n"
        "purpose: Who ANNA is addressing right now and the active conversational frame.\n"
        "token_cap: 1000\n"
        "last_evicted: null\n"
        "---\n\n"
    )
    body = f"# Identity\n\nAddressed as: {state.operator_short_name}\n"
    if state.addressed_as_examples:
        body += "\nGreeting examples:\n"
        for ex in state.addressed_as_examples:
            body += f"- {ex}\n"
    path.write_text(frontmatter + body, encoding="utf-8")


def _seed_soul_file(state: WizardState) -> None:
    path = state.anna_home / "core" / "SOUL.md"
    frontmatter = (
        "---\n"
        "name: SOUL.md\n"
        "purpose: The operator's core values and ANNA's relationship to them.\n"
        "token_cap: 1500\n"
        "last_evicted: null\n"
        "---\n\n"
    )
    body_lines = ["# SOUL", ""]
    if state.operator_context:
        body_lines += ["## Operator context", state.operator_context, ""]
    if state.operator_values:
        body_lines += [
            "## What to weigh on the operator's behalf",
            state.operator_values,
            "",
        ]
    body_lines += [
        "## ANNA's relationship to those values",
        "Treat the items above as standing instructions. When a request",
        "conflicts with them, surface the conflict to the operator before",
        "proceeding.",
        "",
    ]
    path.write_text(frontmatter + "\n".join(body_lines), encoding="utf-8")


def _seed_claude_file(state: WizardState) -> None:
    path = state.anna_home / "core" / "CLAUDE.md"
    frontmatter = (
        "---\n"
        "name: CLAUDE.md\n"
        "purpose: High-level operating instructions ANNA reads on every session.\n"
        "token_cap: 2500\n"
        "last_evicted: null\n"
        "---\n\n"
    )
    body_lines = ["# CLAUDE", ""]
    if state.anna_role:
        body_lines += ["## Role", state.anna_role, ""]
    if state.anna_duties:
        duties = [d.strip() for d in state.anna_duties.split(",") if d.strip()]
        if duties:
            body_lines += ["## Duties"]
            body_lines += [f"- {d}" for d in duties]
            body_lines += [""]
    if state.anna_out_of_scope:
        out = [s.strip() for s in state.anna_out_of_scope.split(",") if s.strip()]
        if out:
            body_lines += ["## Out of scope"]
            body_lines += [f"- {s}" for s in out]
            body_lines += [""]
    if state.anna_tone:
        body_lines += ["## Response style", state.anna_tone, ""]
    body_lines += [
        "## Standing operating rules",
        "- Never reference the operator's other Claude Code agents, vault,",
        "  or slash commands unless the operator asks.",
        "- Surface uncertainty rather than guess.",
        "- Confirm before irreversible actions.",
        "",
    ]
    path.write_text(frontmatter + "\n".join(body_lines), encoding="utf-8")


def step_final_wiring(state: WizardState) -> None:
    click.secho("\n[7/7] Final wiring", bold=True, fg="cyan")
    env_path = state.anna_home / ".env"
    yaml_path = state.anna_home / "anna.yaml"
    _write_env_file(state, env_path)
    _write_anna_yaml(state, yaml_path)
    _install_systemd_unit(state)
    _emit_step(state, step="wiring.env_file_written", answer=str(env_path))
    _emit_step(state, step="wiring.anna_yaml_written", answer=str(yaml_path))


def _write_env_file(state: WizardState, path: Path) -> None:
    lines: list[str] = []
    lines.append(f"ANNA_HOME={state.anna_home}")
    lines.append(f"ANNA_VAULT_ROOT={state.vault_root}")
    lines.append(f"ANNA_AUTH_MODE={state.auth_mode}")
    if state.auth_mode == "api_key":
        lines.append(f"ANTHROPIC_API_KEY={state.anthropic_api_key}")
    if state.use_slack:
        lines.append(f"SLACK_BOT_TOKEN={state.slack_bot_token}")
        lines.append(f"SLACK_APP_TOKEN={state.slack_app_token}")
    if state.use_telegram:
        lines.append(f"TELEGRAM_BOT_TOKEN={state.telegram_bot_token}")
        lines.append(f"ANNA_TELEGRAM_ALLOWED_USERS={state.telegram_admin_chat_id}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _write_anna_yaml(state: WizardState, path: Path) -> None:
    """Render anna.yaml from the operator's wizard answers.

    Mirrors the structure of anna.yaml.example so the file is recognizable
    next to it. Only the keys the wizard collects are substituted; everything
    else is left at the documented defaults so the operator can tune later
    by hand-editing.
    """
    slack_enabled = "true" if state.use_slack else "false"
    telegram_enabled = "true" if state.use_telegram else "false"
    slack_admin = state.slack_admin_channel or ""
    telegram_admin = state.telegram_admin_chat_id or ""

    body = f"""# ============================================================
# ANNA configuration
#
# Generated by `anna-setup`. Non-secret runtime config lives here;
# secrets live in .env. The runtime reloads this file on change.
# See anna.yaml.example in the repo for the full annotated schema.
# ============================================================

auth:
  mode: {state.auth_mode}

runtime:
  # Claude Agent SDK permission gate. ANNA runs headless with no operator at
  # a terminal to approve tool calls. "bypassPermissions" is the right
  # default for a service. Switch to "plan" for read-only testing,
  # "acceptEdits" if you want manual approval on non-edit tools, or
  # "default" if you actually want interactive prompts (you don't).
  permission_mode: bypassPermissions

transports:
  slack:
    enabled: {slack_enabled}
    admin_channel: "{slack_admin}"
  telegram:
    enabled: {telegram_enabled}
    admin_chat_id: "{telegram_admin}"

vault:
  path: {state.vault_root}

watchdog:
  interval_seconds: 300
  worker_stall_seconds: 90
  restart_stalled_workers: false

logging:
  level: INFO
  format: json
  audit:
    enabled: true
    retention_days: 365
    fsync_on_write: true
  transcripts:
    enabled: true
    retention_days: 30
  ship:
    enabled: false
    destination: ""
    format: json

housekeeping:
  daily_sweep_time: "03:17"

sessions:
  dm_gap_hours: 8
  thread_gap_hours: 1
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _install_systemd_unit(state: WizardState) -> None:
    """Copy the packaged systemd unit into ~/.config/systemd/user/ and
    enable + start the service so ANNA is running by the time the
    wizard exits.
    """
    target_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    target_dir.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parents[3] / "systemd" / "anna.service"
    target = target_dir / "anna.service"
    if not src.is_file():
        click.secho(
            f"Warning: could not find packaged anna.service at {src}. "
            f"Copy the unit manually and enable it with "
            f"`systemctl --user enable --now anna`.",
            fg="yellow",
        )
        return

    shutil.copy2(src, target)
    click.secho(f"Installed systemd unit at {target}", fg="green")
    _start_systemd_service()


def _start_systemd_service() -> None:
    """Reload systemd, enable the unit for boot, and start it now.

    Each step is best-effort: a missing or unusable systemd is downgraded
    to a yellow warning with the exact recovery command, since the wizard
    runs on systems where `systemctl --user` may not be available
    (headless boxes without linger, WSL without systemd, macOS).
    """
    if not shutil.which("systemctl"):
        click.secho(
            "Warning: systemctl not found. Start ANNA manually with the "
            "method appropriate for your init system.",
            fg="yellow",
        )
        return

    def _run(args: list[str], purpose: str) -> bool:
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            click.secho(
                f"Warning: {purpose} failed ({' '.join(args)}): {stderr}",
                fg="yellow",
            )
            return False

    if not _run(["systemctl", "--user", "daemon-reload"], "systemd daemon-reload"):
        return
    if not _run(["systemctl", "--user", "enable", "--now", "anna.service"], "enable + start anna"):
        return

    # Verify the service actually came up; surface the journalctl pointer
    # immediately if it did not so the operator does not chase a silent failure.
    status = subprocess.run(
        ["systemctl", "--user", "is-active", "anna.service"],
        capture_output=True,
        text=True,
    )
    state_text = status.stdout.strip() or status.stderr.strip()
    if status.returncode == 0:
        click.secho(f"anna.service is {state_text}.", fg="green")
    else:
        click.secho(
            f"Warning: anna.service is {state_text or 'not active'}. "
            f"Run `journalctl --user -u anna -xe` to see why.",
            fg="yellow",
        )


def _print_live_commands(state: WizardState) -> None:
    click.secho("\nANNA is online.", bold=True, fg="green")
    click.echo(
        "- Service:     systemctl --user status anna\n"
        "- Live logs:   anna-logs --follow\n"
        "- Audit:       anna-logs --audit\n"
        "- Reconfigure: anna-setup --reconfigure"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@click.command()
@click.option("--reconfigure", is_flag=True, help="Rerun the wizard and update existing config.")
@click.option(
    "--persona",
    is_flag=True,
    help="Run only the persona interview (re-write IDENTITY.md, SOUL.md, CLAUDE.md).",
)
@click.option(
    "--anna-home",
    type=click.Path(file_okay=False),
    default=lambda: os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")),
    show_default=True,
    help="Runtime root for ANNA (core files, audit, transcripts).",
)
@click.option(
    "--vault-root",
    type=click.Path(file_okay=False),
    default=lambda: os.path.expanduser(os.environ.get("ANNA_VAULT_ROOT", "~/anna/vault")),
    show_default=True,
    help="Markdown vault root.",
)
def main(reconfigure: bool, persona: bool, anna_home: str, vault_root: str) -> int:
    """Run the ANNA setup wizard."""
    configure_logging(level="INFO", format="json")
    log = get_logger("anna.setup")

    state = WizardState(
        anna_home=Path(anna_home),
        vault_root=Path(vault_root),
        reconfigure=reconfigure,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    if persona:
        log.info("setup.persona_only.start", anna_home=str(state.anna_home))
        try:
            step_persona_bootstrap(state, standalone=True)
        except click.Abort:
            click.secho("Persona interview cancelled.", fg="yellow")
            return 1
        click.secho(
            "\nPersona files rewritten. Restart ANNA so the new identity loads:\n"
            "  systemctl --user restart anna",
            fg="green",
        )
        log.info("setup.persona_only.complete")
        return 0

    log.info("setup.start", anna_home=str(state.anna_home), reconfigure=reconfigure)

    try:
        step_storage_path(state)
        step_channel_selection(state)
        step_telegram_path(state)
        step_slack_path(state)
        step_auth_path(state)
        step_persona_bootstrap(state)
        step_final_wiring(state)
    except click.UsageError as exc:
        click.secho(f"Setup aborted: {exc}", fg="red")
        return 2
    except click.Abort:
        click.secho("Setup cancelled by operator.", fg="yellow")
        return 1

    _print_live_commands(state)
    log.info("setup.complete")
    return 0


def persona_entrypoint() -> int:
    """Console-script entry for ``anna-persona``.

    Forwards to ``main(--persona)`` so the operator can re-run just the
    persona interview without reconfiguring transports or auth.
    """
    return main.main(args=["--persona"], standalone_mode=False)


if __name__ == "__main__":
    raise SystemExit(main())
