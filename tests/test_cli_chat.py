"""Tests for the Phase 2 §5 CLI chat client (``anna chat``).

Subtask 9. The interactive :mod:`prompt_toolkit` loop and Ctrl-C bindings
are excluded from CI per the plan ("the prompt_toolkit interactive loop is
excluded from CI and validated in the manual acceptance test"). These
tests focus on the framing / protocol layer that lives underneath the
TUI:

1. Frame-encoding helpers round-trip the ``hello`` and ``user_message``
   shapes through :func:`anna.cli.chat._encode_frame`.
2. The :func:`anna.cli.chat._render_inbound` loop handles ``delta``,
   ``end_of_turn``, ``error``, and ``bye`` correctly when fed a
   hand-built :class:`asyncio.StreamReader` with NDJSON bytes.
3. The handshake helper raises :class:`anna.cli.chat._HandshakeError`
   when the daemon's ``ack`` carries a mismatched ``protocol_version``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from anna.cli import chat as chat_module
from anna.cli.chat import (
    _PROTOCOL_VERSION,
    _build_hello_frame,
    _build_user_message_frame,
    _encode_frame,
    _HandshakeError,
    _open_and_handshake,
    _render_inbound,
)


# ---------------------------------------------------------------------------
# 1. Frame encoding round-trip
# ---------------------------------------------------------------------------


def test_frame_encoding_round_trips_hello_and_user_message() -> None:
    """``hello`` and ``user_message`` frames serialize to NDJSON and decode back.

    Verifies the wire shape matches the spec in
    ``Architecture → Wire-format (socket framing)``: one JSON object per
    line, terminating with ``\\n``, UTF-8 encoded.
    """
    hello = _build_hello_frame(username="funtime")
    assert hello == {
        "type": "hello",
        "mode": "interactive",
        "username": "funtime",
        "protocol_version": _PROTOCOL_VERSION,
    }

    user_msg = _build_user_message_frame("what's in my morning brief?")
    assert user_msg == {
        "type": "user_message",
        "text": "what's in my morning brief?",
    }

    hello_wire = _encode_frame(hello)
    user_wire = _encode_frame(user_msg)

    # Single trailing newline; no embedded raw newlines (json.dumps would
    # escape them in string values).
    assert hello_wire.endswith(b"\n")
    assert user_wire.endswith(b"\n")
    assert hello_wire.count(b"\n") == 1
    assert user_wire.count(b"\n") == 1

    decoded_hello = json.loads(hello_wire.decode("utf-8"))
    decoded_user = json.loads(user_wire.decode("utf-8"))
    assert decoded_hello == hello
    assert decoded_user == user_msg

    # Embedded newline inside a user message must survive the wire as
    # an escaped \n, not split the frame.
    multiline = _build_user_message_frame("line one\nline two")
    multiline_wire = _encode_frame(multiline)
    assert multiline_wire.count(b"\n") == 1, (
        "embedded newline leaked through json encoding"
    )
    assert json.loads(multiline_wire.decode("utf-8"))["text"] == "line one\nline two"


# ---------------------------------------------------------------------------
# 2. Render loop frame handling
# ---------------------------------------------------------------------------


def _make_reader_with_frames(frames: list[dict]) -> asyncio.StreamReader:
    """Build an :class:`asyncio.StreamReader` pre-populated with NDJSON frames.

    Used to drive :func:`_render_inbound` without standing up a real socket.
    The reader's EOF marker is set so the loop sees a clean end of stream
    if no ``bye`` frame is included.
    """
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data((json.dumps(frame) + "\n").encode("utf-8"))
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_render_inbound_handles_delta_end_of_turn_error_and_bye(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The render loop dispatches each frame type and exits on ``bye``.

    ``run_in_terminal`` is monkey-patched to a passthrough so we don't
    need a real prompt_toolkit application running. Each frame-type
    handler is also patched to record what it was called with; we
    assert the recorded calls match the input frame sequence.
    """
    recorded: list[tuple[str, str]] = []

    async def _passthrough(callback, *args, **kwargs):
        # run_in_terminal's contract is to invoke the callable; we just
        # call it directly and return its result.
        return callback()

    monkeypatch.setattr(chat_module, "run_in_terminal", _passthrough)
    monkeypatch.setattr(
        chat_module,
        "_print_delta",
        lambda text: recorded.append(("delta", text)),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_error",
        lambda message: recorded.append(("error", message)),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_end_of_turn",
        lambda: recorded.append(("end_of_turn", "")),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_tool_indicator",
        lambda name: recorded.append(("tool_call", name)),
    )

    frames = [
        {"type": "delta", "text": "Today's brief covers"},
        {"type": "delta", "text": " three CVE updates"},
        {"type": "tool_call", "name": "mcp__anna_web__web_search"},
        {"type": "end_of_turn"},
        {"type": "error", "message": "ratelimit"},
        {"type": "bye"},
        # Anything past bye should never be read because the loop returns.
        {"type": "delta", "text": "unreachable"},
    ]

    reader = _make_reader_with_frames(frames)
    stop_event = asyncio.Event()
    reason = await asyncio.wait_for(
        _render_inbound(reader, stop_event=stop_event), timeout=2.0
    )

    assert reason == "bye"
    assert stop_event.is_set()
    assert recorded == [
        ("delta", "Today's brief covers"),
        ("delta", " three CVE updates"),
        ("tool_call", "mcp__anna_web__web_search"),
        ("end_of_turn", ""),
        ("error", "ratelimit"),
    ]


@pytest.mark.asyncio
async def test_render_inbound_returns_eof_on_unexpected_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the socket EOFs without a ``bye``, the loop returns ``"eof"``.

    This is the signal :func:`_run_chat` uses to print ``lost connection``
    to stderr and exit 1.
    """

    async def _passthrough(callback, *args, **kwargs):
        return callback()

    monkeypatch.setattr(chat_module, "run_in_terminal", _passthrough)
    monkeypatch.setattr(chat_module, "_print_delta", lambda text: None)

    reader = _make_reader_with_frames(
        [{"type": "delta", "text": "partial answer"}]
    )
    stop_event = asyncio.Event()
    reason = await asyncio.wait_for(
        _render_inbound(reader, stop_event=stop_event), timeout=2.0
    )

    assert reason == "eof"
    assert stop_event.is_set()


# ---------------------------------------------------------------------------
# 3. Handshake protocol-version mismatch
# ---------------------------------------------------------------------------


async def _serve_one_ack(
    socket_path: Path,
    *,
    ack_frame: dict,
    received: list[dict],
) -> asyncio.AbstractServer:
    """Bind a one-shot Unix-socket server that responds with ``ack_frame``.

    Records the first frame it reads (the client's ``hello``) into
    ``received`` so the test can assert the client sent the right shape.
    """

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if line:
            try:
                received.append(json.loads(line.decode("utf-8")))
            except json.JSONDecodeError:
                pass
        writer.write((json.dumps(ack_frame) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        except Exception:
            pass
        # Don't block on further reads — the client tears the socket down
        # as soon as it sees the mismatched ack. Closing here lets the
        # server's outer ``wait_closed`` resolve when the test tears down.
        try:
            writer.close()
        except Exception:
            pass

    return await asyncio.start_unix_server(_handle, path=str(socket_path))


@pytest.mark.asyncio
async def test_handshake_rejects_protocol_version_mismatch(tmp_path: Path) -> None:
    """A mismatched ``protocol_version`` in the ack raises ``_HandshakeError``.

    Per the plan's "Wire-format (socket framing)" section, the daemon
    echoes the version it speaks in the ack; the client refuses to
    proceed if it doesn't match :data:`_PROTOCOL_VERSION`.
    """
    sock_path = tmp_path / "anna.sock"
    received: list[dict] = []
    bad_ack = {
        "type": "ack",
        "conv_key": "cli:local:funtime",
        "protocol_version": 99,
    }
    server = await _serve_one_ack(sock_path, ack_frame=bad_ack, received=received)
    try:
        with pytest.raises(_HandshakeError) as exc_info:
            await _open_and_handshake(sock_path, username="funtime", timeout=2.0)
        msg = str(exc_info.value)
        assert "protocol" in msg.lower()
        assert "99" in msg
        # Client did send a well-formed hello before the failure.
        assert received and received[0]["type"] == "hello"
        assert received[0]["username"] == "funtime"
        assert received[0]["protocol_version"] == _PROTOCOL_VERSION
    finally:
        server.close()
        await server.wait_closed()
