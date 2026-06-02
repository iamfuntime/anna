"""Phase 2.5 web dashboard — installer wiring tests.

Subtask 13 of the Phase 2.5 buildout plan. Covers:

1. ``anna-web.service`` parses as a valid INI / systemd unit and the
   load-bearing lines are present verbatim.
2. The template is discoverable via ``importlib.resources`` the same
   way the daemon's ``anna.service`` is, so a uv-tool install picks it
   up without source-tree assumptions.
3. ``_install_web_unit`` writes the unit to ``~/.config/systemd/user/``
   regardless of the enable/disable choice, and drives ``systemctl``
   accordingly.
4. The wizard's anna.yaml renderer writes ``web.enabled: true`` by
   default and ``web.enabled: false`` when ``state.web_enabled`` is
   False — exercising the ``--disable-web`` / ``--enable-web`` paths
   end-to-end.
"""

from __future__ import annotations

import configparser
from importlib import resources
from pathlib import Path

import pytest

from anna.setup import wizard


# ---------------------------------------------------------------------------
# Template-level invariants
# ---------------------------------------------------------------------------


def _read_template() -> str:
    template = resources.files("anna.setup.templates").joinpath("anna-web.service")
    return template.read_text(encoding="utf-8")


def test_template_is_importable_as_resource():
    """anna.setup.templates.anna-web.service must be packaged and readable."""
    template = resources.files("anna.setup.templates").joinpath("anna-web.service")
    assert template.is_file()
    body = template.read_text(encoding="utf-8")
    assert body.strip().startswith("[Unit]")


def test_template_parses_as_systemd_unit():
    """ConfigParser with allow_no_value=True is a close enough lint for systemd ini."""
    body = _read_template()
    parser = configparser.ConfigParser(allow_no_value=True, strict=False, interpolation=None)
    parser.read_string(body)

    assert parser.has_section("Unit")
    assert parser.has_section("Service")
    assert parser.has_section("Install")

    assert parser.get("Unit", "Description") == "ANNA Web Dashboard"
    assert parser.get("Service", "Type") == "simple"
    assert parser.get("Service", "Restart") == "on-failure"
    assert parser.get("Install", "WantedBy") == "default.target"


def test_template_critical_lines_present_verbatim():
    """The lines the dashboard's runtime contract depends on must match exactly."""
    body = _read_template()
    # ANNA_HOME-anchored config load (commit 09b1562).
    assert "Environment=ANNA_HOME=%h/anna" in body
    # Secrets surface mirrors the daemon (commit 09b1562); leading `-`
    # keeps fresh installs starting cleanly before .env exists.
    assert "EnvironmentFile=-%h/anna/.env" in body
    # uv-tool shim path (commit eab9132).
    assert "ExecStart=%h/.local/bin/anna-web" in body
    # Generous memory ceiling as documented in the plan.
    assert "MemoryMax=512M" in body
    assert "Restart=on-failure" in body
    assert "RestartSec=10" in body


def test_template_has_no_bindsto_anna_service():
    """The dashboard must survive a daemon crash so the operator can
    inspect what went wrong and click Restart — BindsTo would tear it
    down with the daemon."""
    body = _read_template()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("BindsTo="), (
            "anna-web.service must NOT BindsTo anna.service: "
            f"found {stripped!r}"
        )


# ---------------------------------------------------------------------------
# _install_web_unit — file installed, systemctl driven per choice
# ---------------------------------------------------------------------------


class _FakeRun:
    """Records every subprocess.run invocation so the test can assert which
    systemctl verbs the wizard issued."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, args, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        self.calls.append(list(args))

        class _Completed:
            pass

        completed = _Completed()
        completed.returncode = self.returncode  # type: ignore[attr-defined]
        completed.stdout = ""  # type: ignore[attr-defined]
        completed.stderr = self.stderr  # type: ignore[attr-defined]
        return completed


def _which_with(present: set[str]):
    def which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in present else None

    return which


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    """Anchor ``%h`` (HOME) to tmp_path so the unit file lands in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_install_web_unit_writes_file_when_enabled(monkeypatch, tmp_home):
    fake_run = _FakeRun()
    monkeypatch.setattr(wizard.shutil, "which", _which_with({"systemctl"}))
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    state = wizard.WizardState(
        anna_home=tmp_home / "anna",
        vault_root=tmp_home / "anna" / "vault",
        web_enabled=True,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    wizard._install_web_unit(state)

    unit_path = tmp_home / ".config" / "systemd" / "user" / "anna-web.service"
    assert unit_path.is_file(), "anna-web.service must land in ~/.config/systemd/user/"
    body = unit_path.read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/anna-web" in body

    verbs = [" ".join(call) for call in fake_run.calls]
    assert any("daemon-reload" in v for v in verbs), verbs
    assert any("enable --now anna-web.service" in v for v in verbs), verbs


def test_install_web_unit_disables_when_opted_out(monkeypatch, tmp_home):
    fake_run = _FakeRun()
    monkeypatch.setattr(wizard.shutil, "which", _which_with({"systemctl"}))
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    state = wizard.WizardState(
        anna_home=tmp_home / "anna",
        vault_root=tmp_home / "anna" / "vault",
        web_enabled=False,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    wizard._install_web_unit(state)

    # File still lands on disk so flipping back later is a one-toggle move.
    unit_path = tmp_home / ".config" / "systemd" / "user" / "anna-web.service"
    assert unit_path.is_file()

    verbs = [" ".join(call) for call in fake_run.calls]
    assert any("disable anna-web.service" in v for v in verbs), verbs
    # And we must NOT have asked systemd to enable it.
    assert not any("enable --now anna-web.service" in v for v in verbs), verbs


def test_install_web_unit_no_systemctl_still_writes_file(monkeypatch, tmp_home):
    """On WSL-without-systemd / macOS the wizard degrades gracefully:
    the unit file lands on disk and no subprocess call is made."""
    fake_run = _FakeRun()
    monkeypatch.setattr(wizard.shutil, "which", _which_with(set()))
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    state = wizard.WizardState(
        anna_home=tmp_home / "anna",
        vault_root=tmp_home / "anna" / "vault",
        web_enabled=True,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    wizard._install_web_unit(state)

    unit_path = tmp_home / ".config" / "systemd" / "user" / "anna-web.service"
    assert unit_path.is_file()
    assert fake_run.calls == [], (
        "no systemctl available -> no subprocess calls should have been made"
    )


# ---------------------------------------------------------------------------
# _write_anna_yaml — web.enabled reflects state.web_enabled
# ---------------------------------------------------------------------------


def _base_state(tmp_path: Path, *, web_enabled: bool) -> wizard.WizardState:
    return wizard.WizardState(
        anna_home=tmp_path / "anna",
        vault_root=tmp_path / "anna" / "vault",
        use_telegram=True,
        telegram_admin_chat_id="999000",
        auth_mode="max",
        web_enabled=web_enabled,
    )


def test_write_anna_yaml_default_keeps_web_enabled_true(tmp_path):
    state = _base_state(tmp_path, web_enabled=True)
    yaml_path = tmp_path / "anna.yaml"
    wizard._write_anna_yaml(state, yaml_path)

    body = yaml_path.read_text(encoding="utf-8")
    assert "web:" in body
    # Pull the web block out so we don't accidentally match
    # transports.slack.enabled or similar.
    web_block = body.split("web:", 1)[1]
    assert "enabled: true" in web_block.splitlines()[1]
    assert "host: 127.0.0.1" in web_block
    assert "port: 8765" in web_block
    assert "target_unit: anna.service" in web_block


def test_write_anna_yaml_disable_web_flips_enabled_false(tmp_path):
    state = _base_state(tmp_path, web_enabled=False)
    yaml_path = tmp_path / "anna.yaml"
    wizard._write_anna_yaml(state, yaml_path)

    body = yaml_path.read_text(encoding="utf-8")
    assert "web:" in body
    web_block = body.split("web:", 1)[1]
    assert "enabled: false" in web_block.splitlines()[1]


# ---------------------------------------------------------------------------
# CLI flag → state propagation
# ---------------------------------------------------------------------------


def test_disable_web_flag_skips_prompt_and_sets_enabled_false(monkeypatch, tmp_path):
    """Drive only the prompt step. ``--disable-web`` parses through click into
    ``state.web_enabled=False`` + ``state.web_prompt_resolved=True``; the
    prompt step then short-circuits without asking for stdin."""
    state = wizard.WizardState(
        anna_home=tmp_path / "anna",
        vault_root=tmp_path / "anna" / "vault",
        web_enabled=False,
        web_prompt_resolved=True,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    # If the step tried to read stdin, click.confirm would raise EOFError
    # in a test context — proving the short-circuit by absence of failure.
    wizard.step_web_dashboard(state)
    assert state.web_enabled is False


def test_enable_web_flag_skips_prompt_and_keeps_enabled_true(tmp_path):
    state = wizard.WizardState(
        anna_home=tmp_path / "anna",
        vault_root=tmp_path / "anna" / "vault",
        web_enabled=True,
        web_prompt_resolved=True,
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)
    wizard.step_web_dashboard(state)
    assert state.web_enabled is True
