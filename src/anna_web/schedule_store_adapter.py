"""Web-dashboard wrapper around :class:`anna.runtime.schedule_store.ScheduleStore`.

Subtask 9 of the Phase 2.5 buildout. The dashboard reads and writes
``$ANNA_HOME/schedules.yaml`` through the same store the daemon uses
so the on-disk schema stays a single source of truth. The wrapper
exists for three reasons the plan calls out:

* **Clean injection point for tests.** Routes pull
  ``request.app.state.schedule_store`` so tests can override the
  adapter with one pointing at a tmp anna_home.
* **Direct in-process use, daemon picks up changes on its next 30s
  poll cycle.** No IPC, no MCP-tool round-trip; the dashboard owns
  its own ScheduleStore instance backed by the same YAML.
* **Web-only helpers without daemon-side churn.** The adapter is the
  place to add display-side conveniences (formatted destination
  strings, last-fire badges) without touching the daemon's persistence
  layer.

The daemon's :class:`ScheduleStore` exposes ``list``, ``get``,
``create``, ``update(**changes)``, and ``delete``; we delegate to
those exact methods. ``update`` takes keyword changes rather than a
replacement model, so the adapter accepts a full :class:`Schedule`
and decomposes it into the changed-field dict the underlying store
expects.

Concurrency: the daemon and the dashboard each hold their own
in-process ``Supervisor`` lock. Per-process serialization is
guaranteed inside each. Cross-process safety relies on
``os.replace`` atomicity on save and the fact that the daemon only
mutates *state* fields (``state.last_fired_at``,
``state.consecutive_failures``, ``enabled`` on auto-disable) while
the dashboard only mutates *definition* fields (``cron``, ``prompt``,
``destination``, ``timezone``, ``timeout_seconds``,
``natural_language``, ``enabled`` on operator toggle). The
non-overlapping write sets keep the race documented in the plan an
"at worst, dashboard write loses if daemon races on the *same* field"
edge case rather than a correctness hole.

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md``, "Architecture →
Schedule UI — direct ScheduleStore use" for the full design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anna.config import AnnaConfig, load_config
from anna.runtime.schedule_store import ScheduleStore, ScheduleValidationError
from anna.runtime.schedule_types import Schedule
from anna.runtime.supervisor import Supervisor


__all__ = ["ScheduleStoreAdapter", "ScheduleValidationError"]


class ScheduleStoreAdapter:
    """Thin async wrapper around the daemon's :class:`ScheduleStore`.

    Construct one per FastAPI process. The adapter owns its own
    :class:`Supervisor` instance because the daemon's supervisor lives
    in a different process; the supervisor's only role here is to
    provide the per-process asyncio lock the underlying store reaches
    for on every mutation.

    Tests can pass ``config=...`` directly to point the adapter at a
    tmp ``schedules.yaml`` without touching the operator's real file.
    The default ``config`` is whatever :func:`anna.config.load_config`
    returns at app boot, with ``scheduler.state_path`` set so the
    underlying store reads ``<anna_home>/schedules.yaml``.
    """

    def __init__(
        self,
        *,
        anna_home: Path | None = None,
        config: AnnaConfig | None = None,
    ) -> None:
        if config is None:
            config = load_config()
        if anna_home is not None:
            # Pin the on-disk surfaces to the supplied anna_home. Tests
            # pass tmp_path here; production passes ``cfg.anna_home``.
            object.__setattr__(config, "anna_home", anna_home)
            config.scheduler.state_path = str(anna_home / "schedules.yaml")
        self._config = config
        self._supervisor = Supervisor(config=config)
        self._store = ScheduleStore(config=config, supervisor=self._supervisor)
        self._loaded = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        """Lazy-load the schedules cache on first use.

        The underlying store leaves the cache empty until :meth:`load`
        runs; we defer until the first route call so app construction
        stays synchronous (FastAPI's lifespan hook is async but the
        scaffold keeps it minimal).
        """
        if self._loaded:
            return
        await self._store.load()
        self._loaded = True

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    async def list_all(self) -> list[Schedule]:
        """Return every schedule currently on disk.

        Renamed from the underlying ``list`` so the adapter surface
        reads less like Python's built-in. The list is a snapshot of
        the in-memory cache; callers must not mutate the returned
        models in place.
        """
        await self._ensure_loaded()
        return self._store.list()

    async def get(self, schedule_id: str) -> Schedule | None:
        await self._ensure_loaded()
        return self._store.get(schedule_id)

    # ------------------------------------------------------------------
    # Write surface
    # ------------------------------------------------------------------

    async def create(self, schedule: Schedule) -> Schedule:
        """Persist a new schedule via the daemon's store.

        Raises :class:`ScheduleValidationError` for duplicate id,
        invalid cron, or a reserved (admin) destination channel.
        """
        await self._ensure_loaded()
        return await self._store.create(schedule, actor_conv="web:operator")

    async def update(self, schedule_id: str, schedule: Schedule) -> Schedule:
        """Replace the named schedule with the supplied definition.

        The underlying store's ``update`` takes ``**changes`` keyword
        arguments. The dashboard always submits a full form so we
        decompose the incoming :class:`Schedule` into the same change
        dict the underlying store understands. ``id`` is immutable
        and ``state`` is owned by the daemon, so neither is forwarded.
        """
        await self._ensure_loaded()
        existing = self._store.get(schedule_id)
        if existing is None:
            raise ScheduleValidationError(
                f"Schedule '{schedule_id}' does not exist"
            )
        payload = schedule.model_dump(mode="python")
        # The daemon's update path refuses ``id`` and owns ``state``.
        # ``created_at`` is set at create time and must not move on
        # edit, so honor the existing value rather than the form's
        # (the form does not surface it anyway).
        changes: dict[str, Any] = {
            "natural_language": payload.get("natural_language"),
            "cron": payload["cron"],
            "timezone": payload.get("timezone", existing.timezone),
            "prompt": payload["prompt"],
            "destination": payload["destination"],
            "timeout_seconds": payload.get(
                "timeout_seconds", existing.timeout_seconds
            ),
            "enabled": payload.get("enabled", existing.enabled),
        }
        return await self._store.update(
            schedule_id,
            actor_conv="web:operator",
            **changes,
        )

    async def delete(self, schedule_id: str) -> None:
        """Remove the named schedule from disk.

        Raises :class:`ScheduleValidationError` if the id does not
        exist; the route layer maps that to a 404.
        """
        await self._ensure_loaded()
        await self._store.delete(schedule_id, actor_conv="web:operator")
