"""Restart-the-daemon orchestration for the ANNA web dashboard.

Subtask 10 of the Phase 2.5 buildout. This module owns the operator's
"Restart anna.service" button: a small async manager that talks to the
operator's user systemd over dbus when it can, and falls back to
shelling out to ``systemctl --user restart <unit>`` when it can't.

The whole point of having ``dbus-next`` installed (subtask 1) is to
avoid forking a subprocess for the common path. dbus is in-process,
fast, and gives us back the systemd job object path so the caller can
correlate the restart request with the unit's subsequent state
transitions (used by :meth:`RestartManager.also_health_probe`, which
:mod:`anna_web.routes.healthz_routes` consumes — subtask 11).

Security posture: the target unit is **pinned at construction**, never
accepted from a request body. There is no path for a crafted POST to
:func:`anna_web.routes.restart_routes.post_restart` to ask for
``RestartUnit("any-other.service")`` — the dashboard's restart surface
restarts exactly one unit, the one the operator configured in
``web.target_unit`` (defaults to ``anna.service``).

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md`` — Architecture →
Restart endpoint — for the full design.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from anna.log import get_logger

_log = get_logger("anna.web.restart")

_SYSTEMD_BUS_NAME = "org.freedesktop.systemd1"
_SYSTEMD_OBJECT_PATH = "/org/freedesktop/systemd1"
_SYSTEMD_MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
_SYSTEMD_UNIT_IFACE = "org.freedesktop.systemd1.Unit"

RestartMethod = Literal["dbus", "subprocess"]


@dataclass(frozen=True)
class RestartResult:
    """Outcome of a single restart request.

    * ``method`` records which path actually carried the restart — the
      template surfaces it briefly to the operator ("Restarting via
      dbus…") so a quiet fallback to subprocess shows up in the UI.
    * ``job_id`` is the systemd job object path returned by
      ``Manager.RestartUnit``. Only the dbus path produces one; the
      subprocess path leaves it ``None``.
    * ``error`` is ``None`` on success. On failure it carries a short
      human-readable string. When both the dbus path and the subprocess
      fallback fail, both errors are concatenated so the route handler
      can surface the full picture in the toast.
    """

    method: RestartMethod
    job_id: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        """True iff the restart was successfully dispatched."""
        return self.error is None


class RestartManager:
    """Restart a pinned systemd user unit via dbus with a subprocess fallback.

    Construct once per process — the dashboard's :func:`create_app`
    instantiates a single :class:`RestartManager` and parks it on
    ``app.state.restart_manager``. Per-request handlers pull it from
    there and call :meth:`restart`.

    The unit name is consumed at construction time and never re-exposed
    as a parameter on the public surface. :meth:`restart` takes no
    arguments precisely so there is no path for a request body to
    influence which unit gets restarted.
    """

    def __init__(self, *, target_unit: str) -> None:
        # Pin the unit at construction. No setter, no kwarg on restart().
        self._unit = target_unit

    @property
    def target_unit(self) -> str:
        """The systemd user unit name this manager will restart."""
        return self._unit

    # ------------------------------------------------------------------
    # restart()
    # ------------------------------------------------------------------

    async def restart(self) -> RestartResult:
        """Restart the pinned unit.

        Tries the dbus path first (fast, no subprocess, gives back a
        job id the caller can log for correlation). If anything in the
        dbus path raises — failed to connect to the session bus,
        introspect blew up, ``RestartUnit`` returned an error — the
        subprocess path runs as a fallback.

        Returns a :class:`RestartResult` describing the outcome. The
        result is never an exception: any failure is recorded in the
        ``error`` field so the route can render a toast instead of
        bubbling a 500 traceback into the operator's browser.
        """
        dbus_error: str | None = None
        try:
            job_id = await self._restart_via_dbus()
        except Exception as exc:  # noqa: BLE001 - intentional: any failure → fallback
            dbus_error = f"dbus: {type(exc).__name__}: {exc}"
            _log.warning(
                "anna.web.restart.dbus_failed",
                extra={"unit": self._unit, "error": dbus_error},
            )
        else:
            return RestartResult(method="dbus", job_id=job_id, error=None)

        # Subprocess fallback.
        try:
            await self._restart_via_subprocess()
        except Exception as exc:  # noqa: BLE001
            sub_error = f"systemctl: {type(exc).__name__}: {exc}"
            combined = sub_error if dbus_error is None else f"{dbus_error}; {sub_error}"
            return RestartResult(method="subprocess", job_id=None, error=combined)

        return RestartResult(method="subprocess", job_id=None, error=None)

    async def _restart_via_dbus(self) -> str:
        """Talk to systemd over the user dbus session, return the job id."""
        # Import inside the method so a) the module loads even on
        # build hosts without a session bus, and b) tests that monkey-
        # patch ``dbus_next.aio.MessageBus`` see the patched version.
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            introspection = await bus.introspect(_SYSTEMD_BUS_NAME, _SYSTEMD_OBJECT_PATH)
            proxy = bus.get_proxy_object(_SYSTEMD_BUS_NAME, _SYSTEMD_OBJECT_PATH, introspection)
            manager = proxy.get_interface(_SYSTEMD_MANAGER_IFACE)
            # RestartUnit signature is (in s name, in s mode) → (out o job).
            # "replace" mode supersedes any pending job for the same unit.
            job_path = await manager.call_restart_unit(self._unit, "replace")
        finally:
            bus.disconnect()
        return str(job_path)

    async def _restart_via_subprocess(self) -> None:
        """Run ``systemctl --user restart <unit>`` and await its exit."""
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "restart",
            self._unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"systemctl exited {proc.returncode}: {err or '(no stderr)'}"
            )

    # ------------------------------------------------------------------
    # also_health_probe() — consumed by /healthz (subtask 11)
    # ------------------------------------------------------------------

    async def also_health_probe(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Return the ActiveState of the pinned unit + the path used.

        Subtask 11's ``/healthz`` endpoint imports this manager and calls
        ``also_health_probe`` to discover whether ``anna.service`` is
        currently ``active``. Same two-path posture as :meth:`restart`:
        dbus first, subprocess fallback.

        Returns ``{"active_state": <state>, "method": "dbus"|"subprocess"}``.
        ``<state>`` is one of ``"active"``, ``"inactive"``, ``"failed"``,
        or ``"unknown"`` (the catch-all when neither path produced a
        recognizable string).

        ``timeout`` caps each path's wait so a wedged dbus call or a
        runaway ``systemctl`` doesn't pin the /healthz handler.
        """
        try:
            state = await asyncio.wait_for(self._active_state_via_dbus(), timeout=timeout)
            return {"active_state": state, "method": "dbus"}
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "anna.web.health_probe.dbus_failed",
                extra={"unit": self._unit, "error": str(exc)},
            )

        try:
            state = await asyncio.wait_for(
                self._active_state_via_subprocess(), timeout=timeout
            )
            return {"active_state": state, "method": "subprocess"}
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "anna.web.health_probe.subprocess_failed",
                extra={"unit": self._unit, "error": str(exc)},
            )
            return {"active_state": "unknown", "method": "subprocess"}

    async def _active_state_via_dbus(self) -> str:
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            introspection = await bus.introspect(_SYSTEMD_BUS_NAME, _SYSTEMD_OBJECT_PATH)
            proxy = bus.get_proxy_object(_SYSTEMD_BUS_NAME, _SYSTEMD_OBJECT_PATH, introspection)
            manager = proxy.get_interface(_SYSTEMD_MANAGER_IFACE)
            unit_path = await manager.call_get_unit(self._unit)

            unit_introspection = await bus.introspect(_SYSTEMD_BUS_NAME, str(unit_path))
            unit_proxy = bus.get_proxy_object(
                _SYSTEMD_BUS_NAME, str(unit_path), unit_introspection
            )
            unit_iface = unit_proxy.get_interface(_SYSTEMD_UNIT_IFACE)
            state = await unit_iface.get_active_state()
        finally:
            bus.disconnect()
        return _normalize_active_state(str(state))

    async def _active_state_via_subprocess(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "is-active",
            self._unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        # is-active exits non-zero when the unit is inactive/failed, but
        # the state we want still comes back on stdout. Don't raise on a
        # non-zero exit — that's the normal "unit is not active" path.
        raw = (stdout or b"").decode("utf-8", errors="replace").strip()
        return _normalize_active_state(raw)


def _normalize_active_state(raw: str) -> str:
    """Coerce a systemd ActiveState string to the canonical four-value set.

    systemd publishes a small enum (``active``, ``reloading``,
    ``inactive``, ``failed``, ``activating``, ``deactivating``). The
    dashboard cares about three end states plus a catch-all; collapse
    the transitional states into the nearest end state so the /healthz
    consumer doesn't need to know systemd's full enum.
    """
    s = raw.strip().lower()
    if s in {"active", "reloading", "activating"}:
        return "active"
    if s in {"inactive", "deactivating"}:
        return "inactive"
    if s == "failed":
        return "failed"
    return "unknown"
