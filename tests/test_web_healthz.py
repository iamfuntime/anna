"""Tests for ``anna_web.routes.healthz_routes`` (subtask 11).

Required cases from the buildout brief:

1. GET /healthz with a healthy app returns status ok + both flags true.
2. GET /healthz with a dead anna returns ``anna_running: false`` and
   keeps ``status: "ok"``.
3. GET /healthz when the probe raises returns ``anna_running: false``
   without 500ing.
4. GET /healthz when ``app.state.restart_manager`` is absent returns
   ``anna_running: false`` without 500ing (subtask 10 hasn't landed
   in this fixture).
5. GET /healthz when ``app.state.cfg`` is broken returns
   ``config_loaded: false``.
6. The response is JSON (content-type).
7. POST/PUT/DELETE to /healthz returns 405.

Fixture builds a fresh FastAPI app with the healthz router included
and overridable ``app.state.cfg`` + ``app.state.restart_manager``
slots, so the tests don't have to drag in the full ``create_app``
wiring just to flip a couple of state fields.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.routes import healthz_routes


def _build_app(
    *,
    cfg: Any = "default",
    restart_manager: Any = "unset",
) -> FastAPI:
    """Build a minimal FastAPI app with the healthz router mounted.

    Sentinel values ``"default"`` / ``"unset"`` distinguish "leave the
    attribute at the test-default" from "explicitly set this to the
    passed value (including None)". Tests that want to simulate
    subtask 10 not having landed pass ``restart_manager="unset"``;
    tests that want it set to ``None`` pass ``restart_manager=None``.
    """
    app = FastAPI()
    app.include_router(healthz_routes.router)

    if cfg == "default":
        app.state.cfg = AnnaConfig()
    else:
        app.state.cfg = cfg

    if restart_manager != "unset":
        app.state.restart_manager = restart_manager
    # else: leave the attribute off entirely so getattr-with-default
    # exercises the missing-attribute branch.

    return app


def _mock_restart_manager(probe_return: Any) -> MagicMock:
    """RestartManager mock whose ``also_health_probe`` is an AsyncMock."""
    mgr = MagicMock()
    mgr.also_health_probe = AsyncMock(return_value=probe_return)
    return mgr


def _mock_restart_manager_raising(exc: Exception) -> MagicMock:
    mgr = MagicMock()
    mgr.also_health_probe = AsyncMock(side_effect=exc)
    return mgr


# ---------------------------------------------------------------------------
# 1. Healthy app → status ok + both flags true.
# ---------------------------------------------------------------------------


def test_healthz_healthy() -> None:
    mgr = _mock_restart_manager({"active_state": "active", "method": "dbus"})
    app = _build_app(restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "anna_running": True,
        "config_loaded": True,
    }
    mgr.also_health_probe.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Dead anna → anna_running false, status still ok.
# ---------------------------------------------------------------------------


def test_healthz_dead_anna() -> None:
    mgr = _mock_restart_manager({"active_state": "inactive", "method": "dbus"})
    app = _build_app(restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["anna_running"] is False
    assert body["config_loaded"] is True


# ---------------------------------------------------------------------------
# 3. Probe raises → anna_running false, no 500.
# ---------------------------------------------------------------------------


def test_healthz_probe_raises() -> None:
    mgr = _mock_restart_manager_raising(RuntimeError("dbus unreachable"))
    app = _build_app(restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["anna_running"] is False
    assert body["config_loaded"] is True


# ---------------------------------------------------------------------------
# 4. RestartManager absent → anna_running false, no 500.
# ---------------------------------------------------------------------------


def test_healthz_restart_manager_missing() -> None:
    # Don't pass restart_manager at all — attribute is missing,
    # mirroring the order-of-landing case where subtask 10's
    # RestartManager wiring isn't on app.state yet.
    app = _build_app()
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["anna_running"] is False
    assert body["config_loaded"] is True


def test_healthz_restart_manager_none() -> None:
    """Explicit ``None`` (set but not initialized) behaves the same as missing."""
    app = _build_app(restart_manager=None)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["anna_running"] is False


# ---------------------------------------------------------------------------
# 5. Broken cfg → config_loaded false.
# ---------------------------------------------------------------------------


def test_healthz_cfg_none() -> None:
    mgr = _mock_restart_manager({"active_state": "active", "method": "dbus"})
    app = _build_app(cfg=None, restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["config_loaded"] is False
    # anna_running still follows the probe — the two flags are
    # independent.
    assert body["anna_running"] is True


def test_healthz_cfg_wrong_type() -> None:
    """A cfg that isn't an AnnaConfig instance reports config_loaded false."""
    mgr = _mock_restart_manager({"active_state": "active", "method": "dbus"})
    app = _build_app(cfg={"not": "an AnnaConfig"}, restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["config_loaded"] is False


# ---------------------------------------------------------------------------
# 6. Content-Type is JSON.
# ---------------------------------------------------------------------------


def test_healthz_content_type_is_json() -> None:
    app = _build_app()
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "").lower()


# ---------------------------------------------------------------------------
# 7. Only GET is allowed. POST/PUT/DELETE → 405.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_healthz_rejects_mutating_methods(method: str) -> None:
    app = _build_app()
    client = TestClient(app)

    response = client.request(method.upper(), "/healthz")

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Bonus: probe returning unexpected shape doesn't crash the endpoint.
# ---------------------------------------------------------------------------


def test_healthz_probe_returns_non_dict() -> None:
    """Defensive: a future RestartManager that hands back a non-dict
    (or a test stub that does) should not break the probe — it should
    surface as ``anna_running: false``."""
    mgr = _mock_restart_manager("not a dict")
    app = _build_app(restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["anna_running"] is False


def test_healthz_probe_dict_missing_active_state() -> None:
    """A probe dict without the ``active_state`` key reports false."""
    mgr = _mock_restart_manager({"method": "dbus"})
    app = _build_app(restart_manager=mgr)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["anna_running"] is False


def test_healthz_probe_active_state_other_value() -> None:
    """``active_state`` values other than ``"active"`` report false."""
    for state in ("activating", "deactivating", "failed", "reloading", ""):
        mgr = _mock_restart_manager({"active_state": state, "method": "dbus"})
        app = _build_app(restart_manager=mgr)
        client = TestClient(app)

        response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json()["anna_running"] is False, (
            f"active_state={state!r} should map to anna_running=false"
        )
