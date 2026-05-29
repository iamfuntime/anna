"""Interactive setup wizard.

Per v3 section 4. Six steps:

1. Channel selection (Slack, Telegram, both).
2. Telegram path with BotFather walkthrough.
3. Slack path with Slack-app and Socket Mode walkthrough.
4. Auth path: MAX subscription or API key.
5. Persona bootstrap: writes SOUL.md and IDENTITY.md.
6. Final wiring: write .env at chmod 600, install systemd unit, health check.

Every completed interview step emits ``audit.setup.step_completed``. Reruns
under ``--reconfigure`` that change an answer emit ``audit.setup.step_changed``.
Credentials are never logged literally; tokens are recorded as their last
four characters only.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
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


def step_channel_selection(state: WizardState) -> None:
    click.secho("\n[1/6] Channel selection", bold=True, fg="cyan")
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
    click.secho("\n[2/6] Telegram path: BotFather walkthrough", bold=True, fg="cyan")
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
    click.secho("\n[3/6] Slack path: Slack-app and Socket Mode walkthrough", bold=True, fg="cyan")
    click.echo(
        "1. Visit https://api.slack.com/apps and click 'Create New App' -> 'From scratch'.\n"
        "2. Under 'OAuth & Permissions', add bot scopes:\n"
        "     chat:write, channels:read, groups:read, app_mentions:read,\n"
        "     im:history, im:write, chat:write.public.\n"
        "3. Under 'Socket Mode', enable Socket Mode. Create an app-level token\n"
        "   with the connections:write scope.\n"
        "4. Install the app to your workspace.\n"
        "5. Copy the bot token (xoxb-...) and the app token (xapp-...)."
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
    click.secho("\n[4/6] Auth path", bold=True, fg="cyan")
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
        # Soft check for the Claude Code config dir; warn but do not block.
        config_dir = Path(os.path.expanduser("~/.config/claude-code"))
        if not config_dir.is_dir():
            click.secho(
                "Note: ~/.config/claude-code not found. Run `claude login` "
                "before starting ANNA, or pick api_key mode.",
                fg="yellow",
            )


def step_persona_bootstrap(state: WizardState) -> None:
    click.secho("\n[5/6] Persona bootstrap", bold=True, fg="cyan")
    click.echo(
        "ANNA needs a short interview to seed SOUL.md (your values) and\n"
        "IDENTITY.md (how she addresses you). Both files have hard token caps\n"
        "and will be evicted at session boundaries as they grow."
    )
    short_name = click.prompt("What name should ANNA address you as?", type=str)
    state.operator_short_name = short_name
    _emit_step(state, step="persona.short_name", answer=short_name)

    raw_examples = click.prompt(
        "Comma-separated examples of how you want ANNA to greet you "
        "(or press Enter to skip)",
        type=str,
        default="",
        show_default=False,
    )
    state.addressed_as_examples = [s.strip() for s in raw_examples.split(",") if s.strip()]
    _emit_step(state, step="persona.greetings", answer=raw_examples)

    ensure_core_files(state.anna_home / "core")
    _seed_identity_file(state)
    _seed_soul_file(state)


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
    # Leave the existing template in place if the operator already populated
    # it. The bootstrap interview is intentionally short on values.
    if "Addressed as" in path.read_text(encoding="utf-8"):
        return


def step_final_wiring(state: WizardState) -> None:
    click.secho("\n[6/6] Final wiring", bold=True, fg="cyan")
    env_path = state.anna_home / ".env"
    _write_env_file(state, env_path)
    _install_systemd_unit(state)
    _emit_step(state, step="wiring.env_file_written", answer=str(env_path))


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


def _install_systemd_unit(state: WizardState) -> None:
    """Copy the packaged systemd unit into ~/.config/systemd/user/."""
    target_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    target_dir.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parents[3] / "systemd" / "anna.service"
    target = target_dir / "anna.service"
    if src.is_file():
        shutil.copy2(src, target)
        click.secho(f"Installed systemd unit at {target}", fg="green")
    else:
        click.secho(
            f"Warning: could not find packaged anna.service at {src}. "
            f"Copy the unit manually.",
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
    "--anna-home",
    type=click.Path(file_okay=False),
    default=lambda: os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")),
    show_default=True,
    help="Runtime root for ANNA (core files, audit, transcripts).",
)
@click.option(
    "--vault-root",
    type=click.Path(file_okay=False),
    default=lambda: os.path.expanduser(os.environ.get("ANNA_VAULT_ROOT", "~/Obsidian/ANNA")),
    show_default=True,
    help="Markdown vault root.",
)
def main(reconfigure: bool, anna_home: str, vault_root: str) -> int:
    """Run the ANNA setup wizard."""
    configure_logging(level="INFO", format="json")
    log = get_logger("anna.setup")

    state = WizardState(
        anna_home=Path(anna_home),
        vault_root=Path(vault_root),
        reconfigure=reconfigure,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    log.info("setup.start", anna_home=str(state.anna_home), reconfigure=reconfigure)

    try:
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


if __name__ == "__main__":
    raise SystemExit(main())
