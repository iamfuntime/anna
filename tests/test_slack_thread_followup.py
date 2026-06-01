"""Validate the Slack channel-thread follow-up filter and the
persistent participation set that drives it.

Two surfaces under test:

* :class:`anna.transports.slack_thread_state.ThreadParticipation` — the
  JSONL-backed set ANNA writes to when she replies in a channel thread.
* :meth:`anna.transports.slack.SlackAdapter._handle_message_event` — the
  filter the bolt ``message`` listener delegates to.

Plus the send-side hook: a channel-thread outbound marks
participation, a DM outbound does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.transports.base import InboundEvent, OutboundMessage
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation


# ---------------------------------------------------------------------------
# ThreadParticipation
# ---------------------------------------------------------------------------


async def test_thread_participation_load_reads_existing_entries(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "slack_thread_participation.jsonl"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "channel_id": "C111",
                "thread_ts": "1000.000001",
                "first_post_ts": "2026-06-01T00:00:00+00:00",
            }
        )
        + "\n"
        + json.dumps(
            {
                "channel_id": "C222",
                "thread_ts": "2000.000002",
                "first_post_ts": "2026-06-01T00:00:01+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    tp = ThreadParticipation(state_path=state_path)
    await tp.load()

    assert tp.has(channel_id="C111", thread_ts="1000.000001") is True
    assert tp.has(channel_id="C222", thread_ts="2000.000002") is True
    assert tp.has(channel_id="C333", thread_ts="3000.000003") is False


async def test_thread_participation_mark_writes_to_memory_and_disk(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)

    assert tp.has(channel_id="C111", thread_ts="1000.000001") is False

    await tp.mark(channel_id="C111", thread_ts="1000.000001")

    # In-memory.
    assert tp.has(channel_id="C111", thread_ts="1000.000001") is True

    # On-disk.
    assert state_path.exists()
    lines = state_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["channel_id"] == "C111"
    assert record["thread_ts"] == "1000.000001"
    assert "first_post_ts" in record


async def test_thread_participation_mark_is_idempotent(tmp_path: Path) -> None:
    """Re-marking the same key does not double-append."""
    state_path = tmp_path / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)

    await tp.mark(channel_id="C111", thread_ts="1000.000001")
    await tp.mark(channel_id="C111", thread_ts="1000.000001")

    lines = state_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


async def test_thread_participation_survives_reload(tmp_path: Path) -> None:
    """Mark, drop the in-memory state, reload — value still present."""
    state_path = tmp_path / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    await tp.mark(channel_id="C111", thread_ts="1000.000001")
    await tp.mark(channel_id="C222", thread_ts="2000.000002")

    tp_reloaded = ThreadParticipation(state_path=state_path)
    assert tp_reloaded.has(channel_id="C111", thread_ts="1000.000001") is False
    await tp_reloaded.load()

    assert tp_reloaded.has(channel_id="C111", thread_ts="1000.000001") is True
    assert tp_reloaded.has(channel_id="C222", thread_ts="2000.000002") is True


async def test_thread_participation_load_skips_corrupted_lines(tmp_path: Path) -> None:
    """A truncated tail line (process killed mid-write) must not poison loading."""
    state_path = tmp_path / "state" / "slack_thread_participation.jsonl"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "channel_id": "C111",
                "thread_ts": "1000.000001",
                "first_post_ts": "2026-06-01T00:00:00+00:00",
            }
        )
        + "\n"
        + "\n"  # blank line — fine
        + '{"channel_id": "C222", "thread_ts": "2000',  # truncated
        encoding="utf-8",
    )

    tp = ThreadParticipation(state_path=state_path)
    await tp.load()

    # Good entry loaded.
    assert tp.has(channel_id="C111", thread_ts="1000.000001") is True
    # Bad entry skipped.
    assert tp.has(channel_id="C222", thread_ts="2000") is False


async def test_thread_participation_load_missing_file_is_fine(tmp_path: Path) -> None:
    tp = ThreadParticipation(state_path=tmp_path / "nowhere.jsonl")
    await tp.load()
    assert tp.has(channel_id="C111", thread_ts="1000.000001") is False


async def test_thread_participation_skips_records_missing_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "slack_thread_participation.jsonl"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"channel_id": "C111"}) + "\n"  # missing thread_ts
        + json.dumps({"thread_ts": "1000.0"}) + "\n"  # missing channel_id
        + json.dumps(
            {
                "channel_id": "C222",
                "thread_ts": "2000.000002",
                "first_post_ts": "2026-06-01T00:00:00+00:00",
            }
        ) + "\n",
        encoding="utf-8",
    )

    tp = ThreadParticipation(state_path=state_path)
    await tp.load()

    assert tp.has(channel_id="C222", thread_ts="2000.000002") is True


# ---------------------------------------------------------------------------
# SlackAdapter._handle_message_event filter
# ---------------------------------------------------------------------------


def _make_adapter(tmp_path: Path, *, marked: list[tuple[str, str]] | None = None) -> SlackAdapter:
    """Construct a SlackAdapter with a fresh ThreadParticipation.

    ``marked`` pre-populates the participation set without touching disk
    (we synthesize the in-memory state directly so tests stay fast).
    """
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    for channel_id, thread_ts in marked or []:
        tp._set.add((channel_id, thread_ts))  # type: ignore[attr-defined]
    return SlackAdapter(config=cfg, thread_participation=tp)


class _DispatchRecorder:
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []

    async def __call__(self, event: InboundEvent) -> None:
        self.events.append(event)


async def test_on_message_dispatches_dms(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "user": "U_OP",
        "ts": "1716832500.000300",
        "text": "hi anna",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert len(recorder.events) == 1
    assert recorder.events[0].is_dm is True


async def test_on_message_dispatches_dms_even_if_thread_unknown(tmp_path: Path) -> None:
    """A threaded DM should still dispatch — DMs are exempt from the
    thread-participation filter."""
    adapter = _make_adapter(tmp_path)
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "thread_ts": "1716832000.000200",  # DM thread we've never seen
        "user": "U_OP",
        "ts": "1716832500.000300",
        "text": "hi",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert len(recorder.events) == 1


async def test_on_message_drops_top_level_channel_messages(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "user": "U_OP",
        "ts": "1716832500.000300",
        "text": "random chatter",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert recorder.events == []


async def test_on_message_drops_thread_messages_in_unparticipated_threads(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)  # nothing marked
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "1716832500.000300",
        "user": "U_OP",
        "ts": "1716832600.000600",
        "text": "follow-up",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert recorder.events == []


async def test_on_message_dispatches_thread_messages_in_participated_threads(
    tmp_path: Path,
) -> None:
    adapter = _make_adapter(
        tmp_path,
        marked=[("C0AEY346WRL", "1716832500.000300")],
    )
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "1716832500.000300",
        "user": "U_OP",
        "ts": "1716832600.000600",
        "text": "follow-up",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert len(recorder.events) == 1
    assert recorder.events[0].is_thread is True
    assert recorder.events[0].conversation_key == "slack:ch:C0AEY346WRL:1716832500.000300"


async def test_on_message_ignores_bot_echoes(tmp_path: Path) -> None:
    adapter = _make_adapter(
        tmp_path,
        marked=[("C0AEY346WRL", "1716832500.000300")],
    )
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "1716832500.000300",
        "user": "U_BOT",
        "bot_id": "B123",
        "ts": "1716832600.000600",
        "text": "anna's own reply",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert recorder.events == []


async def test_on_message_ignores_edit_subtypes(tmp_path: Path) -> None:
    adapter = _make_adapter(
        tmp_path,
        marked=[("C0AEY346WRL", "1716832500.000300")],
    )
    recorder = _DispatchRecorder()
    adapter.subscribe(recorder)

    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel_type": "channel",
        "channel": "C0AEY346WRL",
        "thread_ts": "1716832500.000300",
        "user": "U_OP",
        "ts": "1716832600.000600",
        "text": "edited",
    }
    await adapter._handle_message_event(event, body={"event": event})
    assert recorder.events == []


# ---------------------------------------------------------------------------
# SlackAdapter.send — marks participation for channel threads
# ---------------------------------------------------------------------------


class _StubSlackClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "ts": "1716832700.000700"}


async def test_send_channel_thread_marks_participation(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stub = _StubSlackClient()
    adapter._client = stub

    conv_key = "slack:ch:C0AEY346WRL:1716832500.000300"
    await adapter.send(OutboundMessage(conversation_key=conv_key, text="hi"))

    assert len(stub.calls) == 1
    assert stub.calls[0]["channel"] == "C0AEY346WRL"
    assert stub.calls[0]["thread_ts"] == "1716832500.000300"

    # Participation was marked.
    assert adapter._thread_participation.has(
        channel_id="C0AEY346WRL", thread_ts="1716832500.000300"
    )
    # And persisted.
    state_path = adapter._thread_participation.state_path
    assert state_path.exists()
    lines = state_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


async def test_send_dm_does_not_mark_participation(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stub = _StubSlackClient()
    adapter._client = stub

    conv_key = "slack:dm:U0ABCD123"
    await adapter.send(OutboundMessage(conversation_key=conv_key, text="hi"))

    assert len(stub.calls) == 1
    # DM sends never set thread_ts.
    assert "thread_ts" not in stub.calls[0]

    # No participation should be tracked for DMs.
    state_path = adapter._thread_participation.state_path
    assert not state_path.exists()


async def test_send_failure_does_not_mark_participation(tmp_path: Path) -> None:
    """If chat_postMessage raises, we should not record participation —
    the send didn't actually happen."""
    adapter = _make_adapter(tmp_path)

    class _FailingClient:
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("slack is down")

    adapter._client = _FailingClient()

    conv_key = "slack:ch:C0AEY346WRL:1716832500.000300"
    with pytest.raises(RuntimeError):
        await adapter.send(OutboundMessage(conversation_key=conv_key, text="hi"))

    assert not adapter._thread_participation.has(
        channel_id="C0AEY346WRL", thread_ts="1716832500.000300"
    )
