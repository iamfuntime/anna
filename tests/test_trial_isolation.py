"""A temp-ANNA_HOME trial must not reach the live ~/anna state or restart the live service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import anna.config as config_module
from anna.config import (
    AnnaConfig,
    effective_restart_unit,
    foreign_home_paths,
    is_isolated_home as _real_predicate,
    normalize_unit_name,
    real_default_home,
    refuse_foreign_home,
)
from anna.setup import wizard
from anna_web.app import create_app
from anna_web.restart import RestartManager


@pytest.fixture
def real_predicate():
    """Kept as a no-op marker: the REAL predicate is always in play now (no suite-wide stub)."""


@pytest.fixture
def fake_real_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "pretend-real" / "anna"
    home.mkdir(parents=True)
    monkeypatch.setattr(config_module, "real_default_home", lambda: home)
    return home


def _trial_cfg(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "trial" / "anna")
    return cfg


def test_state_defaults_follow_anna_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNA_HOME", str(tmp_path / "trial"))
    cfg = AnnaConfig()
    assert cfg.scheduler.resolved_state_path == tmp_path / "trial" / "schedules.yaml"
    assert cfg.vault.resolved_path == tmp_path / "trial" / "vault"
    assert cfg.transports.cli.resolved_socket_path == tmp_path / "trial" / "anna.sock"


def test_isolated_home_cannot_restart_the_live_unit(real_predicate, fake_real_home, tmp_path):
    cfg = _trial_cfg(tmp_path)
    assert cfg.web.target_unit == "anna.service"  # compatible default is unchanged
    assert effective_restart_unit(cfg) is None
    cfg.web.target_unit = "anna-trial.service"
    assert effective_restart_unit(cfg) == "anna-trial.service"  # explicit dedicated unit is honoured
    cfg.web.target_unit = ""
    assert effective_restart_unit(cfg) is None


@pytest.mark.parametrize("alias", ["anna", "anna.service", " anna.service ", "ANNA.service", "anna-web.service",
                                   "anna-web", "Anna-Web", "anna\n", "../anna.service", "x/anna.service"])
def test_isolated_home_cannot_restart_any_spelling_of_a_live_unit(real_predicate, fake_real_home, tmp_path, alias):
    cfg = _trial_cfg(tmp_path)
    cfg.web.target_unit = alias
    assert effective_restart_unit(cfg) is None


def test_unit_names_are_normalised_the_way_systemd_reads_them(real_predicate, fake_real_home, tmp_path):
    assert normalize_unit_name("anna-trial") == "anna-trial.service"
    assert normalize_unit_name(" anna-trial.service ") == "anna-trial.service"
    assert normalize_unit_name("a b") is None and normalize_unit_name("") is None and normalize_unit_name(None) is None
    cfg = _trial_cfg(tmp_path)
    cfg.web.target_unit = "anna-trial"
    assert effective_restart_unit(cfg) == "anna-trial.service"
    live = AnnaConfig()
    object.__setattr__(live, "anna_home", fake_real_home)
    live.web.target_unit = "anna"
    assert effective_restart_unit(live) == "anna.service"  # the live home keeps its own unit


def test_real_default_home_ignores_a_home_override(monkeypatch, tmp_path):
    import pwd
    import os

    monkeypatch.setenv("HOME", str(tmp_path))
    assert real_default_home() == Path(pwd.getpwuid(os.getuid()).pw_dir) / "anna"
    assert _real_predicate(tmp_path / "anna")  # an overridden $HOME does not make its ~/anna "the real one"


def test_real_home_keeps_default_restart_unit(real_predicate, fake_real_home):
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", fake_real_home)
    assert effective_restart_unit(cfg) == "anna.service"


def test_foreign_paths_into_real_home_are_detected(real_predicate, fake_real_home, tmp_path):
    cfg = _trial_cfg(tmp_path)
    assert foreign_home_paths(cfg) == []
    cfg.scheduler.state_path = str(fake_real_home / "schedules.yaml")
    cfg.vault.path = str(fake_real_home / "vault")
    assert {label for label, _ in foreign_home_paths(cfg)} == {"scheduler.state_path", "vault.path"}
    assert is_isolated_home_for(cfg)


def is_isolated_home_for(cfg) -> bool:
    return _real_predicate(cfg.anna_home)


def test_daemon_refuses_to_start_when_trial_points_into_real_home(real_predicate, fake_real_home, tmp_path, capsys):
    import anna.__main__ as entry

    cfg = _trial_cfg(tmp_path)
    cfg.scheduler.state_path = str(fake_real_home / "schedules.yaml")
    with patch.object(entry, "load_config", return_value=cfg), patch.object(entry.asyncio, "run") as run:
        assert entry.run_daemon() == 2
        run.assert_not_called()
    assert "refusing to start" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_disabled_manager_never_touches_systemd():
    mgr = RestartManager(target_unit=None)
    with patch.object(RestartManager, "_restart_via_dbus", AsyncMock(side_effect=AssertionError("dbus"))), \
         patch.object(RestartManager, "_restart_via_subprocess", AsyncMock(side_effect=AssertionError("systemctl"))):
        result = await mgr.restart()
        assert not result.ok and "disabled" in (result.error or "")
        assert (await mgr.also_health_probe())["active_state"] == "unknown"


def test_dashboard_restart_buttons_are_disabled_in_a_trial(real_predicate, fake_real_home, tmp_path):
    cfg = _trial_cfg(tmp_path)
    cfg.anna_home.mkdir(parents=True)
    (cfg.anna_home / "anna.yaml").write_text((Path(__file__).parents[1] / "anna.yaml.example").read_text())
    with patch.object(RestartManager, "_restart_via_dbus", AsyncMock(side_effect=AssertionError("dbus"))), \
         patch.object(RestartManager, "_restart_via_subprocess", AsyncMock(side_effect=AssertionError("systemctl"))):
        client = TestClient(create_app(cfg))
        for page in ("/", "/config", "/settings", "/restart"):
            body = client.get(page).text
            assert 'hx-post="/restart"' not in body, page
        assert 'data-restart-partial="disabled"' in client.get("/config").text
        assert client.post("/restart").status_code == 403


def test_guard_covers_credentials_obsidian_and_config_paths(real_predicate, fake_real_home, tmp_path, monkeypatch):
    cfg = _trial_cfg(tmp_path)
    cfg.google.oauth_client_file = str(fake_real_home / "state" / "google" / "oauth_client.json")
    cfg.integrations.obsidian.vault_path = fake_real_home / "vault"
    monkeypatch.setenv("ANNA_CONFIG_PATH", str(fake_real_home / "anna.yaml"))
    labels = {label for label, _ in foreign_home_paths(cfg)}
    assert {"google.oauth_client_file", "integrations.obsidian.vault_path", "config file"} <= labels


@pytest.mark.parametrize("entry", ["ask", "chat", "admin"])
def test_other_entry_points_refuse_a_trial_that_reaches_the_real_home(real_predicate, fake_real_home, tmp_path,
                                                                     monkeypatch, capsys, entry):
    cfg = _trial_cfg(tmp_path)
    cfg.transports.cli.socket_path = str(fake_real_home / "anna.sock")
    assert refuse_foreign_home(cfg, "anna " + entry) is True
    assert "refusing to run" in capsys.readouterr().err
    if entry == "ask":
        import anna.cli.ask as mod
        monkeypatch.setattr(mod, "load_config", lambda: cfg)
        monkeypatch.setattr("sys.argv", ["anna", "hello"])
        assert mod.main() == 2
    elif entry == "chat":
        import anna.cli.chat as mod
        monkeypatch.setattr(mod, "load_config", lambda: cfg)
        assert mod.main() == 2
    else:
        import anna.cli.admin as mod
        monkeypatch.setattr(mod, "load_config", lambda: cfg)
        with pytest.raises(SystemExit) as exc:
            mod._guarded_config()
        assert exc.value.code == 2


def test_clean_trial_passes_the_guard(real_predicate, fake_real_home, tmp_path):
    assert refuse_foreign_home(_trial_cfg(tmp_path), "anna ask") is False


# ---------------------------------------------------------------------------
# anna setup (M1): a trial home never installs, enables, starts or disables live units
# ---------------------------------------------------------------------------

def _wizard_state(tmp_path: Path, **kw) -> "wizard.WizardState":
    home = tmp_path / "trial" / "anna"
    home.mkdir(parents=True, exist_ok=True)
    return wizard.WizardState(anna_home=home, vault_root=home / "vault", use_telegram=True,
                              telegram_admin_chat_id="999000", **kw)


def test_setup_in_a_trial_home_never_touches_systemd(real_predicate, fake_real_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "scratch-home"))
    for name in ("run", "Popen", "check_call", "check_output", "call"):
        monkeypatch.setattr(wizard.subprocess, name,
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("systemd/subprocess must not run")))
    monkeypatch.setattr(wizard.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(wizard.click, "confirm", lambda *a, **k: True)
    for web in (True, False):
        state = _wizard_state(tmp_path, web_enabled=web)
        assert wizard.step_final_wiring(state) is None
    units = tmp_path / "scratch-home" / ".config" / "systemd"
    assert not units.exists()  # no unit file written anywhere
    body = (tmp_path / "trial" / "anna" / "anna.yaml").read_text()
    assert 'target_unit: ""' in body and f"socket_path: {tmp_path / 'trial' / 'anna' / 'anna.sock'}" in body
    assert "~/anna" not in body.split("cli:", 1)[1].split("vault:", 1)[0]


def test_setup_in_the_real_home_still_installs_units(fake_real_home, tmp_path, monkeypatch):
    state = wizard.WizardState(anna_home=fake_real_home, vault_root=fake_real_home / "vault", use_telegram=True,
                               telegram_admin_chat_id="1")
    assert wizard._live_units_blocked(state) is False
