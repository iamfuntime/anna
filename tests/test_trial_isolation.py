"""A temp-ANNA_HOME trial must not reach the live ~/anna state or restart the live service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import anna.config as config_module
from anna.config import AnnaConfig, effective_restart_unit, foreign_home_paths, is_isolated_home as _real_predicate
from anna_web.app import create_app
from anna_web.restart import RestartManager


@pytest.fixture
def real_predicate(monkeypatch):
    """Undo the suite-wide autouse stub so the real isolation rules run."""
    monkeypatch.setattr(config_module, "is_isolated_home", _real_predicate)


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


def test_real_home_keeps_default_restart_unit(real_predicate, fake_real_home):
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", fake_real_home)
    assert effective_restart_unit(cfg) == "anna.service"


def test_foreign_paths_into_real_home_are_detected(real_predicate, fake_real_home, tmp_path):
    cfg = _trial_cfg(tmp_path)
    assert foreign_home_paths(cfg) == [] or all(str(fake_real_home) not in str(p) for _, p in foreign_home_paths(cfg))
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
