"""Regression test: the wizard's anna.service template must be loadable
from the installed package, not just the source-tree-relative path that
worked under the old ``pip install -e .`` install model.
"""

from __future__ import annotations

from importlib import resources

import pytest

from anna.setup import wizard


def test_template_is_importable_as_resource():
    """anna.setup.templates.anna.service must be packaged and readable."""
    template = resources.files("anna.setup.templates").joinpath("anna.service")
    assert template.is_file()
    contents = template.read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/anna" in contents
    assert "Environment=ANNA_HOME=%h/anna" in contents
    assert "WorkingDirectory=%h/anna" in contents


def test_install_systemd_unit_writes_template(tmp_path, monkeypatch):
    """_install_systemd_unit must read the bundled template and write
    a verbatim copy to ~/.config/systemd/user/anna.service."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Disable the start-and-probe path; we only care about the write.
    monkeypatch.setattr(wizard, "_start_and_probe", lambda state: None)

    state = wizard.WizardState(
        anna_home=tmp_path / "anna",
        vault_root=tmp_path / "anna" / "vault",
    )
    state.anna_home.mkdir(parents=True, exist_ok=True)

    result = wizard._install_systemd_unit(state)
    assert result is None  # _start_and_probe stubbed -> None

    unit_path = tmp_path / ".config" / "systemd" / "user" / "anna.service"
    assert unit_path.is_file()
    body = unit_path.read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/anna" in body
    assert "Environment=ANNA_HOME=%h/anna" in body
