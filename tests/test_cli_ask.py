"""Tests for the ``anna ask`` one-shot CLI client.

Phase 2 §5 subtask 10. The client is exercised against a fake daemon
that binds a Unix socket at ``tmp_path / "anna.sock"`` and scripts the
frame sequence sent back per test case. The tests assert on:

1. The frames the client sends (``hello`` + ``user_message``) and the
   stdout the client prints (concatenated deltas + trailing newline).
2. The no-prompt exit code (2) and stderr usage banner.
3. The error-frame exit code (1) and stderr error line.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from anna.cli import ask as ask_module


# ---------------------------------------------------------------------------
# Fake daemon: a one-connection Unix-socket server that scripts replies.
# ---------------------------------------------------------------------------


class FakeDaemon:
    """A scripted Unix-socket server for testing the ``anna ask`` client.

    Binds at ``socket_path`` and accepts one connection. The
    ``script`` list is the sequence of outbound frames to send: each
    frame is either a ``dict`` (serialized to one NDJSON line) or the
    sentinel string ``"close"`` (close the writer without further
    frames). The first frame is sent *after* the client's ``hello``
    arrives so an ``ack`` can carry a correct ``protocol_version``.

    Inbound frames the client sends are recorded in ``received`` for
    assertion.
    """

    def __init__(self, socket_path: Path, script: list[Any]) -> None:
        self._socket_path = socket_path
        self._script = script
        self._server: asyncio.AbstractServer | None = None
        self.received: list[dict[str, Any]] = []
        self._done = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._socket_path)
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def wait_done(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._done.wait(), timeout=timeout)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # Read inbound until we have the hello + user_message,
            # interleaved with sending scripted outbound frames.
            hello_line = await reader.readline()
            if hello_line:
                self.received.append(json.loads(hello_line.decode("utf-8")))

            # Drain scripted frames in order. After the first frame
            # (typically the ack) read the user_message inbound.
            for idx, item in enumerate(self._script):
                if item == "close":
                    break
                payload = (json.dumps(item) + "\n").encode("utf-8")
                writer.write(payload)
                await writer.drain()

                if idx == 0:
                    # Right after the ack, the client sends user_message.
                    msg_line = await reader.readline()
                    if msg_line:
                        self.received.append(json.loads(msg_line.decode("utf-8")))
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self._done.set()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_streams_deltas_to_stdout_and_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: hello + user_message → deltas → end_of_turn → bye → exit 0."""
    sock_path = tmp_path / "anna.sock"
    script: list[Any] = [
        {"type": "ack", "conv_key": "cli:oneshot:abc", "protocol_version": 1},
        {"type": "delta", "text": "Hello"},
        {"type": "delta", "text": ", world"},
        {"type": "end_of_turn"},
        {"type": "bye"},
    ]
    daemon = FakeDaemon(sock_path, script)
    await daemon.start()
    try:
        exit_code = await ask_module._run(sock_path, "say hi")
        await daemon.wait_done()
    finally:
        await daemon.stop()

    out, err = capsys.readouterr()

    assert exit_code == 0
    # Streaming UX: deltas are concatenated, single trailing newline
    # added by end_of_turn.
    assert out == "Hello, world\n"

    # The client sent exactly hello + user_message.
    assert len(daemon.received) == 2
    hello = daemon.received[0]
    assert hello["type"] == "hello"
    assert hello["mode"] == "oneshot"
    assert hello["protocol_version"] == 1
    assert isinstance(hello.get("username"), str) and hello["username"]
    user_message = daemon.received[1]
    assert user_message == {"type": "user_message", "text": "say hi"}

    # No stderr noise on the happy path.
    assert err == ""


def test_no_prompt_returns_exit_code_two_and_prints_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``anna ask`` with no argv prompt exits 2 and writes usage to stderr."""
    monkeypatch.setattr("sys.argv", ["anna-ask"])
    exit_code = ask_module.main()
    out, err = capsys.readouterr()
    assert exit_code == 2
    assert "usage" in err.lower()
    assert out == ""


def test_empty_prompt_returns_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A whitespace-only argv prompt is treated as missing."""
    monkeypatch.setattr("sys.argv", ["anna-ask", "   "])
    exit_code = ask_module.main()
    _out, err = capsys.readouterr()
    assert exit_code == 2
    assert "usage" in err.lower()


async def test_error_frame_in_response_exits_one_with_stderr_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ``error`` frame mid-stream surfaces on stderr and forces exit 1."""
    sock_path = tmp_path / "anna.sock"
    script: list[Any] = [
        {"type": "ack", "conv_key": "cli:oneshot:abc", "protocol_version": 1},
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "worker crashed"},
        {"type": "bye"},
    ]
    daemon = FakeDaemon(sock_path, script)
    await daemon.start()
    try:
        exit_code = await ask_module._run(sock_path, "go")
        await daemon.wait_done()
    finally:
        await daemon.stop()

    out, err = capsys.readouterr()

    assert exit_code == 1
    # The partial delta still flushed to stdout — the client doesn't
    # rewind. End-of-turn newline is NOT emitted because we never saw
    # end_of_turn before the error.
    assert out == "partial"
    assert "worker crashed" in err
