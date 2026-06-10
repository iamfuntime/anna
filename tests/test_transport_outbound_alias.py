"""Outbound identity-alias reverse mapping on the Slack/Telegram adapters.

When the ``identities:`` block is enabled, :class:`ConversationRouter`
rewrites inbound conv_keys to ``user:<canonical>``. The worker's outbound
reply then carries that key, so each adapter must resolve it back to a
real destination — the configured ``slack_user_id`` DM on Slack, the
configured ``telegram_chat_id`` on Telegram — mirroring the reverse-map
pattern the CLI adapter already had. Regression for the 2026-06-02
incident where ``_channel_and_thread_for("user:seth")`` raised
``ValueError`` and crashed the worker on all aliased channels.

The Slack client / Telegram bot are stubbed; no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig, IdentityAliasEntry
from anna.transports.base import OutboundMessage
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation
from anna.transports.telegram import TelegramAdapter


# ---------------------------------------------------------------------------
# Fakes (same shapes as test_voice_outbound.py)
# ---------------------------------------------------------------------------


class _StubSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.post_calls.append(kwargs)
        return {"ok": True, "ts": "1716832700.000700"}


class _StubTelegramBot:
    def __init__(self) -> None:
        self.message_calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.message_calls.append(kwargs)


class _FakeApplication:
    def __init__(self, bot: _StubTelegramBot) -> None:
        self.bot = bot
        self.updater = None


# ---------------------------------------------------------------------------
# Adapter builders
# ---------------------------------------------------------------------------


def _slack_adapter(
    tmp_path: Path, identities: list[IdentityAliasEntry]
) -> tuple[SlackAdapter, _StubSlackClient]:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.identities = identities
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    adapter = SlackAdapter(config=cfg, thread_participation=tp)
    client = _StubSlackClient()
    adapter._client = client  # type: ignore[attr-defined]
    return adapter, client


def _telegram_adapter(
    tmp_path: Path, identities: list[IdentityAliasEntry]
) -> tuple[TelegramAdapter, _StubTelegramBot]:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.identities = identities
    adapter = TelegramAdapter(config=cfg)
    bot = _StubTelegramBot()
    adapter._application = _FakeApplication(bot)  # type: ignore[attr-defined]
    return adapter, bot


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


async def test_slack_user_canonical_key_resolves_to_dm(tmp_path: Path) -> None:
    """An outbound ``user:seth`` key resolves to the configured slack_user_id
    DM (no thread_ts) instead of raising ValueError."""
    adapter, client = _slack_adapter(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")],
    )

    await adapter.send(OutboundMessage(conversation_key="user:seth", text="hi"))

    assert len(client.post_calls) == 1
    assert client.post_calls[0]["channel"] == "USP2QLB41"
    # DM destination: no thread_ts on the post.
    assert "thread_ts" not in client.post_calls[0]


async def test_slack_unknown_canonical_still_raises(tmp_path: Path) -> None:
    """A canonical absent from the identities config keeps the existing
    clear ValueError (no silent misroute)."""
    adapter, _client = _slack_adapter(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")],
    )

    with pytest.raises(ValueError, match="unrecognized slack conv_key: user:bob"):
        adapter._channel_and_thread_for("user:bob")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def test_telegram_user_canonical_key_resolves_to_chat(tmp_path: Path) -> None:
    """An outbound ``user:seth`` key resolves to the configured
    telegram_chat_id (no topic) instead of raising ValueError."""
    adapter, bot = _telegram_adapter(
        tmp_path,
        identities=[
            IdentityAliasEntry(canonical="seth", telegram_chat_id="993947726")
        ],
    )

    await adapter.send(OutboundMessage(conversation_key="user:seth", text="hi"))

    assert len(bot.message_calls) == 1
    assert bot.message_calls[0]["chat_id"] == 993947726
    # DM destination: no topic thread on the message.
    assert "message_thread_id" not in bot.message_calls[0]


async def test_telegram_unknown_canonical_still_raises(tmp_path: Path) -> None:
    """A canonical absent from the identities config keeps the existing
    clear ValueError (no silent misroute)."""
    adapter, _bot = _telegram_adapter(
        tmp_path,
        identities=[
            IdentityAliasEntry(canonical="seth", telegram_chat_id="993947726")
        ],
    )

    with pytest.raises(ValueError, match="unrecognized telegram conv_key: user:bob"):
        adapter._chat_and_topic_for("user:bob")
