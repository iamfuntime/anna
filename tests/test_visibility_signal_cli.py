"""Tests for the Phase 2 CLI thinking-signal frames.

Cadence-Visibility Hooks plan, subtask 10. The CLIAdapter overrides
``start_thinking_signal`` to write ``{"type": "thinking"}`` and
``clear_thinking_signal`` to write ``{"type": "thinking_done"}`` via
the existing session writer.

Three cases per the plan:

* ``thinking`` frame is written on start when a session exists.
* ``thinking_done`` frame is written on clear.
* Absent session at start AND at clear are no-ops returning cleanly.

The pattern mirrors ``tests/test_cli_adapter.py``: a real Unix socket
under ``tmp_path``, with the adapter started and a client connected via
``asyncio.open_unix_connection``. This keeps the writer / write_lock /
``UnixSocketServer.send_frame`` chain exercised end-to-end rather than
mocked at the framing layer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.transports.base import InboundEvent, SignalHandle
from anna.transports.cli import CLIAdapter


# ---------------------------------------------------------------------------
# Helpers (mirrored from tests/test_cli_adapter.py)
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.transports.cli.socket_path = str(tmp_path / "anna.sock")
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
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError(f"predicate not satisfied within {timeout}s")


def _make_event(conv_key: str) -> InboundEvent:
    return InboundEvent(
        transport="cli",
        conversation_key=conv_key,
        sender_id="funtime",
        sender_display="funtime",
        text="hi",
        is_dm=True,
        is_thread=False,
    )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_thinking_signal_writes_thinking_frame(tmp_path: Path) -> None:
    """``start_thinking_signal`` writes ``{"type": "thinking"}`` for a live session
    and returns a SignalHandle carrying the session key."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        reader, writer = await _open_client(Path(cfg.transports.cli.socket_path))
        await _send_line(
            writer,
            {"type": "hello", "mode": "interactive", "username": "funtime"},
        )
        await _read_frame(reader)  # consume ack
        await _wait_for(lambda: "cli:local:funtime" in adapter._sessions)

        event = _make_event("cli:local:funtime")
        handle = await adapter.start_thinking_signal(event)

        frame = await _read_frame(reader)
        assert frame == {"type": "thinking"}

        assert handle is not None
        assert handle.transport == "cli"
        assert handle.conv_key == "cli:local:funtime"
        assert handle.cli_session_key == "cli:local:funtime"

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_clear_thinking_signal_writes_thinking_done_frame(
    tmp_path: Path,
) -> None:
    """``clear_thinking_signal`` writes ``{"type": "thinking_done"}`` for a live
    session looked up via ``handle.cli_session_key``."""
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

        # Start first so the round-trip mirrors the real worker path.
        handle = await adapter.start_thinking_signal(
            _make_event("cli:local:funtime")
        )
        assert handle is not None
        start_frame = await _read_frame(reader)
        assert start_frame == {"type": "thinking"}

        await adapter.clear_thinking_signal(handle)
        clear_frame = await _read_frame(reader)
        assert clear_frame == {"type": "thinking_done"}

        writer.close()
        await writer.wait_closed()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_absent_session_is_noop_for_start_and_clear(tmp_path: Path) -> None:
    """When no session exists for the conv_key (operator closed the TUI
    mid-handoff), both ``start_thinking_signal`` and
    ``clear_thinking_signal`` are quiet no-ops that return cleanly. Start
    returns ``None`` so the worker can skip the clear path; clear with a
    handle pointing at a missing session simply returns."""
    cfg = _make_config(tmp_path)
    adapter = CLIAdapter(config=cfg)
    await adapter.start()
    try:
        # No connected client, no session registered.
        event = _make_event("cli:local:ghost")
        result = await adapter.start_thinking_signal(event)
        assert result is None

        # Clear with a synthesized handle pointing at the same missing
        # session — must not raise even though the lookup will miss.
        ghost_handle = SignalHandle(
            transport="cli",
            conv_key="cli:local:ghost",
            cli_session_key="cli:local:ghost",
        )
        await adapter.clear_thinking_signal(ghost_handle)
    finally:
        await adapter.stop()
