"""Tests for the Phase 2 §5 CLI :class:`CLIAdapter`.

Subtask 5. End-to-end over a real Unix socket bound at a ``tmp_path``
location, exercising the adapter's session lifecycle, frame dispatch,
outbound routing, and identity-alias reverse mapping. The router is not
involved here — the adapter's inbound handler list is fed by hand from
the test bodies so each case asserts only the adapter's behavior.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig, IdentityAliasEntry
from anna.transports.base import InboundEvent, OutboundMessage
from anna.transports.cli import CLIAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    identities: list[IdentityAliasEntry] | None = None,
) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.transports.cli.socket_path = str(tmp_path / "anna.sock")
    if identities:
        cfg.identities = identities
    return cfg


async def _open_client(
    socket_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(path=str(socket_path))


async def _send_line(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    writer.write((json.dumps(frame) + "\n").encode("utf-8"))
    await writer.drain()


async def _read_frame(
    reader: asyncio.StreamReader,
    *,
    timeout: float = 1.0,
) -> dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not line:
        raise EOFError("unexpected EOF reading frame")
    return json.loads(line.decode("utf-8"))


async def _wait_for(predicate, *, timeout: float = 1.0, step: float = 0.01) -> None:
    """Poll ``predicate`` until True or raise on timeout."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError(f"predicate not satisfied within {timeout}s")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_binds_socket_stop_removes_file(tmp_path: Path) -> None:
    """``start()`` binds the configured socket path; ``stop()`` cleans it up."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        sock_path = Path(cfg.transports.cli.socket_path)
        assert sock_path.exists()
        assert await adapter.health_check() is True
    finally:
        await adapter.stop()
    assert not sock_path.exists()
    assert await adapter.health_check() is False


@pytest.mark.asyncio
async def test_hello_interactive_acks_with_local_conv_key(tmp_path: Path) -> None:
    """An interactive ``hello`` registers a session and acks ``cli:local:<username>``."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {
                "type": "hello",
                "mode": "interactive",
                "username": "funtime",
                "protocol_version": 1,
            },
        )
        ack = await _read_frame(reader)
        assert ack["type"] == "ack"
        assert ack["conv_key"] == "cli:local:funtime"
        assert ack["protocol_version"] == 1

        # Session registered under the per-transport key.
        await _wait_for(lambda: "cli:local:funtime" in adapter._sessions)

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_hello_oneshot_acks_with_unique_uuid_conv_key(tmp_path: Path) -> None:
    """A oneshot ``hello`` produces a fresh ``cli:oneshot:<uuid>`` conv_key."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        sock_path = Path(cfg.transports.cli.socket_path)

        # Two oneshot connections — each should get a distinct conv_key
        # even though both report the same username.
        reader_a, writer_a = await _open_client(sock_path)
        await _send_line(
            writer_a,
            {"type": "hello", "mode": "oneshot", "username": "funtime"},
        )
        ack_a = await _read_frame(reader_a)

        reader_b, writer_b = await _open_client(sock_path)
        await _send_line(
            writer_b,
            {"type": "hello", "mode": "oneshot", "username": "funtime"},
        )
        ack_b = await _read_frame(reader_b)

        assert ack_a["type"] == "ack"
        assert ack_b["type"] == "ack"
        assert ack_a["conv_key"].startswith("cli:oneshot:")
        assert ack_b["conv_key"].startswith("cli:oneshot:")
        assert ack_a["conv_key"] != ack_b["conv_key"]

        writer_a.close()
        await writer_a.wait_closed()
        writer_b.close()
        await writer_b.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_user_message_builds_inbound_event_with_stream_subscriber(
    tmp_path: Path,
) -> None:
    """A ``user_message`` frame dispatches an InboundEvent with stream_subscriber wired."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)

    received: list[InboundEvent] = []

    async def _handler(event: InboundEvent) -> None:
        received.append(event)

    adapter.subscribe(_handler)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        await _read_frame(reader)  # consume ack

        await _send_line(
            writer,
            {"type": "user_message", "text": "morning brief please"},
        )
        await _wait_for(lambda: len(received) == 1)

        event = received[0]
        assert event.transport == "cli"
        assert event.conversation_key == "cli:local:funtime"
        assert event.sender_id == "funtime"
        assert event.text == "morning brief please"
        assert event.is_dm is True
        assert event.stream_subscriber is not None

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_stream_subscriber_writes_delta_frame(tmp_path: Path) -> None:
    """Invoking the inbound event's stream_subscriber sends a ``delta`` frame."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)

    received: list[InboundEvent] = []

    async def _handler(event: InboundEvent) -> None:
        received.append(event)

    adapter.subscribe(_handler)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        await _read_frame(reader)  # ack

        await _send_line(
            writer,
            {"type": "user_message", "text": "hello"},
        )
        await _wait_for(lambda: len(received) == 1)

        event = received[0]
        assert event.stream_subscriber is not None

        # Simulate the worker emitting two TextBlock chunks.
        await event.stream_subscriber("Today's brief")
        await event.stream_subscriber(" covers three items.")

        frame_a = await _read_frame(reader)
        frame_b = await _read_frame(reader)
        assert frame_a == {"type": "delta", "text": "Today's brief"}
        assert frame_b == {"type": "delta", "text": " covers three items."}

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_send_writes_final_text_then_end_of_turn(tmp_path: Path) -> None:
    """``send(OutboundMessage)`` writes final_text + end_of_turn in order."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        await _read_frame(reader)  # ack
        await _wait_for(lambda: "cli:local:funtime" in adapter._sessions)

        await adapter.send(
            OutboundMessage(
                conversation_key="cli:local:funtime",
                text="Final assistant reply.",
            )
        )

        frame_a = await _read_frame(reader)
        frame_b = await _read_frame(reader)
        assert frame_a == {"type": "final_text", "text": "Final assistant reply."}
        assert frame_b == {"type": "end_of_turn"}

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_send_oneshot_writes_bye_and_closes(tmp_path: Path) -> None:
    """One-shot ``send`` writes final_text + end_of_turn + bye, then closes."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "oneshot", "username": "funtime"},
        )
        ack = await _read_frame(reader)
        conv_key = ack["conv_key"]
        await _wait_for(lambda: conv_key in adapter._sessions)

        await adapter.send(
            OutboundMessage(
                conversation_key=conv_key,
                text="one-shot reply",
            )
        )

        frame_a = await _read_frame(reader)
        frame_b = await _read_frame(reader)
        frame_c = await _read_frame(reader)
        assert frame_a == {"type": "final_text", "text": "one-shot reply"}
        assert frame_b == {"type": "end_of_turn"}
        assert frame_c == {"type": "bye"}

        # Session should be unregistered after oneshot send.
        await _wait_for(lambda: conv_key not in adapter._sessions)

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_identity_alias_reverse_mapping_routes_outbound(tmp_path: Path) -> None:
    """A session for an aliased cli_username is reachable via ``user:<canonical>``."""
    cfg = _make_config(
        tmp_path,
        identities=[IdentityAliasEntry(canonical="seth", cli_username="funtime")],
    )
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        await _read_frame(reader)  # ack
        await _wait_for(lambda: "user:seth" in adapter._sessions)

        # Both the per-transport key and the post-alias key resolve to
        # the same session.
        assert adapter._sessions["cli:local:funtime"] is adapter._sessions["user:seth"]

        # The router would emit the rewritten key here.
        await adapter.send(
            OutboundMessage(
                conversation_key="user:seth",
                text="aliased reply",
            )
        )
        frame_a = await _read_frame(reader)
        frame_b = await _read_frame(reader)
        assert frame_a == {"type": "final_text", "text": "aliased reply"}
        assert frame_b == {"type": "end_of_turn"}

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_send_unknown_conv_key_drops_silently(tmp_path: Path) -> None:
    """``send`` for a conv_key with no live session logs and returns without raising."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        # No connected client, no session. Should not raise.
        await adapter.send(
            OutboundMessage(
                conversation_key="cli:local:ghost",
                text="will be dropped",
            )
        )
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_cancel_frame_logs_but_does_not_kill_session(tmp_path: Path) -> None:
    """A ``cancel`` frame is logged and the session continues to dispatch later frames."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)

    received: list[InboundEvent] = []

    async def _handler(event: InboundEvent) -> None:
        received.append(event)

    adapter.subscribe(_handler)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        await _read_frame(reader)  # ack

        await _send_line(writer, {"type": "cancel"})
        # cancel does not produce an InboundEvent.
        await _send_line(writer, {"type": "user_message", "text": "after cancel"})
        await _wait_for(lambda: len(received) == 1)
        assert received[0].text == "after cancel"

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# conversation_key_for
# ---------------------------------------------------------------------------


def test_conversation_key_for_inbound_event_returns_existing_key() -> None:
    event = InboundEvent(
        transport="cli",
        conversation_key="cli:local:funtime",
        sender_id="funtime",
        sender_display="funtime",
        text="hello",
        is_dm=True,
        is_thread=False,
    )
    assert CLIAdapter.conversation_key_for(event) == "cli:local:funtime"


def test_conversation_key_for_dict_interactive() -> None:
    assert (
        CLIAdapter.conversation_key_for({"mode": "interactive", "username": "funtime"})
        == "cli:local:funtime"
    )


def test_conversation_key_for_dict_oneshot_unique() -> None:
    a = CLIAdapter.conversation_key_for({"mode": "oneshot", "username": "funtime"})
    b = CLIAdapter.conversation_key_for({"mode": "oneshot", "username": "funtime"})
    assert a.startswith("cli:oneshot:")
    assert b.startswith("cli:oneshot:")
    assert a != b
