"""Interactive setup wizard.

Per v3 section 4. Seven steps:

1. Storage: where ANNA's memory (vault root) lives.
2. Channel selection (Slack, Telegram, both).
3. Telegram path with BotFather walkthrough.
4. Slack path with Slack-app and Socket Mode walkthrough.
5. Auth path: MAX subscription or API key.
6. Persona bootstrap: seeds IDENTITY.md, SOUL.md, and CLAUDE.md.
7. Final wiring: pre-flight confirm, write .env at chmod 600 + anna.yaml,
   install + start the systemd unit, then probe real per-transport readiness.

The wizard owns stdout as a human conversation: it does NOT call
``configure_logging`` (that is the long-running service's job), so no JSON log
lines leak between prompts. Every completed interview step still emits
``audit.setup.step_completed`` to the audit JSONL file (``audit.setup.step_changed``
on a changed answer under ``--reconfigure``). Credentials are never logged
literally; tokens are recorded as their last four characters only.
"""

from __future__ import annotations

import json
import logging
import os
import select
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import structlog

from anna.core.identity import ensure_core_files
from anna.log import audit_event

# Repo/docs URLs surfaced in the walkthroughs so the operator always has a
# canonical, screenshot-friendly reference one click away.
_DOCS_BASE = "https://github.com/iamfuntime/anna/blob/main/docs"


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
    verbose: bool = False
    answers: dict[str, str] = field(default_factory=dict)


def _silence_console_logging() -> None:
    """Keep the interactive wizard's console clean.

    The wizard owns stdout as a human conversation, so it deliberately does NOT
    call ``configure_logging`` (that wires a JSON StreamHandler to stdout for
    the long-running service). But structlog's *default* configuration prints
    every INFO event to stdout via its own ``PrintLogger`` — so the audit
    mirrors emitted by ``audit_event`` would still scroll past between prompts.

    We reconfigure structlog locally for the wizard process: a filtering bound
    logger that drops everything below WARNING (the INFO ``audit.setup.*``
    mirrors vanish) and a ``PrintLogger`` aimed at *stderr*. The audit *file*
    write inside ``audit_event`` is independent of this and stays fully intact.

    A broken audit write still logs CRITICAL, which clears the WARNING filter
    and prints to stderr — a broken audit trail should be loud. The running
    service is unaffected: it never imports/runs the wizard ``main``, and
    ``configure_logging`` overrides this on its own startup.
    """
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def _emit_lifecycle(state: WizardState, *, event: str, **fields: Any) -> None:
    """Record a wizard lifecycle event (start/complete) to the audit trail.

    Replaces the old ``log.info`` lifecycle calls so nothing prints to the
    console during the interactive wizard, while the JSONL audit file still
    captures the run for later inspection via ``anna-logs --audit``.
    """
    audit_dir = state.anna_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_event(event, audit_dir=audit_dir, actor="operator", **fields)


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
# Walkthroughs
# ---------------------------------------------------------------------------

# Brief = the few lines almost everyone needs. Detailed = every gotcha, shown
# on request (the operator answers "yes" to the expander, or passes --verbose).
# The full long-form also lives in docs/ for those who prefer screenshots.

TELEGRAM_STEPS_BRIEF = (
    "Create a bot with Telegram's @BotFather:\n"
    "  1. DM @BotFather and send /newbot.\n"
    "  2. Pick a name and a username ending in 'bot'.\n"
    "  3. Copy the HTTP API token it gives you, and paste it below.\n"
    f"  Full guide (with screenshots): {_DOCS_BASE}/telegram-setup.md"
)

TELEGRAM_STEPS_DETAILED = (
    "Detailed Telegram setup:\n"
    "  1. Open Telegram and start a chat with @BotFather.\n"
    "  2. Send /newbot. Pick a display name, then a username ending in 'bot'.\n"
    "  3. BotFather replies with an HTTP API token (digits:letters). Copy it.\n"
    "  4. Paste it below. The token is stored in .env at chmod 600 and is\n"
    "     never logged; only its last 4 characters appear in audit events.\n"
    "  5. After this, DM your new bot once from your own account so ANNA can\n"
    "     learn your numeric user ID (asked next). ANNA pins its allowed-users\n"
    "     list to that ID so strangers can't talk to her."
)

SLACK_STEPS_BRIEF = (
    "Create a Slack app at https://api.slack.com/apps ('From scratch'):\n"
    "  1. OAuth & Permissions -> add bot scopes (chat:write, im:history,\n"
    "     im:read, im:write, app_mentions:read, channels:read, groups:read,\n"
    "     chat:write.public).\n"
    "  2. Socket Mode -> enable; create an app-level token (connections:write).\n"
    "  3. Event Subscriptions -> enable; subscribe to message.im + app_mention.\n"
    "  4. App Home -> enable the Messages tab AND 'Allow users to send ...\n"
    "     messages from the messages tab'.\n"
    "  5. Install to your workspace, then copy the bot (xoxb-) and app (xapp-)\n"
    "     tokens below.\n"
    f"  Full guide (with screenshots + gotchas): {_DOCS_BASE}/slack-setup.md"
)

SLACK_STEPS_DETAILED = (
    "Detailed Slack setup:\n"
    "  1. Visit https://api.slack.com/apps -> 'Create New App' -> 'From scratch'.\n"
    "  2. Under 'OAuth & Permissions', add bot scopes:\n"
    "       chat:write, chat:write.public, channels:read, groups:read,\n"
    "       app_mentions:read, im:history, im:read, im:write.\n"
    "  3. Under 'Socket Mode', enable Socket Mode. Create an app-level token\n"
    "     with the connections:write scope.\n"
    "  4. Under 'Event Subscriptions':\n"
    "       a. Toggle 'Enable Events' ON.\n"
    "       b. Under 'Subscribe to bot events', click 'Add Bot User Event'\n"
    "          and add BOTH (you need both — message.im covers DMs,\n"
    "          app_mention covers @anna in channels):\n"
    "            - message.im     (required for DMs)\n"
    "            - app_mention    (required for @anna in channels)\n"
    "       c. Save changes at the bottom of the page.\n"
    "  5. Under 'App Home':\n"
    "       - Set a Display Name and Default Username for the bot user.\n"
    "       - Under 'Show Tabs', enable the Messages Tab AND tick the checkbox\n"
    "         'Allow users to send Slash commands and messages from the\n"
    "         messages tab'. Without it Slack shows 'Sending messages to this\n"
    "         app has been turned off' when you try to DM the bot.\n"
    "  6. Install the app to your workspace.\n"
    "  7. Copy the bot token (xoxb-...) and the app token (xapp-...).\n"
    "\n"
    "  If you change scopes or events later, you must Reinstall the app from\n"
    "  'OAuth & Permissions' for the changes to take effect."
)


def _walkthrough(brief: str, detailed: str, *, verbose: bool, what: str) -> None:
    """Print the brief walkthrough, then the detailed one only if asked.

    ``verbose`` (the --verbose flag) forces the detailed view; otherwise the
    operator is offered an opt-in expander so the happy path stays calm.
    """
    click.echo(brief)
    if verbose:
        click.echo("\n" + detailed)
        return
    if click.confirm(f"\nShow the detailed {what} setup steps?", default=False):
        click.echo("\n" + detailed)


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
    click.secho("\n[3/7] Telegram setup", bold=True, fg="cyan")
    _walkthrough(TELEGRAM_STEPS_BRIEF, TELEGRAM_STEPS_DETAILED, verbose=state.verbose, what="Telegram")
    click.echo("")
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
    click.secho("\n[4/7] Slack setup", bold=True, fg="cyan")
    _walkthrough(SLACK_STEPS_BRIEF, SLACK_STEPS_DETAILED, verbose=state.verbose, what="Slack")
    click.echo("")
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


def step_final_wiring(state: WizardState) -> dict[str, Any] | None:
    """Write config, install + start the service, and probe real readiness.

    Returns the probe result (see ``_start_and_probe``) so the caller can print
    an honest recap, or ``None`` when the service could not be started here
    (no systemd / missing unit) and the operator must start it themselves.
    """
    click.secho("\n[7/7] Final wiring", bold=True, fg="cyan")
    if not _confirm_plan(state):
        raise click.Abort()

    env_path = state.anna_home / ".env"
    yaml_path = state.anna_home / "anna.yaml"
    _write_env_file(state, env_path)
    _write_anna_yaml(state, yaml_path)
    _emit_step(state, step="wiring.env_file_written", answer=str(env_path))
    _emit_step(state, step="wiring.anna_yaml_written", answer=str(yaml_path))
    return _install_systemd_unit(state)


def _confirm_plan(state: WizardState) -> bool:
    """Show what the wizard is about to write, then ask once for the go-ahead.

    A calm, no-surprises checkpoint before any file is created or the service
    started. Returns True to proceed, False to abort cleanly.
    """
    transports = []
    if state.use_slack:
        transports.append("Slack")
    if state.use_telegram:
        transports.append("Telegram")

    click.echo("Here's what I'll set up:")
    click.echo(f"  Channels    : {', '.join(transports) or 'none'}")
    click.echo(f"  Claude auth : {state.auth_mode}")
    click.echo(f"  Vault root  : {state.vault_root}")
    click.echo(f"  Config      : {state.anna_home / 'anna.yaml'}")
    click.echo(f"  Secrets     : {state.anna_home / '.env'} (chmod 600)")
    click.echo(f"  Service     : systemd user unit 'anna', enabled + started now")
    return click.confirm("\nWrite config and start ANNA?", default=True)


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
# secrets live in .env. Edits take effect on the next anna restart
# (`systemctl --user restart anna`); there is no hot-reload.
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
  telegram:
    enabled: {telegram_enabled}
  # cli: Phase 2 §5 local CLI transport. The daemon binds a Unix-domain
  # socket at socket_path; `anna chat` (interactive TUI) and `anna ask`
  # (one-shot) connect to it from the same host. The socket is created
  # mode 0600 immediately after bind, so filesystem permissions are the
  # entire auth boundary — there is no token, no TLS, no remote case.
  # idle_gap_minutes is the per-CLI idle close, distinct from
  # sessions.dm_gap_hours (8h) and sessions.thread_gap_hours (1h). v1
  # ships NDJSON framing only; the field is kept for forward-compat.
  cli:
    enabled: true
    socket_path: ~/anna/anna.sock
    idle_gap_minutes: 30
    framing: ndjson

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

admin:
  slack_channel_id: "{slack_admin}"
  telegram_chat_id: "{telegram_admin}"
  startup_alert: true
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _install_systemd_unit(state: WizardState) -> dict[str, Any] | None:
    """Copy the packaged systemd unit into ~/.config/systemd/user/, then start
    and probe the service. Returns the probe result, or ``None`` if the unit
    file is missing (operator must wire it up manually).
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
        return None

    shutil.copy2(src, target)
    return _start_and_probe(state)


def _now_journal_since() -> str:
    """A `journalctl --since` timestamp for 'now', in local time.

    journalctl interprets a bare ``YYYY-MM-DD HH:MM:SS`` string in the system
    timezone, so we capture local now (not UTC) right before starting the unit
    and only read lines from this boot forward.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _journal_events(since: str, deadline: float):
    """Yield parsed ANNA event dicts from the user journal until ``deadline``.

    Spawns ``journalctl --user -u anna -o json --since <since> -f`` and reads
    line-by-line, bounded by a monotonic deadline via ``select`` so a quiet
    service never blocks the wizard. journald's ``-o json`` wraps the real
    event JSON inside the ``MESSAGE`` field, so we decode twice and skip any
    line that isn't an ANNA structured event. Best-effort: yields nothing if
    journald is unavailable.
    """
    cmd = ["journalctl", "--user", "-u", "anna", "-o", "json", "--since", since, "-f"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return
    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                outer = json.loads(line)
                inner = json.loads(outer.get("MESSAGE", ""))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if isinstance(inner, dict) and "event" in inner:
                yield inner
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


def _await_readiness(state: WizardState, *, since: str, timeout_s: int = 20) -> dict[str, Any] | None:
    """Tail the journal after start, classifying per-transport connectivity.

    Returns a dict ``{"transports": {name: "ok"|"failed"|"timeout"},
    "bot_username": str, "errors": {name: str}}`` or ``None`` when journald
    can't be probed (no journalctl / non-systemd session) so the caller can
    fall back to a calm "watch the logs" message instead of a false claim.
    """
    if not shutil.which("journalctl"):
        return None

    expected: set[str] = set()
    if state.use_slack:
        expected.add("slack")
    if state.use_telegram:
        expected.add("telegram")

    results: dict[str, Any] = {"transports": {}, "bot_username": "", "errors": {}}
    seen: set[str] = set()
    deadline = time.monotonic() + timeout_s

    for inner in _journal_events(since, deadline):
        event = inner.get("event")
        channel = inner.get("channel")
        if event == "channel.connected" and channel:
            results["transports"][channel] = "ok"
            seen.add(channel)
            if channel == "telegram" and inner.get("bot_username"):
                results["bot_username"] = inner["bot_username"]
        elif event in ("channel.health_check_failed", "channel.import_failed") and channel:
            results["transports"][channel] = "failed"
            results["errors"][channel] = inner.get("error", "")
            seen.add(channel)
        if expected and expected.issubset(seen):
            break

    # Any expected transport we never heard from timed out (slow network, or
    # the service is still coming up). Not fatal — the recap points at the logs.
    for channel in expected:
        results["transports"].setdefault(channel, "timeout")
    return results


def _start_and_probe(state: WizardState) -> dict[str, Any] | None:
    """daemon-reload, enable + start the unit, then probe readiness.

    Returns ``{"service": "active"|"failed"|"unknown", "restarts": int,
    "readiness": <_await_readiness result or None>}`` or ``None`` when
    systemctl is unavailable (WSL without systemd, macOS) so the wizard degrades
    to a manual-start hint rather than pretending the service is up.
    """
    if not shutil.which("systemctl"):
        click.secho(
            "Note: systemctl --user not available here. Config is written; "
            "start ANNA with the method appropriate for your init system.",
            fg="yellow",
        )
        return None

    def _run(args: list[str], purpose: str) -> bool:
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as exc:
            click.secho(f"Warning: {purpose} failed: {(exc.stderr or '').strip()}", fg="yellow")
            return False

    _run(["systemctl", "--user", "daemon-reload"], "systemd daemon-reload")
    since = _now_journal_since()
    started = _run(["systemctl", "--user", "enable", "--now", "anna.service"], "enable + start anna")

    click.echo("Starting ANNA and waiting for her to connect…")
    readiness = _await_readiness(state, since=since) if started else None

    is_active = subprocess.run(
        ["systemctl", "--user", "is-active", "anna.service"], capture_output=True, text=True
    )
    service_state = (is_active.stdout.strip() or is_active.stderr.strip() or "unknown")
    restarts = _systemd_restart_count()
    return {"service": service_state, "restarts": restarts, "readiness": readiness}


def _systemd_restart_count() -> int:
    """Best-effort read of the unit's NRestarts (a restart loop = boot failure)."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "NRestarts", "--value", "anna.service"],
            capture_output=True,
            text=True,
        )
        return int((out.stdout or "0").strip() or "0")
    except (ValueError, OSError):
        return 0


def _linger_hint() -> str | None:
    """Return a one-line linger warning if the user's services stop on logout."""
    if not shutil.which("loginctl"):
        return None
    try:
        out = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if out.stdout.strip().lower() in ("no", ""):
        user = os.environ.get("USER", "$USER")
        return f"Heads up: enable linger so ANNA keeps running after you log out:\n  loginctl enable-linger {user}"
    return None


def _print_readiness_recap(state: WizardState, probe: dict[str, Any] | None) -> None:
    """Honest, warm closing recap: what she is, how to reach her, what's next."""
    click.echo("")
    # --- Headline reflects reality, not just "we ran systemctl". ---
    if probe is None:
        click.secho("ANNA is configured.", bold=True, fg="yellow")
        click.echo("Start her, then watch her come online:  anna-logs --follow")
    else:
        readiness = probe.get("readiness")
        boot_ok = probe.get("service") == "active" and probe.get("restarts", 0) == 0
        if boot_ok:
            click.secho("ANNA is online.", bold=True, fg="green")
        elif probe.get("restarts", 0) > 0 or probe.get("service") in ("activating", "failed"):
            click.secho("ANNA started but isn't healthy yet.", bold=True, fg="red")
            if state.auth_mode == "max":
                click.echo("Most likely Claude auth — run `claude login`, then: systemctl --user restart anna")
            click.echo("See why:  anna-logs --follow")
        else:
            click.secho("ANNA is starting.", bold=True, fg="yellow")

        # --- Per-transport status, if we could probe the journal. ---
        if readiness is not None:
            _print_transport_lines(readiness)

    # --- How to talk to her now. ---
    bot_username = ""
    if probe and probe.get("readiness"):
        bot_username = probe["readiness"].get("bot_username", "")
    _print_talk_to_her(state, bot_username)

    # --- Next steps. ---
    click.secho("\nNext steps", bold=True)
    click.echo(
        "  Status      : systemctl --user status anna\n"
        "  Live logs   : anna-logs --follow\n"
        "  Audit trail : anna-logs --audit\n"
        "  Reconfigure : anna-setup --reconfigure\n"
        "  Edit persona: anna-persona"
    )
    hint = _linger_hint()
    if hint:
        click.secho("\n" + hint, fg="yellow")


def _print_transport_lines(readiness: dict[str, Any]) -> None:
    labels = {"slack": "Slack", "telegram": "Telegram"}
    for channel, status in sorted(readiness.get("transports", {}).items()):
        label = labels.get(channel, channel).ljust(9)
        if status == "ok":
            click.secho(f"  {label} connected", fg="green")
        elif status == "failed":
            err = readiness.get("errors", {}).get(channel, "")
            click.secho(f"  {label} failed — {err or 'check anna-logs --follow'}", fg="red")
        else:
            click.secho(f"  {label} not connected yet — watch anna-logs --follow", fg="yellow")


def _print_talk_to_her(state: WizardState, bot_username: str) -> None:
    click.secho("\nTalk to her", bold=True)
    if state.use_telegram:
        if bot_username:
            click.echo(f"  Telegram : DM @{bot_username} and say hi.")
        else:
            click.echo("  Telegram : DM your new bot and say hi.")
    if state.use_slack:
        click.echo("  Slack    : DM the app, or @-mention it in a channel.")


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
@click.option("--verbose", is_flag=True, help="Show the detailed channel-setup walkthroughs inline.")
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
def main(reconfigure: bool, persona: bool, verbose: bool, anna_home: str, vault_root: str) -> int:
    """Run the ANNA setup wizard."""
    # The wizard owns stdout as a human conversation, so we deliberately do not
    # call configure_logging (that wires JSON to stdout for the service). This
    # keeps the audit JSONL intact while leaving the console clean.
    _silence_console_logging()

    state = WizardState(
        anna_home=Path(anna_home),
        vault_root=Path(vault_root),
        reconfigure=reconfigure,
        verbose=verbose,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    if persona:
        _emit_lifecycle(state, event="setup.persona_only.start", anna_home=str(state.anna_home))
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
        _emit_lifecycle(state, event="setup.persona_only.complete")
        return 0

    _emit_lifecycle(state, event="setup.start", anna_home=str(state.anna_home), reconfigure=reconfigure)

    click.secho("Welcome — let's get ANNA set up. This takes a couple of minutes.", bold=True, fg="cyan")
    try:
        step_storage_path(state)
        step_channel_selection(state)
        step_telegram_path(state)
        step_slack_path(state)
        step_auth_path(state)
        step_persona_bootstrap(state)
        probe = step_final_wiring(state)
    except click.UsageError as exc:
        click.secho(f"Setup aborted: {exc}", fg="red")
        return 2
    except click.Abort:
        click.secho("Setup cancelled. Nothing was started; re-run anna-setup any time.", fg="yellow")
        return 1

    _print_readiness_recap(state, probe)
    _emit_lifecycle(state, event="setup.complete")
    return 0


def persona_entrypoint() -> int:
    """Console-script entry for ``anna-persona``.

    Forwards to ``main(--persona)`` so the operator can re-run just the
    persona interview without reconfiguring transports or auth.
    """
    return main.main(args=["--persona"], standalone_mode=False)


if __name__ == "__main__":
    raise SystemExit(main())
