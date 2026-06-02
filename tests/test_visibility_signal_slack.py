"""Slack thinking-signal reaction hook.

Subtask 8 of the Cadence-Visibility Hooks plan. ``SlackAdapter``
overrides ``start_thinking_signal`` and ``clear_thinking_signal`` to
post and remove a Slack reaction on the inbound message, using the
``channel`` and ``ts`` stashed on ``InboundEvent.raw``.

The four cases pinned in the plan:

(a) add called with the right channel/ts/emoji on start;
(b) remove called with the same channel/ts/emoji on clear;
(c) add failure logs a warning and returns ``None``;
(d) clear tolerates a handle with missing fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anna.config import AnnaConfig
from anna.transports.base import InboundEvent, SignalHandle
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubReactionsClient:
    """Mock Slack web client recording reactions_add / reactions_remove calls.

    Mirrors the ``_StubSlackClient`` pattern from
    ``tests/test_slack_thread_followup.py`` — the SlackAdapter only
    needs the method shape ``async def reactions_<verb>(**kwargs)``.
    """

    def __init__(
        self,
        *,
        add_raises: BaseException | None = None,
        remove_raises: BaseException | None = None,
    ) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.remove_calls: list[dict[str, Any]] = []
        self._add_raises = add_raises
        self._remove_raises = remove_raises

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append(kwargs)
        if self._add_raises is not None:
            raise self._add_raises
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, Any]:
        self.remove_calls.append(kwargs)
        if self._remove_raises is not None:
            raise self._remove_raises
        return {"ok": True}


def _make_adapter(
    tmp_path: Path,
    *,
    client: _StubReactionsClient | None = None,
    slack_emoji: str | None = None,
) -> SlackAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    if slack_emoji is not None:
        cfg.runtime.visibility.slack_emoji = slack_emoji
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    adapter = SlackAdapter(config=cfg, thread_participation=tp)
    adapter._client = client if client is not None else _StubReactionsClient()
    return adapter


def _make_event(
    *,
    channel: str = "D123",
    ts: str = "1716832500.000300",
    conv_key: str = "slack:dm:U_OP",
) -> InboundEvent:
    raw = {
        "type": "message",
        "channel_type": "im",
        "channel": channel,
        "user": "U_OP",
        "ts": ts,
        "text": "hi anna",
    }
    return InboundEvent(
        transport="slack",
        conversation_key=conv_key,
        sender_id="U_OP",
        sender_display="U_OP",
        text="hi anna",
        is_dm=True,
        is_thread=False,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# (a) add called with the right channel/ts/emoji on start
# ---------------------------------------------------------------------------


async def test_start_posts_reaction_with_channel_ts_and_emoji(tmp_path: Path) -> None:
    client = _StubReactionsClient()
    adapter = _make_adapter(tmp_path, client=client)
    event = _make_event(channel="D123", ts="1716832500.000300")

    handle = await adapter.start_thinking_signal(event)

    assert handle is not None
    assert handle.transport == "slack"
    assert handle.conv_key == event.conversation_key
    assert handle.slack_channel == "D123"
    assert handle.slack_ts == "1716832500.000300"
    assert handle.slack_emoji == "thinking_face"

    assert len(client.add_calls) == 1
    assert client.add_calls[0] == {
        "channel": "D123",
        "timestamp": "1716832500.000300",
        "name": "thinking_face",
    }


async def test_start_honors_configured_emoji(tmp_path: Path) -> None:
    client = _StubReactionsClient()
    adapter = _make_adapter(tmp_path, client=client, slack_emoji="hourglass_flowing_sand")
    event = _make_event()

    handle = await adapter.start_thinking_signal(event)

    assert handle is not None
    assert handle.slack_emoji == "hourglass_flowing_sand"
    assert client.add_calls[0]["name"] == "hourglass_flowing_sand"


async def test_start_returns_none_when_channel_or_ts_missing(tmp_path: Path) -> None:
    client = _StubReactionsClient()
    adapter = _make_adapter(tmp_path, client=client)
    event = InboundEvent(
        transport="slack",
        conversation_key="slack:dm:U_OP",
        sender_id="U_OP",
        sender_display="U_OP",
        text="hi",
        is_dm=True,
        is_thread=False,
        raw={"type": "message", "user": "U_OP"},  # no channel / ts
    )

    handle = await adapter.start_thinking_signal(event)

    assert handle is None
    assert client.add_calls == []


# ---------------------------------------------------------------------------
# (b) remove called with the same channel/ts/emoji on clear
# ---------------------------------------------------------------------------


async def test_clear_removes_reaction_with_same_args(tmp_path: Path) -> None:
    client = _StubReactionsClient()
    adapter = _make_adapter(tmp_path, client=client)
    event = _make_event(channel="C0AEY346WRL", ts="1716832500.000300")

    handle = await adapter.start_thinking_signal(event)
    assert handle is not None
    await adapter.clear_thinking_signal(handle)

    assert len(client.remove_calls) == 1
    assert client.remove_calls[0] == {
        "channel": "C0AEY346WRL",
        "timestamp": "1716832500.000300",
        "name": "thinking_face",
    }


# ---------------------------------------------------------------------------
# (c) add failure logs warning and returns None
# ---------------------------------------------------------------------------


async def test_start_returns_none_when_reactions_add_raises(tmp_path: Path) -> None:
    client = _StubReactionsClient(add_raises=RuntimeError("slack 429"))
    adapter = _make_adapter(tmp_path, client=client)
    event = _make_event()

    handle = await adapter.start_thinking_signal(event)

    # API call attempted, but the failure is swallowed and the worker
    # gets a None handle so it can skip the clear path.
    assert handle is None
    assert len(client.add_calls) == 1


# ---------------------------------------------------------------------------
# (d) clear is safe when handle has missing fields
# ---------------------------------------------------------------------------


async def test_clear_is_noop_when_handle_fields_missing(tmp_path: Path) -> None:
    client = _StubReactionsClient()
    adapter = _make_adapter(tmp_path, client=client)

    # Handle with no Slack-specific fields populated — clear must
    # return without touching the client.
    bare_handle = SignalHandle(transport="slack", conv_key="slack:dm:U_OP")
    await adapter.clear_thinking_signal(bare_handle)

    assert client.remove_calls == []


async def test_clear_swallows_reactions_remove_errors(tmp_path: Path) -> None:
    """A ``reaction_not_found`` or network drop on remove must not
    propagate; the worker's ``finally`` block must complete cleanly."""

    client = _StubReactionsClient(remove_raises=RuntimeError("reaction_not_found"))
    adapter = _make_adapter(tmp_path, client=client)
    event = _make_event()

    handle = await adapter.start_thinking_signal(event)
    assert handle is not None

    # Must NOT raise.
    await adapter.clear_thinking_signal(handle)

    assert len(client.remove_calls) == 1
