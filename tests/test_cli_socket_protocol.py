"""Tests for the Phase 2 §5 CLI Unix-domain-socket protocol.

Subtask 4. Exercises :class:`anna.transports.cli_server.UnixSocketServer`
end-to-end over a real Unix socket bound at a ``tmp_path`` location:

* Bind sets owner-only mode 0600.
* The first frame on a connection drives ``on_session``; subsequent
  frames drive ``on_inbound`` in order.
* Malformed lines are dropped without crashing the server; following
  valid lines still dispatch.
* Closing the client socket drains the read loop and sets
  ``session.closed``.

The adapter wrapping this server (subtask 5) is mocked: ``on_session``
and ``on_inbound`` here are plain callbacks recording into lists.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from anna.transports.cli_server import ClientSession, UnixSocketServer


def _socket_path(tmp_path: Path) -> Path:
    # Keep the path short. Linux's sun_path is 108 bytes; pytest tmp_paths
    # under /tmp are well within that, but it's worth being explicit.
    return tmp_path / "anna.sock"


async def _start_server(
    socket_path: Path,
    *,
    sessions: list[ClientSession],
    inbounds: list[tuple[ClientSession, dict[str, Any]]],
) -> UnixSocketServer:
    async def on_session(session: ClientSession) -> None:
        sessions.append(session)

    async def on_inbound(session: ClientSession, frame: dict[str, Any]) -> None:
        inbounds.append((session, frame))

    server = UnixSocketServer(
        socket_path=socket_path,
        on_session=on_session,
        on_inbound=on_inbound,
    )
    await server.start()
    return server


async def _open_client(
    socket_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(path=str(socket_path))


async def _send_line(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    writer.write((json.dumps(frame) + "\n").encode("utf-8"))
    await writer.drain()


@pytest.mark.asyncio
async def test_bind_sets_socket_mode_to_0o600(tmp_path: Path) -> None:
    """After ``start()``, the socket file must be owner-only (0600)."""
    sock = _socket_path(tmp_path)
    sessions: list[ClientSession] = []
    inbounds: list[tuple[ClientSession, dict[str, Any]]] = []
    server = await _start_server(sock, sessions=sessions, inbounds=inbounds)
    try:
        assert sock.exists()
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        assert server.is_serving is True
    finally:
        await server.stop()
    # Socket file should be cleaned up on stop.
    assert not sock.exists()


@pytest.mark.asyncio
async def test_hello_and_user_message_dispatch(tmp_path: Path) -> None:
    """``hello`` fires on_session once; ``user_message`` fires on_inbound once."""
    sock = _socket_path(tmp_path)
    sessions: list[ClientSession] = []
    inbounds: list[tuple[ClientSession, dict[str, Any]]] = []
    server = await _start_server(sock, sessions=sessions, inbounds=inbounds)
    try:
        reader, writer = await _open_client(sock)
        await _send_line(
            writer,
            {
                "type": "hello",
                "mode": "interactive",
                "username": "funtime",
                "protocol_version": 1,
            },
        )
        # Wait for the server to consume the hello and call on_session.
        for _ in range(50):
            if sessions:
                break
            await asyncio.sleep(0.01)
        assert len(sessions) == 1
        session = sessions[0]
        assert session.username == "funtime"
        assert session.mode == "interactive"
        # Placeholder conv_key shape per the module docstring.
        assert session.conv_key == "cli:local:funtime"

        msg = {"type": "user_message", "text": "what's in my morning brief?"}
        await _send_line(writer, msg)
        for _ in range(50):
            if inbounds:
                break
            await asyncio.sleep(0.01)
        assert len(inbounds) == 1
        got_session, got_frame = inbounds[0]
        assert got_session is session
        assert got_frame == msg

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_multiple_frames_dispatched_in_order(tmp_path: Path) -> None:
    """Three ``user_message`` frames in one connection arrive in order."""
    sock = _socket_path(tmp_path)
    sessions: list[ClientSession] = []
    inbounds: list[tuple[ClientSession, dict[str, Any]]] = []
    server = await _start_server(sock, sessions=sessions, inbounds=inbounds)
    try:
        reader, writer = await _open_client(sock)
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        messages = [
            {"type": "user_message", "text": "one"},
            {"type": "user_message", "text": "two"},
            {"type": "user_message", "text": "three"},
        ]
        for m in messages:
            await _send_line(writer, m)

        for _ in range(100):
            if len(inbounds) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(inbounds) == 3
        assert [frame["text"] for _, frame in inbounds] == ["one", "two", "three"]

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_malformed_line_does_not_crash_server(tmp_path: Path) -> None:
    """A non-JSON line is dropped; the next valid line still dispatches."""
    sock = _socket_path(tmp_path)
    sessions: list[ClientSession] = []
    inbounds: list[tuple[ClientSession, dict[str, Any]]] = []
    server = await _start_server(sock, sessions=sessions, inbounds=inbounds)
    try:
        reader, writer = await _open_client(sock)
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        for _ in range(50):
            if sessions:
                break
            await asyncio.sleep(0.01)
        assert len(sessions) == 1

        # Garbage line — not JSON.
        writer.write(b"not json at all\n")
        await writer.drain()
        # Unknown type — recognized JSON, dropped.
        await _send_line(writer, {"type": "no_such_frame_type", "text": "ignored"})
        # Valid follow-up — must still dispatch.
        await _send_line(writer, {"type": "user_message", "text": "after garbage"})

        for _ in range(100):
            if inbounds:
                break
            await asyncio.sleep(0.01)
        assert len(inbounds) == 1
        assert inbounds[0][1]["text"] == "after garbage"

        # Server still serving (not crashed).
        assert server.is_serving is True

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_client_close_terminates_read_loop_and_sets_closed(tmp_path: Path) -> None:
    """When the client closes the socket, ``session.closed`` is set."""
    sock = _socket_path(tmp_path)
    sessions: list[ClientSession] = []
    inbounds: list[tuple[ClientSession, dict[str, Any]]] = []
    server = await _start_server(sock, sessions=sessions, inbounds=inbounds)
    try:
        reader, writer = await _open_client(sock)
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        for _ in range(50):
            if sessions:
                break
            await asyncio.sleep(0.01)
        assert len(sessions) == 1
        session = sessions[0]
        assert not session.closed.is_set()

        writer.close()
        await writer.wait_closed()

        # Wait for the server side to observe EOF and finalize the session.
        try:
            await asyncio.wait_for(session.closed.wait(), timeout=1.0)
        except TimeoutError:
            pytest.fail("session.closed was not set after client disconnect")
        assert session.closed.is_set()

        # No further inbound frames should arrive — the loop terminated
        # cleanly on EOF rather than crashing or looping on a stale fd.
        assert inbounds == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_frame_writes_ndjson_with_lock(tmp_path: Path) -> None:
    """``send_frame`` writes one JSON-per-line and the lock serializes writes.

    Round-trip: open a client, send hello, the test then calls
    ``UnixSocketServer.send_frame`` from the server side via the session
    that ``on_session`` captured. The client reader receives the bytes
    line-by-line.
    """
    sock = _socket_path(tmp_path)
    sessions: list[ClientSession] = []
    inbounds: list[tuple[ClientSession, dict[str, Any]]] = []
    server = await _start_server(sock, sessions=sessions, inbounds=inbounds)
    try:
        reader, writer = await _open_client(sock)
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        for _ in range(50):
            if sessions:
                break
            await asyncio.sleep(0.01)
        session = sessions[0]

        # Fire two concurrent send_frame calls. The lock must serialize
        # them so the bytes don't interleave; both frames decode cleanly.
        f1 = {"type": "delta", "text": "hello, "}
        f2 = {"type": "delta", "text": "world\nwith embedded newline"}
        await asyncio.gather(
            UnixSocketServer.send_frame(session.writer, session.write_lock, f1),
            UnixSocketServer.send_frame(session.writer, session.write_lock, f2),
        )

        # Two lines should now be readable, one JSON per line.
        line_a = await asyncio.wait_for(reader.readline(), timeout=1.0)
        line_b = await asyncio.wait_for(reader.readline(), timeout=1.0)
        decoded = {json.loads(line_a.decode())["text"], json.loads(line_b.decode())["text"]}
        assert decoded == {"hello, ", "world\nwith embedded newline"}
        # Embedded newline must have been escaped on the wire — otherwise
        # readline() would have split it into a third line.
        assert b"\n" not in line_b[:-1], (
            "embedded newline leaked through json encoding"
        )

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
