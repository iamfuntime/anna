"""Validate the router-side cadence-reminder loader closure.

Cadence-Visibility Hooks plan (Inbox/2026-06-02) subtask 7.
``ConversationRouter._build_visibility_callbacks(transport)`` returns a
:class:`VisibilityCallbacks` bundle whose ``cadence_reminder_loader`` is
populated for the two buffered transports (Slack, Telegram) and ``None``
for CLI. The loader reads ``core/CADENCE.md`` via
:func:`anna.core.identity.read_core_file` on every call so the operator
can edit the file without restarting ANNA.

Two cases per the plan:

* A Slack event's bundle carries a working loader that returns the
  contents of the test ``CADENCE.md``.
* A CLI event's bundle carries ``cadence_reminder_loader is None`` so
  the worker's prepend path short-circuits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anna.config import AnnaConfig
from anna.runtime.router import ConversationRouter
from anna.runtime.supervisor import Supervisor
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeAdapter(ChannelAdapter):
    """Minimal adapter stub that satisfies the abstract base.

    The router's ``_build_visibility_callbacks`` only ever pokes at the
    two thinking-signal methods (which the abstract base supplies as
    no-ops). The other abstract methods are required by the metaclass to
    instantiate but are never called from these tests.
    """

    name = "fake"

    async def start(self) -> None:  # pragma: no cover - never called
        return None

    async def stop(self) -> None:  # pragma: no cover - never called
        return None

    async def send(self, message: OutboundMessage) -> None:  # pragma: no cover
        return None

    def subscribe(self, handler: Any) -> None:  # pragma: no cover
        return None

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    @classmethod
    def conversation_key_for(cls, event: Any) -> str:  # pragma: no cover
        return "fake:dm:test"


def _make_router(
    tmp_path: Path,
    *,
    cadence_reminder: bool = True,
    adapters: dict[str, ChannelAdapter] | None = None,
) -> ConversationRouter:
    """Build a router rooted at ``tmp_path`` with a writable core/ dir.

    Mirrors the pattern in ``tests/test_router_identity_alias.py``: a
    bare :class:`AnnaConfig`, anna_home redirected under tmp_path,
    core_dir created so the loader has somewhere to read from. The
    ``cadence_reminder`` flag is plumbed through so the "disabled" path
    can be verified without changing the rest of the visibility config.
    """
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.runtime.visibility.cadence_reminder = cadence_reminder
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)
    return ConversationRouter(
        config=cfg,
        supervisor=supervisor,
        adapters=adapters or {},
    )


def _slack_event() -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key="slack:dm:USP2QLB41",
        sender_id="USP2QLB41",
        sender_display="Seth",
        text="hello",
        is_dm=True,
        is_thread=False,
    )


def _cli_event() -> InboundEvent:
    return InboundEvent(
        transport="cli",
        conversation_key="cli:local:funtime",
        sender_id="funtime",
        sender_display="funtime",
        text="hello",
        is_dm=True,
        is_thread=False,
    )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_slack_event_gets_reminder_prepended(tmp_path: Path) -> None:
    """For Slack the router builds a callable loader that returns the
    stripped contents of ``core/CADENCE.md``. The worker uses this to
    prepend a ``<system-reminder>`` block on every buffered event."""
    router = _make_router(
        tmp_path,
        adapters={"slack": _FakeAdapter()},
    )
    reminder_body = (
        "Speak in short sentences. Avoid filler verbs.\n"
        "Never say 'kicking off in the background'."
    )
    cadence_path = router._config.core_dir / "CADENCE.md"
    # Add surrounding whitespace so the .strip() inside the loader is
    # exercised (the worker treats an unstripped body as "no reminder"
    # via the truthy check, but the loader is the source of the stripping).
    cadence_path.write_text(f"\n\n{reminder_body}\n\n", encoding="utf-8")

    bundle = router._build_visibility_callbacks(_slack_event().transport)

    assert bundle.cadence_reminder_loader is not None
    assert callable(bundle.cadence_reminder_loader)
    assert bundle.cadence_reminder_loader() == reminder_body


def test_cli_event_does_not_get_reminder(tmp_path: Path) -> None:
    """CLI sees streaming deltas live so the reminder is unnecessary and
    the router leaves ``cadence_reminder_loader`` as ``None``. The worker's
    prepend branch short-circuits on the ``is None`` check, leaving the
    inbound text unmodified."""
    router = _make_router(
        tmp_path,
        adapters={"cli": _FakeAdapter()},
    )

    bundle = router._build_visibility_callbacks(_cli_event().transport)

    assert bundle.cadence_reminder_loader is None
