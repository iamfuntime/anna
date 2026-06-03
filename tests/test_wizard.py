"""Smoke tests for the setup wizard.

These drive the wizard end-to-end via click's CliRunner with all the OS
boundaries (systemctl, journalctl, loginctl) monkeypatched, so no real service
is touched and no real tokens are needed. The headline guarantee under test is
the one the redesign exists for: **no JSON log lines leak to the console during
the interactive wizard**, while the audit JSONL trail is still written.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from anna.setup import wizard


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _fake_which(present: set[str]):
    def which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in present else None

    return which


def _fake_run(active: bool = True, restarts: str = "0"):
    """A subprocess.run stand-in for systemctl/loginctl calls."""

    class _Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def run(args, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        joined = " ".join(args)
        if "is-active" in joined:
            return _Completed(0 if active else 3, "active" if active else "failed")
        if "NRestarts" in joined:
            return _Completed(0, restarts)
        if "Linger" in joined:
            return _Completed(0, "yes")  # linger on -> no nag
        return _Completed(0, "")

    return run


def _journal_yielding(events: list[dict]):
    def _gen(since, deadline):  # noqa: ANN001
        yield from events

    return _gen


def _telegram_inputs(short_name: str = "Tester", detail: str = "") -> str:
    """Input lines for a Telegram-only, MAX-auth run (Enter accepts defaults)."""
    return "\n".join(
        [
            "",            # vault root path -> default
            "",            # Enable Telegram? -> default yes
            "",            # Enable Slack? -> default no
            detail,        # Show detailed Telegram steps? -> default no
            "tok-SEKRET",  # Telegram bot token
            "tok-SEKRET",  # ...confirm
            "999000",      # numeric user id
            "",            # Auth mode -> default max
            short_name,    # persona: address as
            "",            # greeting examples
            "",            # what do you do
            "",            # what to weigh
            "",            # role
            "",            # duties
            "",            # out of scope
            "",            # tone
            "",            # Disable web dashboard? -> default no (keep enabled)
            "",            # Write config and start ANNA? -> default yes
        ]
    ) + "\n"


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Patch OS boundaries; HOME -> tmp so the systemd unit copy stays in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(wizard.shutil, "which", _fake_which({"systemctl", "journalctl"}))
    monkeypatch.setattr(wizard.subprocess, "run", _fake_run())
    monkeypatch.setattr(
        wizard,
        "_journal_events",
        _journal_yielding([{"event": "channel.connected", "channel": "telegram", "bot_username": "annabot"}]),
    )
    return tmp_path


def _run_wizard(tmp_path: Path, args_extra: list[str], stdin: str):
    home = tmp_path / "anna"
    vault = home / "vault"
    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--vault-root", str(vault), *args_extra],
        input=stdin,
        catch_exceptions=False,
    )
    return result, home


# ---------------------------------------------------------------------------
# The headline guarantee: clean console, intact audit trail
# ---------------------------------------------------------------------------


def test_no_json_blobs_on_stdout(patched):
    result, home = _run_wizard(patched, [], _telegram_inputs())
    assert result.exit_code == 0, result.output

    # No structured audit event names should appear on the console.
    assert "audit.setup.step_completed" not in result.output
    assert "audit.setup.step_changed" not in result.output

    # And no output line should parse as a structured log record.
    for line in result.output.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        assert "event" not in obj, f"JSON log leaked to stdout: {line}"


def test_audit_trail_written_without_raw_secrets(patched):
    result, home = _run_wizard(patched, [], _telegram_inputs())
    assert result.exit_code == 0, result.output

    audit_files = list((home / "audit").glob("audit-*.jsonl"))
    assert audit_files, "audit JSONL not written"
    content = audit_files[0].read_text(encoding="utf-8")
    assert "audit.setup.step_completed" in content       # trail present in the FILE
    assert "tok-SEKRET" not in content                   # raw token never persisted
    assert "SEKRET"[-4:] == "KRET"                        # only last4 is recorded
    assert '"last4": "KRET"' in content


# ---------------------------------------------------------------------------
# Files written
# ---------------------------------------------------------------------------


def test_config_and_core_files_written(patched):
    result, home = _run_wizard(patched, [], _telegram_inputs(short_name="Seth"))
    assert result.exit_code == 0, result.output

    env_path = home / ".env"
    assert env_path.is_file()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert "TELEGRAM_BOT_TOKEN=tok-SEKRET" in env_path.read_text()

    yaml_text = (home / "anna.yaml").read_text()
    assert "mode: max" in yaml_text
    assert "telegram:" in yaml_text and "enabled: true" in yaml_text

    identity = (home / "core" / "IDENTITY.md").read_text()
    assert "Seth" in identity
    assert (home / "core" / "SOUL.md").is_file()
    assert (home / "core" / "CLAUDE.md").is_file()


# ---------------------------------------------------------------------------
# Readiness recap
# ---------------------------------------------------------------------------


def test_recap_reports_connected_transport(patched):
    result, _ = _run_wizard(patched, [], _telegram_inputs())
    assert "ANNA is online." in result.output
    assert "Telegram" in result.output and "connected" in result.output
    assert "@annabot" in result.output  # bot_username surfaced for the DM hint


def test_recap_reports_timeout_when_no_connect_event(monkeypatch, patched):
    # Journal yields nothing -> the expected transport times out, not a dead end.
    monkeypatch.setattr(wizard, "_journal_events", _journal_yielding([]))
    result, _ = _run_wizard(patched, [], _telegram_inputs())
    assert result.exit_code == 0, result.output
    assert "not connected yet" in result.output
    assert "anna-logs --follow" in result.output


def test_degraded_without_systemctl(monkeypatch, patched):
    # No systemctl (WSL/macOS): config still written, calm fallback, exit 0.
    monkeypatch.setattr(wizard.shutil, "which", _fake_which(set()))
    result, home = _run_wizard(patched, [], _telegram_inputs())
    assert result.exit_code == 0, result.output
    assert (home / ".env").is_file()
    assert "ANNA is configured." in result.output
    assert "anna-logs --follow" in result.output


# ---------------------------------------------------------------------------
# Detailed-walkthrough opt-in
# ---------------------------------------------------------------------------


def test_detailed_walkthrough_is_opt_in(patched):
    brief, _ = _run_wizard(patched, [], _telegram_inputs(detail="n"))
    assert "Detailed Telegram setup:" not in brief.output

    shown, _ = _run_wizard(patched, [], _telegram_inputs(detail="y"))
    assert "Detailed Telegram setup:" in shown.output


# ---------------------------------------------------------------------------
# Persona-only mode
# ---------------------------------------------------------------------------


def test_persona_only_writes_core_not_service(patched):
    persona_inputs = "\n".join(["Seth", "", "", "", "", "", "", ""]) + "\n"
    result, home = _run_wizard(patched, ["--persona"], persona_inputs)
    assert result.exit_code == 0, result.output
    assert (home / "core" / "IDENTITY.md").is_file()
    assert not (home / ".env").exists()
    assert not (home / "anna.yaml").exists()


# ---------------------------------------------------------------------------
# Reconfigure: the clobbering-bug regression suite
# ---------------------------------------------------------------------------

# A fully-populated existing anna.yaml with a *sentinel* operator-tuned value
# in a section the wizard does not collect answers for (housekeeping). If a
# reconfigure ever re-renders from the static template, this value resets to
# the template default ("03:17") and the sentinel assertion fails loud — that
# is exactly the regression we are guarding against.
_EXISTING_YAML = """\
auth:
  mode: api_key
runtime:
  permission_mode: bypassPermissions
transports:
  slack:
    enabled: true
  telegram:
    enabled: true
vault:
  path: /home/op/customvault
housekeeping:
  # operator hand-tuned this; the wizard never asks about it
  daily_sweep_time: "04:42"
admin:
  slack_channel_id: "C0SENTINEL"
  telegram_chat_id: "5550000"
  startup_alert: true
web:
  enabled: false
  host: 127.0.0.1
  port: 9999
  target_unit: anna.service
"""

# An existing .env carrying both wizard-owned keys and an operator-added key
# (BRAVE_SEARCH_API_KEY) the wizard does not own. The reconfigure must leave
# the operator key untouched.
_EXISTING_ENV = (
    "ANNA_HOME=/home/op/anna\n"
    "ANNA_VAULT_ROOT=/home/op/customvault\n"
    "ANNA_AUTH_MODE=api_key\n"
    "ANTHROPIC_API_KEY=sk-old\n"
    "SLACK_BOT_TOKEN=xoxb-old\n"
    "SLACK_APP_TOKEN=xapp-old\n"
    "TELEGRAM_BOT_TOKEN=tg-old\n"
    "ANNA_TELEGRAM_ALLOWED_USERS=5550000\n"
    "BRAVE_SEARCH_API_KEY=brave-operator-added-key\n"
)


def _seed_existing_install(home: Path, vault: Path | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    yaml_text = _EXISTING_YAML
    env_text = _EXISTING_ENV
    if vault is not None:
        # Point the vault at a writable location so step_storage_path's
        # mkdir succeeds under a broader reconfigure.
        yaml_text = yaml_text.replace("/home/op/customvault", str(vault))
        env_text = env_text.replace("/home/op/customvault", str(vault))
    (home / "anna.yaml").write_text(yaml_text, encoding="utf-8")
    (home / ".env").write_text(env_text, encoding="utf-8")
    os.chmod(home / ".env", 0o600)


def test_enable_web_reconfigure_preserves_other_sections(patched):
    """--reconfigure --enable-web flips web.enabled and adds nothing else;
    a sentinel value in an untouched section survives."""
    home = patched / "anna"
    _seed_existing_install(home)

    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--reconfigure", "--enable-web"],
        input="",
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    yaml_text = (home / "anna.yaml").read_text()
    # web flipped on, but host/port preserved (not reset to template defaults).
    assert "enabled: true" in yaml_text
    assert "port: 9999" in yaml_text
    # Untouched section survived verbatim — this is the clobbering guard.
    assert '"04:42"' in yaml_text
    assert "C0SENTINEL" in yaml_text


def test_enable_web_reconfigure_does_not_rewrite_env(patched):
    """The web toggle is anna.yaml-only; .env (incl. BRAVE_SEARCH_API_KEY)
    is never touched."""
    home = patched / "anna"
    _seed_existing_install(home)
    env_before = (home / ".env").read_text()

    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--reconfigure", "--enable-web"],
        input="",
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    env_after = (home / ".env").read_text()
    assert env_after == env_before  # byte-for-byte; not rewritten at all
    assert "BRAVE_SEARCH_API_KEY=brave-operator-added-key" in env_after


def test_enable_web_reconfigure_first_enable_no_web_block(patched):
    """A pre-2.5 anna.yaml with no web block gets one built from defaults."""
    home = patched / "anna"
    home.mkdir(parents=True, exist_ok=True)
    no_web = "\n".join(
        line for line in _EXISTING_YAML.splitlines()
        if not (
            line.startswith("web:")
            or line.startswith("  enabled: false")
            or line.startswith("  host:")
            or line.startswith("  port:")
            or line.startswith("  target_unit:")
        )
    )
    (home / "anna.yaml").write_text(no_web + "\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--reconfigure", "--enable-web"],
        input="",
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    yaml_text = (home / "anna.yaml").read_text()
    assert "web:" in yaml_text
    assert "enabled: true" in yaml_text
    assert "port: 8765" in yaml_text  # WebDashboardConfig default


def test_broader_reconfigure_loads_existing_values_as_defaults(patched):
    """A bare --reconfigure seeds WizardState from the existing config so the
    prompts default to current values and untouched .env keys survive."""
    home = patched / "anna"
    vault = patched / "customvault"
    _seed_existing_install(home, vault=vault)

    # Accept every default (blank lines), MAX auth path so no api key prompt
    # unless defaulted. The existing config has api_key auth; we re-enter it.
    # Inputs: vault(def) telegram?(def y) slack?(def -> existing true shown)
    # but channel prompts use fixed defaults, so we answer explicitly.
    stdin = "\n".join(
        [
            "",            # vault root -> default (existing customvault path)
            "y",           # Enable Telegram?
            "y",           # Enable Slack?
            "n",           # detailed telegram steps
            "tg-new",      # telegram bot token
            "tg-new",      # confirm
            "5550000",     # numeric id
            "n",           # detailed slack steps
            "xoxb-new",    # slack bot token
            "xoxb-new",    # confirm
            "xapp-new",    # slack app token
            "xapp-new",    # confirm
            "C0SENTINEL",  # slack admin channel
            "max",         # auth mode (switch to max)
            "Op",          # persona name
            "", "", "", "", "", "", "",  # remaining persona prompts
            "",            # disable web? -> default no
            "",            # write + start?
        ]
    ) + "\n"

    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--reconfigure"],
        input=stdin,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    # The vault-root prompt should have offered the existing path as default.
    assert str(vault) in result.output

    # Untouched section + operator env key still present after a full
    # reconfigure (per-section write + per-key env set, not blind overwrite).
    yaml_text = (home / "anna.yaml").read_text()
    assert '"04:42"' in yaml_text
    env_after = (home / ".env").read_text()
    assert "BRAVE_SEARCH_API_KEY=brave-operator-added-key" in env_after
    # Wizard-owned key updated in place.
    assert "TELEGRAM_BOT_TOKEN=tg-new" in env_after


# A fully-populated anna.yaml that carries a *customized* transports.cli block
# (non-default socket_path + idle_gap_minutes) plus the housekeeping sentinel.
# The broader reconfigure must NOT drop cli (it is required on AnnaConfig with
# no default; a wholesale transports replace would fail validation and abort)
# and must NOT reset the operator's cli tuning.
_EXISTING_YAML_WITH_CLI = """\
auth:
  mode: api_key
runtime:
  permission_mode: bypassPermissions
transports:
  slack:
    enabled: false
  telegram:
    enabled: true
  cli:
    enabled: true
    socket_path: /custom/run/anna.sock
    idle_gap_minutes: 99
    framing: ndjson
vault:
  path: /home/op/customvault
housekeeping:
  daily_sweep_time: "04:42"
admin:
  slack_channel_id: "C0SENTINEL"
  telegram_chat_id: "5550000"
  startup_alert: false
web:
  enabled: false
  host: 127.0.0.1
  port: 9999
  target_unit: anna.service
"""


def test_broader_reconfigure_preserves_cli_and_untouched_sections(patched):
    """A bare --reconfigure write path must not abort, must keep a customized
    transports.cli block and an untouched sentinel section, and must apply the
    one transport toggle the operator changed (slack off -> on)."""
    home = patched / "anna"
    vault = patched / "customvault"
    home.mkdir(parents=True, exist_ok=True)
    yaml_text = _EXISTING_YAML_WITH_CLI.replace("/home/op/customvault", str(vault))
    (home / "anna.yaml").write_text(yaml_text, encoding="utf-8")
    (home / ".env").write_text(_EXISTING_ENV.replace("/home/op/customvault", str(vault)), encoding="utf-8")
    os.chmod(home / ".env", 0o600)

    # Telegram stays on; flip Slack on (it was off in the existing config).
    stdin = "\n".join(
        [
            "",            # vault root -> default (existing path)
            "y",           # Enable Telegram?
            "y",           # Enable Slack? (flip from off to on)
            "n",           # detailed telegram steps
            "tg-new",      # telegram bot token
            "tg-new",      # confirm
            "5550000",     # numeric id
            "n",           # detailed slack steps
            "xoxb-new",    # slack bot token
            "xoxb-new",    # confirm
            "xapp-new",    # slack app token
            "xapp-new",    # confirm
            "C0SENTINEL",  # slack admin channel
            "max",         # auth mode
            "Op",          # persona name
            "", "", "", "", "", "", "",  # remaining persona prompts
            "",            # disable web? -> default no
            "",            # write + start?
        ]
    ) + "\n"

    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--reconfigure"],
        input=stdin,
        catch_exceptions=False,
    )
    # (a) Did NOT raise/abort on an otherwise-valid config.
    assert result.exit_code == 0, result.output
    assert "failed validation" not in result.output

    # Re-validate the written config so the assertions read typed fields, not
    # string-matched YAML lines.
    from anna_web.config_store import ConfigStore

    cfg = ConfigStore(anna_home=home).load_validated()

    # (b) Customized cli block survived intact.
    assert cfg.transports.cli.socket_path == "/custom/run/anna.sock"
    assert cfg.transports.cli.idle_gap_minutes == 99
    assert cfg.transports.cli.framing == "ndjson"

    # (c) Untouched sentinel section survived (housekeeping + admin.startup_alert).
    assert cfg.housekeeping.daily_sweep_time == "04:42"
    assert cfg.admin.startup_alert is False  # operator value preserved, not forced True

    # (d) The toggle the operator changed was applied.
    assert cfg.transports.slack.enabled is True
    assert cfg.transports.telegram.enabled is True


def test_fresh_install_still_uses_template_and_overwrite(patched):
    """Regression guard: reconfigure=False writes via the original static
    template + full .env overwrite, unchanged."""
    result, home = _run_wizard(patched, [], _telegram_inputs())
    assert result.exit_code == 0, result.output
    yaml_text = (home / "anna.yaml").read_text()
    # The static template's banner comment is the fingerprint of the
    # fresh-install render path.
    assert "Generated by `anna-setup`" in yaml_text
    assert "daily_sweep_time: \"03:17\"" in yaml_text  # template default
    env_text = (home / ".env").read_text()
    assert env_text.startswith("ANNA_HOME=")  # full overwrite layout


def test_reconfigure_invalid_existing_yaml_clear_error(patched):
    """A reconfigure against an anna.yaml that fails AnnaConfig validation
    produces a clear message, not a raw traceback."""
    home = patched / "anna"
    home.mkdir(parents=True, exist_ok=True)
    # Invalid permission_mode in the runtime section. The web-toggle path only
    # overwrites the `web` section, so this pre-existing violation survives into
    # the full-document validation that write_section runs and makes it raise.
    (home / "anna.yaml").write_text(
        "runtime:\n  permission_mode: totally-bogus\nweb:\n  enabled: false\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        wizard.main,
        ["--anna-home", str(home), "--reconfigure", "--enable-web"],
        input="",
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "failed validation" in result.output
    # No Python traceback leaked.
    assert "Traceback (most recent call last)" not in result.output
