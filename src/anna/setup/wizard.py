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

import asyncio
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
from importlib import resources
from pathlib import Path
from typing import Any

import click
import structlog
from dotenv import set_key

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
    # Phase 2.5 voice messages. Optional, skippable. One key covers both
    # inbound Whisper STT and outbound tts-1 TTS (the voice: block in
    # anna.yaml defaults both api_key_env to OPENAI_API_KEY). A
    # wizard-owned .env key: written on fresh install, updated IN PLACE via
    # dotenv.set_key on reconfigure so it is never dropped and an existing
    # value pre-fills the prompt default. Empty = operator skipped it.
    openai_api_key: str = ""
    operator_short_name: str = ""
    addressed_as_examples: list[str] = field(default_factory=list)
    operator_context: str = ""
    operator_values: str = ""
    anna_role: str = ""
    anna_duties: str = ""
    anna_out_of_scope: str = ""
    anna_tone: str = ""
    # Phase 2.5 web dashboard install posture. Default-on per the plan;
    # the wizard offers an opt-out prompt and ``--disable-web`` flips the
    # default for non-interactive installs. The unit file is *always*
    # written to ~/.config/systemd/user/ regardless — only the
    # systemctl enable/start step and the ``web.enabled`` field in
    # anna.yaml respond to this flag, so flipping back later is a
    # one-line YAML edit plus ``systemctl --user enable --now anna-web``.
    web_enabled: bool = True
    # When ``--disable-web``/``--enable-web`` is passed on the CLI the
    # wizard skips the interactive prompt — the flag is the answer.
    web_prompt_resolved: bool = False
    reconfigure: bool = False
    verbose: bool = False
    answers: dict[str, str] = field(default_factory=dict)
    # Pre-interview snapshot of each step's prior answer, keyed by the same
    # ``step`` string passed to ``_emit_step``. Populated only under a broader
    # ``--reconfigure`` (see ``_preload_existing_into_state``) so audit events
    # can record honest before/after diffs (``audit.setup.step_changed``).
    priors: dict[str, str] = field(default_factory=dict)


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

    # Under a broader reconfigure, auto-resolve the prior answer from the
    # pre-interview snapshot when the caller didn't pass one explicitly, so the
    # event honestly records step_changed vs step_completed.
    if prior is None and step in state.priors:
        prior = state.priors[step]

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

    _prompt_openai_key(state)


def _prompt_openai_key(state: WizardState) -> None:
    """Optional, skippable prompt for the OpenAI API key (voice messages).

    Phase 2.5 voice uses OpenAI for both inbound speech-to-text (Whisper)
    and outbound text-to-speech (tts-1); a single ``OPENAI_API_KEY`` covers
    both. The key is entirely optional — leaving it blank just means voice
    notes won't transcribe and TTS replies won't synthesize until the
    operator adds the key later (here on a ``--reconfigure``, by hand in
    ``.env``, or via the web dashboard).

    On ``--reconfigure`` the existing value (loaded into
    ``state.openai_api_key`` by ``_preload_existing_into_state``) is shown
    as a masked default so an Enter-through preserves it rather than wiping
    it. The key is then persisted as a wizard-owned ``.env`` variable —
    written wholesale on a fresh install, updated IN PLACE on reconfigure
    (see ``_env_pairs`` / ``_write_env_file``) so it is never dropped.
    """
    click.echo(
        "\nVoice messages (optional). ANNA can transcribe inbound Slack/\n"
        "Telegram voice notes (OpenAI Whisper) and speak replies back\n"
        "(OpenAI tts-1). Both use a single OpenAI API key. Leave blank to\n"
        "skip — you can add OPENAI_API_KEY to .env later and restart.\n"
        "Get one at https://platform.openai.com/api-keys."
    )
    existing = state.openai_api_key
    # On reconfigure, show a masked default so Enter preserves the live key
    # instead of blanking it. On a fresh install there is nothing to show.
    if existing:
        prompt_label = "OpenAI API key (Enter keeps the existing key)"
        default = existing
        show_default = False
    else:
        prompt_label = "OpenAI API key (Enter to skip)"
        default = ""
        show_default = False
    key = click.prompt(
        prompt_label,
        hide_input=True,
        default=default,
        show_default=show_default,
    )
    state.openai_api_key = key
    if key:
        _emit_step(state, step="voice.openai_api_key", answer=key, is_secret=True)


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


def step_web_dashboard(state: WizardState) -> None:
    """Ask the operator whether to keep the web dashboard enabled.

    Default-on per the Phase 2.5 plan. The prompt is "Disable web
    dashboard? [y/N]"; default ``n`` keeps it enabled. Skipped when
    ``--enable-web`` or ``--disable-web`` was passed on the CLI — the
    flag is the answer and the prompt would just re-ask. Either way,
    the unit file lands on disk in the next step so the operator can
    flip back later by toggling ``web.enabled`` in ``anna.yaml`` and
    running ``systemctl --user enable --now anna-web``.
    """
    if state.web_prompt_resolved:
        _emit_step(
            state,
            step="web.enabled",
            answer=f"{state.web_enabled} (cli flag)",
        )
        return
    click.secho("\nWeb dashboard", bold=True, fg="cyan")
    click.echo(
        "ANNA ships a localhost-only FastAPI dashboard on 127.0.0.1:8765.\n"
        "It edits anna.yaml / .env / schedules.yaml through forms and offers a\n"
        "one-button restart of the main service. The auth boundary is\n"
        "127.0.0.1 + filesystem permissions; remote access is your\n"
        "reverse-proxy problem (Caddy / Tailscale / SSH tunnel)."
    )
    disable = click.confirm("Disable web dashboard?", default=False)
    state.web_enabled = not disable
    _emit_step(state, step="web.enabled", answer=str(state.web_enabled))


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
    probe = _install_systemd_unit(state)
    _install_web_unit(state)
    return probe


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
    web_state = "enabled (127.0.0.1:8765)" if state.web_enabled else "disabled (unit installed but stopped)"
    click.echo(f"  Web dashboard: {web_state}")
    return click.confirm("\nWrite config and start ANNA?", default=True)


def _env_pairs(state: WizardState) -> list[tuple[str, str]]:
    """The ordered (key, value) pairs the wizard owns in ``.env``.

    Single source of truth for both the fresh-install full-overwrite path
    and the reconfigure per-key update path so the two never drift on which
    variables the wizard is responsible for.
    """
    pairs: list[tuple[str, str]] = [
        ("ANNA_HOME", str(state.anna_home)),
        ("ANNA_VAULT_ROOT", str(state.vault_root)),
        ("ANNA_AUTH_MODE", state.auth_mode),
    ]
    if state.auth_mode == "api_key":
        pairs.append(("ANTHROPIC_API_KEY", state.anthropic_api_key))
    if state.use_slack:
        pairs.append(("SLACK_BOT_TOKEN", state.slack_bot_token))
        pairs.append(("SLACK_APP_TOKEN", state.slack_app_token))
    if state.use_telegram:
        pairs.append(("TELEGRAM_BOT_TOKEN", state.telegram_bot_token))
        pairs.append(("ANNA_TELEGRAM_ALLOWED_USERS", state.telegram_admin_chat_id))
    # Phase 2.5 voice. Optional and wizard-owned: include it only when the
    # operator supplied a value, so skipping the prompt neither writes an
    # empty OPENAI_API_KEY on a fresh install nor blanks an existing key on
    # reconfigure (a blank pair would set_key it to "" and drop the live
    # value). When present it is updated IN PLACE on reconfigure via the
    # same set_key loop as every other wizard-owned key, so it is never
    # dropped and never wholesale-overwritten alongside operator-added vars.
    if state.openai_api_key:
        pairs.append(("OPENAI_API_KEY", state.openai_api_key))
    return pairs


def _write_env_file(state: WizardState, path: Path) -> None:
    """Persist the wizard-owned ``.env`` variables.

    Fresh install (``reconfigure=False``): build the file from the fixed key
    list and overwrite wholesale — there is nothing to preserve yet.

    Reconfigure (``reconfigure=True``): update each wizard-owned key in place
    via :func:`dotenv.set_key` so operator-added variables the wizard does not
    own (``BRAVE_SEARCH_API_KEY`` and friends) survive untouched. We drive
    ``set_key`` directly against the live ``.env`` rather than going through
    :class:`anna_web.env_store.EnvStore`, whose documented-key allow-list is
    parsed from ``.env.example`` at a repo-relative path that may not exist in
    a wheel-installed package layout.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pairs = _env_pairs(state)

    if state.reconfigure and path.exists():
        for key, value in pairs:
            set_key(str(path), key, value, quote_mode="never")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        return

    lines = [f"{key}={value}" for key, value in pairs]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _reconfigure_anna_yaml(state: WizardState, path: Path) -> None:
    """Update anna.yaml in place, one section at a time, preserving the rest.

    Routes every section the wizard owns through
    :meth:`anna_web.config_store.ConfigStore.write_section`, which reloads the
    on-disk round-trip document, replaces a single top-level section, validates
    the whole document against :class:`AnnaConfig`, and writes atomically. Any
    section the wizard does not touch (``scheduler``, ``google``, ``tools``,
    ``subagents``, ``identities``, operator hand-edits) is left exactly as the
    operator last saved it — comments and key ordering included.

    If the existing anna.yaml already fails ``AnnaConfig`` validation, the very
    first ``write_section`` raises a pydantic ``ValidationError``. We surface
    that as a clean :class:`click.ClickException` so the operator sees an
    actionable message ("your existing anna.yaml failed validation") rather than
    a raw traceback. The old blind-overwrite behavior silently replaced such a
    file; failing loud is the safer regression.
    """
    from pydantic import ValidationError

    from anna_web.config_store import ConfigStore

    store = ConfigStore(anna_home=state.anna_home)

    # Only the sections the wizard collects answers for. Everything else in the
    # document is deliberately left untouched.
    #
    # CRITICAL: write_section replaces each top-level section WHOLESALE
    # (config_store.py: ``doc[section] = payload`` — no field-level merge). So
    # every payload below is built by reading the EXISTING section and overlaying
    # ONLY the fields the wizard owns. A partial payload would drop required
    # sub-sections the operator never sees in the interview — e.g. transports.cli
    # (CLITransportConfig is required, no default), which would fail AnnaConfig
    # validation and abort an otherwise-valid reconfigure, and silently reset any
    # operator-tuned cli.socket_path / idle_gap_minutes / framing along the way.
    sections: list[tuple[str, dict[str, Any]]] = [
        ("auth", _auth_payload(state, store)),
        ("transports", _transports_payload(state, store)),
        ("vault", _vault_payload(state, store)),
        ("admin", _admin_payload(state, store)),
        ("web", _web_payload(state, store)),
    ]

    try:
        for section, payload in sections:
            asyncio.run(store.write_section(section, payload))
    except ValidationError as exc:
        raise click.ClickException(
            f"Your existing anna.yaml at {path} failed validation, so the "
            f"reconfigure was aborted before any change was written. Fix the "
            f"file by hand (or move it aside to regenerate from scratch) and "
            f"re-run anna-setup.\n\nDetails:\n{exc}"
        ) from exc


def _auth_payload(state: WizardState, store: "Any") -> dict[str, Any]:
    """Build the ``auth`` section payload for a reconfigure write.

    Reads the existing auth block and overlays only ``mode`` (the one field the
    wizard owns), preserving any other auth keys the operator may have set.
    Falls back to ``AuthConfig`` defaults on a first-time / missing section.
    """
    from anna.config import AuthConfig

    try:
        payload = store.load_validated().auth.model_dump()
    except Exception:
        payload = AuthConfig().model_dump()
    payload["mode"] = state.auth_mode
    return payload


def _transports_payload(state: WizardState, store: "Any") -> dict[str, Any]:
    """Build the ``transports`` section payload for a reconfigure write.

    Reads the existing transports block and overlays ONLY ``slack.enabled`` and
    ``telegram.enabled`` — the two toggles the wizard owns. The nested sub-dicts
    are mutated in place so sibling keys survive: most importantly the required
    ``cli`` block (CLITransportConfig has no default, so dropping it would fail
    validation), plus any other operator-tuned cli fields (socket_path,
    idle_gap_minutes, framing). Falls back to ``TransportsConfig`` defaults on a
    first-time / missing section.
    """
    from anna.config import TransportsConfig

    try:
        payload = store.load_validated().transports.model_dump()
    except Exception:
        payload = TransportsConfig().model_dump()
    # Overlay only the fields the wizard owns; mutate nested dicts in place so
    # siblings (cli, and telegram's other fields) are left intact.
    payload.setdefault("slack", {})["enabled"] = state.use_slack
    payload.setdefault("telegram", {})["enabled"] = state.use_telegram
    return payload


def _vault_payload(state: WizardState, store: "Any") -> dict[str, Any]:
    """Build the ``vault`` section payload for a reconfigure write.

    Reads the existing vault block and overlays only ``path`` (the field the
    wizard owns). Falls back to ``VaultConfig`` defaults on a first-time /
    missing section.
    """
    from anna.config import VaultConfig

    try:
        payload = store.load_validated().vault.model_dump()
    except Exception:
        payload = VaultConfig().model_dump()
    payload["path"] = str(state.vault_root)
    return payload


def _admin_payload(state: WizardState, store: "Any") -> dict[str, Any]:
    """Build the ``admin`` section payload for a reconfigure write.

    Reads the existing admin block and overlays only the two alert destinations
    the wizard collects (``slack_channel_id`` and ``telegram_chat_id``). The
    operator's existing ``startup_alert`` value is preserved verbatim — the
    wizard does not collect it, so it must not be forced to a fixed value. Falls
    back to ``AdminConfig`` defaults on a first-time / missing section.
    """
    from anna.config import AdminConfig

    try:
        payload = store.load_validated().admin.model_dump()
    except Exception:
        payload = AdminConfig().model_dump()
    payload["slack_channel_id"] = state.slack_admin_channel or ""
    payload["telegram_chat_id"] = state.telegram_admin_chat_id or ""
    return payload


def _web_payload(state: WizardState, store: "Any") -> dict[str, Any]:
    """Build the ``web`` section payload for a reconfigure write.

    Reads the existing web block so all non-``enabled`` fields (host, port,
    target_unit, any operator tuning) are preserved, then overlays the new
    ``enabled`` value. When the existing config has no web block yet (first
    enable on a pre-2.5 install), fall back to ``WebDashboardConfig`` defaults.
    """
    from anna.config import WebDashboardConfig

    try:
        existing = store.load_validated().web
        payload = existing.model_dump()
    except Exception:
        payload = WebDashboardConfig().model_dump()
    payload["enabled"] = state.web_enabled
    return payload


def _write_anna_yaml(state: WizardState, path: Path) -> None:
    """Render anna.yaml from the operator's wizard answers.

    Mirrors the structure of anna.yaml.example so the file is recognizable
    next to it. Only the keys the wizard collects are substituted; everything
    else is left at the documented defaults so the operator can tune later
    by hand-editing.

    On a reconfigure of an existing install we never re-render from this static
    template — that is the clobbering bug. We route through
    :func:`_reconfigure_anna_yaml` instead, which edits sections in place and
    preserves untouched ones. The template path below is fresh-install only.
    """
    if state.reconfigure and path.exists():
        _reconfigure_anna_yaml(state, path)
        return

    slack_enabled = "true" if state.use_slack else "false"
    telegram_enabled = "true" if state.use_telegram else "false"
    slack_admin = state.slack_admin_channel or ""
    telegram_admin = state.telegram_admin_chat_id or ""
    web_enabled_yaml = "true" if state.web_enabled else "false"

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

# Checkpointing and resume. resume_from_transcript folds the unsaved
# transcript tail (newer than the latest checkpoint) into the resume
# block on worker spawn, so a restart that skipped a checkpoint still
# comes back with recent context. periodic_enabled writes a lightweight
# checkpoint every every_turns / every_minutes during an active
# conversation, decoupled from eviction. No hot-reload — restart to apply.
checkpoint:
  periodic_enabled: true
  every_turns: 6
  every_minutes: 10
  resume_from_transcript: true
  tail_max_turns: 8
  tail_max_tokens: 1500

admin:
  slack_channel_id: "{slack_admin}"
  telegram_chat_id: "{telegram_admin}"
  startup_alert: true

# Phase 2.5 web dashboard. Localhost-only FastAPI app served by the
# anna-web.service systemd user unit. Bind 127.0.0.1 + filesystem
# permissions on .env are the entire auth boundary; remote access is
# the operator's reverse-proxy problem. Flip `enabled: false` and
# `systemctl --user disable --now anna-web` to turn it off without
# uninstalling the unit.
web:
  enabled: {web_enabled_yaml}
  host: 127.0.0.1
  port: 8765
  target_unit: anna.service
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
    # Bundled template — see src/anna/setup/templates/anna.service. Read via
    # importlib.resources so the lookup works both from a `pip install -e .`
    # editable checkout and from a `uv tool install`-managed venv. Same
    # pattern anna.core.identity uses for anna.core_files (identity.py:84-89).
    target = target_dir / "anna.service"
    try:
        template_resource = resources.files("anna.setup.templates").joinpath("anna.service")
        rendered = template_resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        click.secho(
            "Warning: could not load packaged anna.service template. "
            "Copy the unit manually and enable it with "
            "`systemctl --user enable --now anna`.",
            fg="yellow",
        )
        return None

    target.write_text(rendered, encoding="utf-8")
    return _start_and_probe(state)


def _install_web_unit(state: WizardState) -> None:
    """Copy ``anna-web.service`` into ~/.config/systemd/user/ and enable
    or disable it per ``state.web_enabled``.

    The unit file is *always* written, regardless of the enable/disable
    choice. Disabling just means ``web.enabled: false`` in anna.yaml and
    ``systemctl --user disable anna-web.service`` — flipping it back on
    later is a one-line YAML edit plus
    ``systemctl --user enable --now anna-web.service``, no second
    template install required.

    Failures degrade with a yellow warning rather than aborting the
    wizard; the dashboard is optional surface and the daemon is the
    load-bearing service.
    """
    target_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "anna-web.service"
    try:
        template_resource = resources.files("anna.setup.templates").joinpath("anna-web.service")
        rendered = template_resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        click.secho(
            "Warning: could not load packaged anna-web.service template. "
            "Copy the unit manually if you want the web dashboard online.",
            fg="yellow",
        )
        return

    target.write_text(rendered, encoding="utf-8")
    _emit_step(state, step="wiring.web_unit_written", answer=str(target))

    if not shutil.which("systemctl"):
        # WSL without systemd, macOS, etc. The file is on disk; the
        # operator wires the service themselves.
        return

    # daemon-reload picks up the freshly-written unit. Best-effort: a
    # warning here is fine, the next step will surface a real error if
    # systemd is actually broken.
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        text=True,
    )

    if state.web_enabled:
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "anna-web.service"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.secho(
                f"Warning: enable + start of anna-web.service failed: "
                f"{(result.stderr or '').strip()}",
                fg="yellow",
            )
    else:
        # Silently fail if it's already disabled — `disable` returns
        # nonzero on a unit that was never enabled, and that's the
        # expected state on a fresh install with --disable-web.
        subprocess.run(
            ["systemctl", "--user", "disable", "anna-web.service"],
            capture_output=True,
            text=True,
        )


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
    if state.web_enabled:
        click.echo("  Web dashboard: http://127.0.0.1:8765")
    else:
        click.echo(
            "  Web dashboard: disabled (flip web.enabled in anna.yaml +\n"
            "                  systemctl --user enable --now anna-web)"
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


def _reconfigure_web_only(state: WizardState) -> int:
    """Additive ``--enable-web`` / ``--disable-web`` against an existing install.

    The operative intent is just the web toggle: flip ``web.enabled`` and add a
    ``web`` block if the config predates Phase 2.5, preserving every other
    section verbatim. We deliberately do NOT touch ``.env`` here — the web
    toggle is anna.yaml-only, and the bug this branch fixes is exactly the blind
    ``.env`` overwrite that dropped operator-added keys.

    Modeled on the ``--persona`` early-return in :func:`main`: it emits the same
    lifecycle start/complete audit events and returns an exit code directly.
    """
    from pydantic import ValidationError

    from anna_web.config_store import ConfigStore

    _emit_lifecycle(
        state,
        event="setup.start",
        anna_home=str(state.anna_home),
        reconfigure=True,
        mode="web_toggle",
    )

    store = ConfigStore(anna_home=state.anna_home)
    yaml_path = store.path
    if not yaml_path.exists():
        raise click.UsageError(
            f"No existing anna.yaml at {yaml_path}. Run anna-setup without "
            f"--reconfigure first to create the initial config."
        )

    try:
        payload = _web_payload(state, store)
        asyncio.run(store.write_section("web", payload))
    except ValidationError as exc:
        raise click.ClickException(
            f"Your existing anna.yaml at {yaml_path} failed validation, so the "
            f"web toggle was not written. Fix the file by hand and re-run.\n\n"
            f"Details:\n{exc}"
        ) from exc

    _emit_step(state, step="web.enabled", answer=f"{state.web_enabled} (cli flag)")
    _emit_lifecycle(state, event="setup.complete")

    verb = "enabled" if state.web_enabled else "disabled"
    click.secho(
        f"Web dashboard {verb} in anna.yaml. Apply it with:\n"
        f"  systemctl --user {'enable --now' if state.web_enabled else 'disable --now'} anna-web",
        fg="green",
    )
    return 0


def _preload_existing_into_state(state: WizardState) -> None:
    """Load existing anna.yaml / .env values into WizardState before the interview.

    Under a broader ``--reconfigure`` each ``click.prompt(default=...)`` should
    show the operator's current value so they edit rather than re-enter (and so
    leaving a field blank does not silently reset it). Best-effort: a config
    that won't validate falls through to a fresh interview rather than aborting,
    since the per-section write later will surface the real validation error.
    """
    from anna_web.config_store import ConfigStore

    store = ConfigStore(anna_home=state.anna_home)
    if not store.path.exists():
        return
    try:
        cfg = store.load_validated()
    except Exception:
        return

    state.vault_root = Path(os.path.expanduser(str(cfg.vault.path)))
    state.use_slack = cfg.transports.slack.enabled
    state.use_telegram = cfg.transports.telegram.enabled
    state.auth_mode = cfg.auth.mode
    state.slack_admin_channel = cfg.admin.slack_channel_id or ""
    state.telegram_admin_chat_id = cfg.admin.telegram_chat_id or ""
    if not state.web_prompt_resolved:
        state.web_enabled = cfg.web.enabled

    # Secrets live in .env, not anna.yaml. Pull the admin/allowed-user id from
    # there too so the telegram default is the live value.
    env_path = state.anna_home / ".env"
    if env_path.exists():
        from dotenv import dotenv_values

        env = dotenv_values(str(env_path))
        allowed = env.get("ANNA_TELEGRAM_ALLOWED_USERS")
        if allowed:
            state.telegram_admin_chat_id = allowed
        # Pre-fill the optional voice key so its prompt defaults to the live
        # value and an Enter-through preserves it (never blanks it).
        openai_key = env.get("OPENAI_API_KEY")
        if openai_key:
            state.openai_api_key = openai_key

    # Snapshot the loaded values keyed by the step strings the step_* functions
    # emit, so _emit_step records honest step_changed/step_completed diffs.
    state.priors.update(
        {
            "storage.vault_root": str(state.vault_root),
            "channels.selected": f"slack={state.use_slack},telegram={state.use_telegram}",
            "auth.mode": state.auth_mode,
            "slack.admin_channel": state.slack_admin_channel,
            "telegram.admin_chat_id": state.telegram_admin_chat_id,
            "web.enabled": str(state.web_enabled),
        }
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
@click.option(
    "--disable-web",
    "web_choice",
    flag_value="disable",
    default=None,
    help="Install the anna-web unit but leave it disabled (skips the interactive prompt).",
)
@click.option(
    "--enable-web",
    "web_choice",
    flag_value="enable",
    help="Keep the web dashboard enabled (the default; provided for clarity in scripted installs).",
)
def main(
    reconfigure: bool,
    persona: bool,
    verbose: bool,
    anna_home: str,
    vault_root: str,
    web_choice: str | None,
) -> int:
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
    if web_choice == "disable":
        state.web_enabled = False
        state.web_prompt_resolved = True
    elif web_choice == "enable":
        state.web_enabled = True
        state.web_prompt_resolved = True
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

    # Web-toggle-only reconfigure. When --reconfigure is paired with an
    # explicit --enable-web/--disable-web on an already-configured install, the
    # operative intent is purely the web block: add/flip web.enabled and
    # preserve everything else (no full interview, no .env rewrite). This is the
    # exact `anna-setup --reconfigure --enable-web` invocation the clobbering
    # bug regressed. A bare --reconfigure (no web flag) falls through to the
    # broader interview path below.
    if reconfigure and state.web_prompt_resolved:
        return _reconfigure_web_only(state)

    _emit_lifecycle(state, event="setup.start", anna_home=str(state.anna_home), reconfigure=reconfigure)

    # Broader reconfigure: seed WizardState from the existing config so each
    # prompt's default shows the current value (operator edits, not re-enters).
    if reconfigure:
        _preload_existing_into_state(state)

    click.secho("Welcome — let's get ANNA set up. This takes a couple of minutes.", bold=True, fg="cyan")
    try:
        step_storage_path(state)
        step_channel_selection(state)
        step_telegram_path(state)
        step_slack_path(state)
        step_auth_path(state)
        step_persona_bootstrap(state)
        step_web_dashboard(state)
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
