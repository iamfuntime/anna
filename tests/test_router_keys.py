"""Validate conversation_key derivation across both transports."""

from __future__ import annotations

import pytest

from anna.transports.slack import SlackAdapter
from anna.transports.telegram import TelegramAdapter


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def test_slack_dm_key() -> None:
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D012345",
        "user": "U0ABCD123",
        "ts": "1716832500.000300",
        "text": "hello",
    }
    assert SlackAdapter.conversation_key_for(event) == "slack:dm:U0ABCD123"


def test_slack_channel_thread_reply_key() -> None:
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "1716832500.000300",
        "user": "U0ABCD123",
        "ts": "1716832600.000600",
        "text": "reply",
    }
    expected = "slack:ch:C0AEY346WRL:1716832500.000300"
    assert SlackAdapter.conversation_key_for(event) == expected


def test_slack_app_mention_top_level_key() -> None:
    # A top-level @-mention keys WITHOUT the ":oneshot" suffix so it shares the
    # key of the thread ANNA's reply spawns (see
    # test_slack_app_mention_and_thread_reply_share_key).
    event = {
        "type": "app_mention",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "user": "U0ABCD123",
        "ts": "1716832700.000700",
        "text": "<@U0BOT> hi",
    }
    expected = "slack:ch:C0AEY346WRL:1716832700.000700"
    assert SlackAdapter.conversation_key_for(event) == expected


def test_slack_app_mention_and_thread_reply_share_key() -> None:
    # Regression guard: a top-level @-mention (turn 1) and the operator's
    # follow-up thread reply (turn 2) must derive the SAME conversation_key so
    # turn 2 reuses turn 1's worker/session instead of spawning a cold one.
    # ANNA's reply opens a thread whose thread_ts == the mention's event_ts.
    mention = {
        "type": "app_mention",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "user": "U0ABCD123",
        "ts": "T1",
        "text": "<@U0BOT> kick off a task",
    }
    thread_reply = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "T1",
        "user": "U0ABCD123",
        "ts": "T2",
        "text": "and one more thing",
    }
    expected = "slack:ch:C0AEY346WRL:T1"
    assert SlackAdapter.conversation_key_for(mention) == expected
    assert SlackAdapter.conversation_key_for(thread_reply) == expected
    assert SlackAdapter.conversation_key_for(mention) == SlackAdapter.conversation_key_for(
        thread_reply
    )


def test_slack_app_mention_in_thread_uses_thread_key() -> None:
    event = {
        "type": "app_mention",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "1716832500.000300",
        "user": "U0ABCD123",
        "ts": "1716832700.000700",
        "text": "<@U0BOT> follow-up",
    }
    expected = "slack:ch:C0AEY346WRL:1716832500.000300"
    assert SlackAdapter.conversation_key_for(event) == expected


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def test_telegram_dm_key() -> None:
    event = {"chat_id": 993947726, "chat_type": "private", "topic_id": None}
    assert TelegramAdapter.conversation_key_for(event) == "telegram:dm:993947726"


def test_telegram_group_key() -> None:
    event = {"chat_id": -100123456, "chat_type": "supergroup", "topic_id": None}
    assert TelegramAdapter.conversation_key_for(event) == "telegram:gr:-100123456"


def test_telegram_group_topic_key() -> None:
    event = {"chat_id": -100123456, "chat_type": "supergroup", "topic_id": 42}
    assert TelegramAdapter.conversation_key_for(event) == "telegram:gr:-100123456:42"
