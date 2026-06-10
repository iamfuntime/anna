"""Optional-integration registry for the ANNA web dashboard.

Mission-control subtask 8 (see
``Inbox/2026-06-10-anna-web-mission-control-plan.md``, "Optional-
integration pattern"): a generic gating pattern for features that
touch operator resources beyond the daemon's own state. The first
registration is Obsidian/TaskNotes; the registry *shape* is the
deliverable.

Each :class:`Integration` declares the three surfaces the gate
controls:

* ``is_enabled(cfg)`` — the config gate. Everything below stays
  invisible until it returns True.
* ``nav_entries`` — nav links ``base.html`` renders via the
  ``integration_nav`` Jinja global that
  :func:`anna_web.app.create_app` populates from
  :func:`nav_entries`. Disabled integration → no nav entry at all.
* ``build_router`` — lazily-imported ``APIRouter`` that ``create_app``
  mounts via :func:`routers` only when the gate passes. Disabled
  integration → the route module never even imports and its paths 404.

Readers gate themselves through :func:`is_enabled` with the
integration's registry name (e.g. the TaskNote reader, subtask 9,
checks ``is_enabled(cfg, OBSIDIAN_TASKNOTES)``).

With the all-off defaults on :class:`anna.config.IntegrationsConfig`,
a vanilla deploy shows **no** integration UI — the dashboard is pure
daemon observability. Gates are restart-applied like every anna.yaml
setting (no hot-reload).

Fail-soft contract: a crashing ``is_enabled`` probe counts as
*disabled* — it never raises into ``create_app`` or a template. This
mirrors the crash-tolerance discipline of
:func:`anna_web.routes.healthz_routes.gather_health`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter

from anna.config import AnnaConfig

# Registry name of the Obsidian/TaskNotes integration. Readers and
# tests reference the gate by this constant rather than a stringly
# duplicate.
OBSIDIAN_TASKNOTES = "obsidian_tasknotes"


@dataclass(frozen=True)
class NavEntry:
    """One nav link rendered in ``base.html`` while its integration is enabled."""

    label: str
    href: str


@dataclass(frozen=True)
class Integration:
    """One optional integration: gate + nav entries + (lazy) router.

    ``build_router`` is a zero-arg callable (not an imported router)
    so a disabled integration never pays the import of its route
    module — and so a future integration whose routes have heavier
    dependencies stays cheap to *not* enable.
    """

    name: str
    is_enabled: Callable[[AnnaConfig], bool]
    nav_entries: tuple[NavEntry, ...] = ()
    build_router: Callable[[], APIRouter] | None = None


def _obsidian_tasknotes_enabled(cfg: AnnaConfig) -> bool:
    """TaskNote board gate: BOTH ``obsidian.enabled`` and ``tasknotes_enabled``."""
    obsidian = cfg.integrations.obsidian
    return bool(obsidian.enabled and obsidian.tasknotes_enabled)


def _build_tasks_router() -> APIRouter:
    # Lazy import: a disabled integration's route module never loads.
    from anna_web.routes import tasks_routes

    return tasks_routes.router


REGISTRY: tuple[Integration, ...] = (
    Integration(
        name=OBSIDIAN_TASKNOTES,
        is_enabled=_obsidian_tasknotes_enabled,
        nav_entries=(NavEntry(label="Tasks", href="/tasks"),),
        build_router=_build_tasks_router,
    ),
)


def _gate(integration: Integration, cfg: AnnaConfig) -> bool:
    """Fail-soft wrapper around one integration's ``is_enabled`` probe.

    A probe that raises (exotic cfg shape, future refactor drift)
    counts as disabled rather than 500ing app construction or a
    template render.
    """
    try:
        return bool(integration.is_enabled(cfg))
    except Exception:
        return False


def enabled_integrations(cfg: AnnaConfig) -> list[Integration]:
    """Every registered integration whose gate passes for ``cfg``."""
    return [i for i in REGISTRY if _gate(i, cfg)]


def nav_entries(cfg: AnnaConfig) -> list[NavEntry]:
    """Nav links for enabled integrations, in registry order.

    ``create_app`` exposes this as the ``integration_nav`` Jinja
    global; ``base.html`` renders one ``<li>`` per entry.
    """
    return [
        entry
        for integration in enabled_integrations(cfg)
        for entry in integration.nav_entries
    ]


def routers(cfg: AnnaConfig) -> list[APIRouter]:
    """Routers for enabled integrations, ready for ``include_router``."""
    return [
        integration.build_router()
        for integration in enabled_integrations(cfg)
        if integration.build_router is not None
    ]


def is_enabled(cfg: AnnaConfig, name: str) -> bool:
    """Reader-availability gate: is the named integration enabled?

    Unknown names are simply disabled, never an error — a reader
    probing a gate that was renamed or removed degrades to "no data"
    rather than crashing a view.
    """
    for integration in REGISTRY:
        if integration.name == name:
            return _gate(integration, cfg)
    return False
