"""Validate Phase 2 §5 identity-alias normalization in ``ConversationRouter``.

The aliasing is opt-in via the ``identities:`` config block (default empty,
so existing router tests see no behavior change). When configured, an
inbound event whose per-transport identifier matches an entry has its
``conversation_key`` rewritten to ``user:<canonical>`` before the worker
registry, transcript writer, or any other downstream consumer sees it.

These tests cover the six shapes called out in the plan plus one
transport-scoping cross-check.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from anna.config import AnnaConfig, IdentityAliasEntry
from anna.runtime.router import (
    ConversationRouter,
    _build_identity_index,
    _identifier_from_event,
)
from anna.runtime.supervisor import Supervisor
from anna.transports.base import InboundEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_router(tmp_path, identities: list[IdentityAliasEntry] | None = None) -> ConversationRouter:
    cfg = AnnaConfig()
    if identities is not None:
        # AnnaConfig._check_unique_canonical fires on assignment via
        # model_validator(mode="after") at construction time only; we
        # bypass re-validation here because the entries already come
        # from a constructed config in real usage.
        cfg.identities = identities
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)
    return ConversationRouter(config=cfg, supervisor=supervisor, adapters={})


def _slack_dm_event(user_id: str) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=f"slack:dm:{user_id}",
        sender_id=user_id,
        sender_display="Seth",
        text="hello",
        is_dm=True,
        is_thread=False,
    )


def _slack_channel_event(channel: str, ts: str) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=f"slack:ch:{channel}:{ts}",
        sender_id="USOMEONE",
        sender_display="Someone",
        text="reply",
        is_dm=False,
        is_thread=True,
    )


def _telegram_dm_event(chat_id: str) -> InboundEvent:
    return InboundEvent(
        transport="telegram",
        conversation_key=f"telegram:dm:{chat_id}",
        sender_id=chat_id,
        sender_display="Seth",
        text="hello",
        is_dm=True,
        is_thread=False,
    )


def _cli_local_event(username: str) -> InboundEvent:
    return InboundEvent(
        transport="cli",
        conversation_key=f"cli:local:{username}",
        sender_id=username,
        sender_display=username,
        text="hello",
        is_dm=True,
        is_thread=False,
    )


def _cli_oneshot_event(uuid_str: str, username: str) -> InboundEvent:
    return InboundEvent(
        transport="cli",
        conversation_key=f"cli:oneshot:{uuid_str}",
        sender_id=username,
        sender_display=username,
        text="ad-hoc",
        is_dm=False,
        is_thread=False,
    )


# ---------------------------------------------------------------------------
# _identifier_from_event helper
# ---------------------------------------------------------------------------


def test_identifier_from_event_slack_dm() -> None:
    assert _identifier_from_event(_slack_dm_event("USP2QLB41")) == "USP2QLB41"


def test_identifier_from_event_telegram_dm() -> None:
    assert _identifier_from_event(_telegram_dm_event("993947726")) == "993947726"


def test_identifier_from_event_cli_local() -> None:
    assert _identifier_from_event(_cli_local_event("funtime")) == "funtime"


def test_identifier_from_event_slack_channel_returns_none() -> None:
    assert _identifier_from_event(
        _slack_channel_event("C0AEY346WRL", "1716832500.000300")
    ) is None


def test_identifier_from_event_cli_oneshot_returns_none() -> None:
    """One-shot conv_keys must never be aliased per Phase 2 §5."""
    assert _identifier_from_event(
        _cli_oneshot_event("abc-123", "funtime")
    ) is None


# ---------------------------------------------------------------------------
# Six plan-mandated cases on _normalize_conv_key
# ---------------------------------------------------------------------------


def test_slack_dm_from_aliased_user_rewrites_to_canonical(tmp_path) -> None:
    router = _make_router(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")],
    )
    event = _slack_dm_event("USP2QLB41")
    assert router._normalize_conv_key(event) == "user:seth"


def test_slack_dm_from_non_aliased_user_passes_through(tmp_path) -> None:
    router = _make_router(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")],
    )
    event = _slack_dm_event("U_OTHER_USER")
    assert router._normalize_conv_key(event) == "slack:dm:U_OTHER_USER"


def test_telegram_dm_from_aliased_chat_rewrites_to_canonical(tmp_path) -> None:
    router = _make_router(
        tmp_path,
        identities=[
            IdentityAliasEntry(canonical="seth", telegram_chat_id="993947726")
        ],
    )
    event = _telegram_dm_event("993947726")
    assert router._normalize_conv_key(event) == "user:seth"


def test_cli_interactive_from_aliased_username_rewrites_to_canonical(tmp_path) -> None:
    router = _make_router(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", cli_username="funtime")],
    )
    event = _cli_local_event("funtime")
    assert router._normalize_conv_key(event) == "user:seth"


def test_cli_oneshot_is_never_aliased(tmp_path) -> None:
    """Even when the username matches an entry, the oneshot shape's
    ``cli:oneshot:<uuid>`` conv_key has no extractable identity and must
    pass through unchanged. Each ``anna ask`` invocation owns its own
    ephemeral worker."""
    router = _make_router(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", cli_username="funtime")],
    )
    event = _cli_oneshot_event("9b3d-uuid", "funtime")
    assert router._normalize_conv_key(event) == "cli:oneshot:9b3d-uuid"


def test_slack_channel_thread_passes_through(tmp_path) -> None:
    """Slack channel threads carry no operator identity in the conv_key."""
    router = _make_router(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")],
    )
    event = _slack_channel_event("C0AEY346WRL", "1716832500.000300")
    assert (
        router._normalize_conv_key(event)
        == "slack:ch:C0AEY346WRL:1716832500.000300"
    )


# ---------------------------------------------------------------------------
# Transport-scoping cross-check
# ---------------------------------------------------------------------------


def test_alias_lookup_is_transport_scoped(tmp_path) -> None:
    """An entry that only sets ``slack_user_id`` must NOT match a
    Telegram event with the same numeric identifier, even if the string
    values would compare equal. The index is bucketed by transport."""
    router = _make_router(
        tmp_path,
        identities=[
            IdentityAliasEntry(canonical="seth", slack_user_id="993947726")
        ],
    )
    event = _telegram_dm_event("993947726")
    assert router._normalize_conv_key(event) == "telegram:dm:993947726"


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


def test_build_identity_index_buckets_by_populated_transport_fields() -> None:
    entries = [
        IdentityAliasEntry(
            canonical="seth",
            slack_user_id="USP2QLB41",
            cli_username="funtime",
        ),
        IdentityAliasEntry(canonical="other", telegram_chat_id="123"),
    ]
    index = _build_identity_index(entries)
    assert {e.canonical for e in index["slack"]} == {"seth"}
    assert {e.canonical for e in index["telegram"]} == {"other"}
    assert {e.canonical for e in index["cli"]} == {"seth"}


# ---------------------------------------------------------------------------
# dispatch integration: rewritten conv_key flows to worker spawn
# ---------------------------------------------------------------------------


class _RecordingWorker:
    """Stand-in to capture which key the router spawned a worker under."""

    def __init__(self, conversation_key: str, transport: str) -> None:
        self.conversation_key = conversation_key
        self.transport = transport
        self.submitted: list[InboundEvent] = []

    async def submit(self, event: InboundEvent) -> None:
        self.submitted.append(event)

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_dispatch_uses_canonical_key_when_alias_matches(tmp_path) -> None:
    router = _make_router(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")],
    )

    spawned: list[_RecordingWorker] = []

    async def _spawn(event: InboundEvent) -> _RecordingWorker:
        # The event the router hands us should already carry the
        # rewritten conv_key.
        worker = _RecordingWorker(event.conversation_key, event.transport)
        spawned.append(worker)
        router._workers[event.conversation_key] = worker  # type: ignore[assignment]
        return worker

    router._get_or_spawn_worker = _spawn  # type: ignore[method-assign]

    await router.dispatch(_slack_dm_event("USP2QLB41"))

    assert len(spawned) == 1
    assert spawned[0].conversation_key == "user:seth"
    assert len(spawned[0].submitted) == 1
    assert spawned[0].submitted[0].conversation_key == "user:seth"
    # Original event field shouldn't leak through; the dataclass replace
    # is non-destructive on the caller's reference.
    assert spawned[0].submitted[0].transport == "slack"
