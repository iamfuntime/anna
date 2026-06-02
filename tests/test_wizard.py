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
