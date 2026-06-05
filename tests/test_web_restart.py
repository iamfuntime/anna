"""Tests for the ``/restart`` route + :class:`RestartManager` (subtask 10).

Eight cases pin the contract subtask 10 ships:

1. ``RestartManager.restart()`` happy path via dbus — verifies that
   the dbus client is invoked with ``Manager.RestartUnit(unit, "replace")``
   and the result advertises ``method="dbus"`` plus the job path.
2. dbus failure → subprocess fallback succeeds. ``method="subprocess"``,
   ``error is None``.
3. Both paths fail → ``method="subprocess"`` (the path tried last),
   ``error`` contains both error strings.
4. ``RestartManager.also_health_probe()`` over dbus returns ``"active"``
   when systemd publishes ``ActiveState=active``.
5. ``POST /restart`` returns 200 + the restart-button partial in the
   "restarting" state, mentioning "dbus".
6. ``POST /restart`` returns 500 + an error toast when the manager
   reports failure.
7. ``GET /restart`` (rehydrate-to-idle path) returns 200 + the partial
   in the idle state. ``HEAD`` is not exposed; ``DELETE`` returns 405.
8. The unit name is pinned at construction — :meth:`restart` accepts
   no unit argument so there is no path for a request to redirect the
   restart at a different unit.

dbus is never actually contacted. ``MessageBus`` is monkey-patched
with an ``AsyncMock`` for the bus + proxy + interface chain, so the
tests are fast and work on hosts without a session bus (CI, the build
host, sandboxes without ``$XDG_RUNTIME_DIR/bus``).
"""

from __future__ import annotations

import inspect
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from anna_web.app import app
from anna_web.restart import RestartManager, RestartResult, build_restart_argv


# ---------------------------------------------------------------------------
# build_restart_argv() — the detached restart command construction.
# ---------------------------------------------------------------------------


def test_build_restart_argv_detaches_via_systemd_run() -> None:
    """The restart fallback wraps systemctl in a transient systemd scope.

    A bare ``systemctl --user restart anna.service`` issued from inside
    anna.service's own cgroup gets SIGKILLed mid-restart (the stop phase
    kills the whole cgroup, including the systemctl that would start the
    service back up). ``systemd-run --user --scope`` runs the restart in
    a fresh cgroup that outlives anna.service's, so the start survives.
    """
    assert build_restart_argv("anna.service") == [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "systemctl",
        "--user",
        "restart",
        "anna.service",
    ]


def test_build_restart_argv_parameterized_on_unit() -> None:
    """The unit name flows through to the tail of the argv unchanged."""
    argv = build_restart_argv("anna-web.service")
    assert argv[:7] == [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "systemctl",
        "--user",
        "restart",
    ]
    assert argv[-1] == "anna-web.service"


# ---------------------------------------------------------------------------
# Helpers — dbus_next mocking.
# ---------------------------------------------------------------------------


def _make_dbus_chain(*, restart_job_path: str = "/org/freedesktop/systemd1/job/42"):
    """Build a fake MessageBus class whose connect() chain succeeds.

    Returns (FakeBusClass, fake_manager_iface) so a test can assert
    on ``fake_manager_iface.call_restart_unit.await_args``.
    """
    fake_manager = MagicMock()
    fake_manager.call_restart_unit = AsyncMock(return_value=restart_job_path)
    fake_manager.call_get_unit = AsyncMock(
        return_value="/org/freedesktop/systemd1/unit/anna_2eservice"
    )

    fake_unit_iface = MagicMock()
    fake_unit_iface.get_active_state = AsyncMock(return_value="active")

    def _get_interface(iface_name: str):
        if iface_name.endswith(".Manager"):
            return fake_manager
        return fake_unit_iface

    fake_proxy = MagicMock()
    fake_proxy.get_interface.side_effect = _get_interface

    fake_bus = MagicMock()
    fake_bus.introspect = AsyncMock(return_value=MagicMock(name="introspection"))
    fake_bus.get_proxy_object = MagicMock(return_value=fake_proxy)
    fake_bus.disconnect = MagicMock()

    # MessageBus(bus_type=...).connect() must return the fake bus.
    instance = MagicMock()
    instance.connect = AsyncMock(return_value=fake_bus)

    class FakeBusClass:
        def __init__(self, *args, **kwargs):
            pass

        def __new__(cls, *args, **kwargs):  # noqa: D401 - return our singleton
            return instance

    return FakeBusClass, fake_manager


# ---------------------------------------------------------------------------
# 1. dbus happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_dbus_happy_path() -> None:
    """dbus connect → introspect → RestartUnit("anna.service", "replace")."""
    FakeBusClass, fake_manager = _make_dbus_chain(
        restart_job_path="/org/freedesktop/systemd1/job/7"
    )
    manager = RestartManager(target_unit="anna.service")

    with patch("dbus_next.aio.MessageBus", FakeBusClass):
        result = await manager.restart()

    assert result.method == "dbus"
    assert result.error is None
    assert result.job_id == "/org/freedesktop/systemd1/job/7"
    fake_manager.call_restart_unit.assert_awaited_once_with("anna.service", "replace")


# ---------------------------------------------------------------------------
# 2. dbus raises → subprocess fallback succeeds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_falls_back_to_subprocess() -> None:
    """When the dbus connect fails, the subprocess path runs cleanly."""

    class ExplodingBus:
        def __init__(self, *args, **kwargs):
            pass

        def __new__(cls, *args, **kwargs):
            inst = MagicMock()
            inst.connect = AsyncMock(side_effect=RuntimeError("no session bus"))
            return inst

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0

    manager = RestartManager(target_unit="anna.service")

    with (
        patch("dbus_next.aio.MessageBus", ExplodingBus),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)) as p,
    ):
        result = await manager.restart()

    assert result.method == "subprocess"
    assert result.error is None
    assert result.job_id is None
    # Subprocess invocation shape: the restart is detached from anna's
    # own cgroup via ``systemd-run --user --scope`` so it survives the
    # SIGKILL of the cgroup during the stop phase.
    call_args = p.await_args.args
    assert call_args[:5] == (
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "systemctl",
    )
    assert call_args[5:8] == ("--user", "restart", "anna.service")


# ---------------------------------------------------------------------------
# 3. Both paths fail → error captures both.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_both_paths_fail() -> None:
    """When dbus and subprocess both blow up the error carries both reasons."""

    class ExplodingBus:
        def __init__(self, *args, **kwargs):
            pass

        def __new__(cls, *args, **kwargs):
            inst = MagicMock()
            inst.connect = AsyncMock(side_effect=RuntimeError("dbus busted"))
            return inst

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"systemctl: unit not found"))
    fake_proc.returncode = 5

    manager = RestartManager(target_unit="anna.service")

    with (
        patch("dbus_next.aio.MessageBus", ExplodingBus),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
    ):
        result = await manager.restart()

    assert result.method == "subprocess"
    assert result.error is not None
    # Both paths' errors stitched together.
    assert "dbus" in result.error
    assert "systemctl" in result.error
    assert "dbus busted" in result.error
    assert "unit not found" in result.error


# ---------------------------------------------------------------------------
# 4. also_health_probe() via dbus → "active".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_also_health_probe_active_via_dbus() -> None:
    """dbus path returns ``{"active_state": "active", "method": "dbus"}``."""
    FakeBusClass, _fake_manager = _make_dbus_chain()
    manager = RestartManager(target_unit="anna.service")

    with patch("dbus_next.aio.MessageBus", FakeBusClass):
        out = await manager.also_health_probe()

    assert out == {"active_state": "active", "method": "dbus"}


# ---------------------------------------------------------------------------
# Route tests — POST /restart and friends.
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient with the module-level app, restart_manager left as-is.

    Individual route tests overwrite ``app.state.restart_manager`` with
    a per-test mock and then restore the original afterwards. Restore
    is unconditional so a partial-state test doesn't leak a mock into
    later tests.
    """
    original = getattr(app.state, "restart_manager", None)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if original is not None:
            app.state.restart_manager = original


# ---------------------------------------------------------------------------
# 5. POST /restart → 200 + restarting partial mentions "dbus".
# ---------------------------------------------------------------------------


def test_post_restart_success(client: TestClient) -> None:
    fake = MagicMock()
    fake.target_unit = "anna.service"
    fake.restart = AsyncMock(
        return_value=RestartResult(
            method="dbus",
            job_id="/org/freedesktop/systemd1/job/3",
            error=None,
        )
    )
    app.state.restart_manager = fake

    response = client.post("/restart")

    assert response.status_code == 200
    body = response.text
    assert "Restarting" in body
    assert "anna.service" in body
    assert "dbus" in body
    fake.restart.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. POST /restart failure → 500 + error toast.
# ---------------------------------------------------------------------------


def test_post_restart_failure_returns_500(client: TestClient) -> None:
    fake = MagicMock()
    fake.target_unit = "anna.service"
    fake.restart = AsyncMock(
        return_value=RestartResult(
            method="subprocess",
            job_id=None,
            error="dbus: RuntimeError: no bus; systemctl: RuntimeError: exit 5",
        )
    )
    app.state.restart_manager = fake

    response = client.post("/restart")

    assert response.status_code == 500
    body = response.text
    # Idle partial re-rendered for retry, plus an error toast.
    assert "Restart failed" in body
    assert "no bus" in body or "exit 5" in body
    # The submit button is still rendered (idle state) so the operator
    # can retry without a manual page reload.
    assert "Restart anna.service" in body


# ---------------------------------------------------------------------------
# 7. /restart verb handling — GET allowed (rehydrate), DELETE rejected.
# ---------------------------------------------------------------------------


def test_get_restart_returns_idle_partial(client: TestClient) -> None:
    """GET /restart returns the idle button partial for the polling rehydrate."""
    fake = MagicMock()
    fake.target_unit = "anna.service"
    app.state.restart_manager = fake

    response = client.get("/restart")

    assert response.status_code == 200
    body = response.text
    assert "Restart anna.service" in body
    # The "restarting" wrapper must NOT appear on the idle render.
    assert 'data-restart-partial="idle"' in body
    assert 'data-restart-partial="restarting"' not in body


def test_delete_restart_returns_405(client: TestClient) -> None:
    """No DELETE handler is registered — Starlette returns 405."""
    fake = MagicMock()
    fake.target_unit = "anna.service"
    app.state.restart_manager = fake
    response = client.delete("/restart")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# 8. Unit is pinned at construction — restart() takes no unit arg.
# ---------------------------------------------------------------------------


def test_restart_signature_accepts_no_unit_argument() -> None:
    """No request body can redirect the restart at a different unit.

    The contract is structural: :meth:`RestartManager.restart` takes
    only ``self``. Anyone trying to pass a unit name (as a kwarg, as a
    positional) gets a :class:`TypeError` at the call site, long before
    any dbus or subprocess code runs.
    """
    sig = inspect.signature(RestartManager.restart)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert params == [], (
        "RestartManager.restart() must accept no parameters other than self "
        "so no request body can influence which unit is restarted."
    )

    # And belt-and-suspenders: calling with any positional or keyword
    # raises TypeError.
    manager = RestartManager(target_unit="anna.service")
    with pytest.raises(TypeError):
        # type: ignore[call-arg]
        manager.restart("some-other.service")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        # type: ignore[call-arg]
        manager.restart(unit="some-other.service")  # type: ignore[call-arg]
