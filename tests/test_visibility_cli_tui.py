"""Tests for the TUI rendering of the visibility ``thinking`` / ``thinking_done``
frames added in Cadence-Visibility Hooks plan subtask 11.

The CLI adapter (subtask 10) now emits ``{"type": "thinking"}`` /
``{"type": "thinking_done"}`` frames. ``anna chat`` (interactive TUI) and
``anna ask`` (one-shot) both interpret them. These tests exercise the
protocol-level frame handlers in both clients with a hand-rolled
``asyncio.StreamReader`` so we don't need a live prompt_toolkit
application or real socket.

Two cases per the plan, exercised against both clients:

* (a) ``thinking`` → ``delta`` → ``end_of_turn`` — the first delta
  implicitly clears the thinking marker; nothing raises.
* (b) ``thinking`` → ``thinking_done`` → ``delta`` → ``end_of_turn`` —
  ``thinking_done`` flips the in-loop flag before the delta arrives.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from anna.cli import ask as ask_module
from anna.cli import chat as chat_module
from anna.cli.chat import _render_inbound


def _make_reader_with_frames(frames: list[dict]) -> asyncio.StreamReader:
    """Hand-roll a :class:`StreamReader` pre-loaded with NDJSON frames."""
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data((json.dumps(frame) + "\n").encode("utf-8"))
    reader.feed_eof()
    return reader


# ---------------------------------------------------------------------------
# ``anna chat`` — interactive TUI render loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_thinking_then_delta_then_end_of_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``thinking`` → ``delta`` → ``end_of_turn``: handler dispatches in order."""
    recorded: list[tuple[str, str]] = []

    async def _passthrough(callback, *args, **kwargs):
        return callback()

    monkeypatch.setattr(chat_module, "run_in_terminal", _passthrough)
    monkeypatch.setattr(
        chat_module,
        "_print_delta",
        lambda text: recorded.append(("delta", text)),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_thinking",
        lambda: recorded.append(("thinking", "")),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_end_of_turn",
        lambda: recorded.append(("end_of_turn", "")),
    )

    frames = [
        {"type": "thinking"},
        {"type": "delta", "text": "hi"},
        {"type": "end_of_turn"},
        {"type": "bye"},
    ]
    reader = _make_reader_with_frames(frames)
    stop_event = asyncio.Event()
    reason = await asyncio.wait_for(
        _render_inbound(reader, stop_event=stop_event), timeout=2.0
    )

    assert reason == "bye"
    assert recorded == [
        ("thinking", ""),
        ("delta", "hi"),
        ("end_of_turn", ""),
    ]


@pytest.mark.asyncio
async def test_chat_thinking_then_thinking_done_then_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``thinking`` → ``thinking_done`` → ``delta``: handler runs cleanly.

    ``thinking_done`` has no visible render call (the next output overwrites
    the marker); we assert the loop processed it without raising and the
    delta still flushed in order.
    """
    recorded: list[tuple[str, str]] = []

    async def _passthrough(callback, *args, **kwargs):
        return callback()

    monkeypatch.setattr(chat_module, "run_in_terminal", _passthrough)
    monkeypatch.setattr(
        chat_module,
        "_print_delta",
        lambda text: recorded.append(("delta", text)),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_thinking",
        lambda: recorded.append(("thinking", "")),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_end_of_turn",
        lambda: recorded.append(("end_of_turn", "")),
    )

    frames = [
        {"type": "thinking"},
        {"type": "thinking_done"},
        {"type": "delta", "text": "hi"},
        {"type": "end_of_turn"},
        {"type": "bye"},
    ]
    reader = _make_reader_with_frames(frames)
    stop_event = asyncio.Event()
    reason = await asyncio.wait_for(
        _render_inbound(reader, stop_event=stop_event), timeout=2.0
    )

    assert reason == "bye"
    # ``thinking_done`` is a silent state flip; no entry recorded for it.
    assert recorded == [
        ("thinking", ""),
        ("delta", "hi"),
        ("end_of_turn", ""),
    ]


@pytest.mark.asyncio
async def test_chat_thinking_is_not_reprinted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive ``thinking`` frames render only one marker.

    Guards against a daemon-side double-emit producing two ``[thinking…]``
    lines stacked on the operator's screen.
    """
    recorded: list[tuple[str, str]] = []

    async def _passthrough(callback, *args, **kwargs):
        return callback()

    monkeypatch.setattr(chat_module, "run_in_terminal", _passthrough)
    monkeypatch.setattr(
        chat_module,
        "_print_thinking",
        lambda: recorded.append(("thinking", "")),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_delta",
        lambda text: recorded.append(("delta", text)),
    )
    monkeypatch.setattr(
        chat_module,
        "_print_end_of_turn",
        lambda: recorded.append(("end_of_turn", "")),
    )

    frames = [
        {"type": "thinking"},
        {"type": "thinking"},
        {"type": "delta", "text": "hi"},
        {"type": "bye"},
    ]
    reader = _make_reader_with_frames(frames)
    stop_event = asyncio.Event()
    await asyncio.wait_for(
        _render_inbound(reader, stop_event=stop_event), timeout=2.0
    )

    # Only one thinking line printed even though two frames arrived.
    assert recorded.count(("thinking", "")) == 1
    assert ("delta", "hi") in recorded


# ---------------------------------------------------------------------------
# ``anna ask`` — one-shot client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_thinking_then_delta_then_end_of_turn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``thinking`` writes ``[thinking…]`` to stderr; delta to stdout; clean exit."""
    frames = [
        {"type": "thinking"},
        {"type": "delta", "text": "hi"},
        {"type": "end_of_turn"},
        {"type": "bye"},
    ]
    reader = _make_reader_with_frames(frames)
    exit_code = await ask_module._stream_response(reader)
    out, err = capsys.readouterr()

    assert exit_code == 0
    assert out == "hi\n"
    assert "[thinking…]" in err


@pytest.mark.asyncio
async def test_ask_thinking_then_thinking_done_then_delta(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``thinking_done`` is a silent no-op on the one-shot client.

    The stderr marker stays put; only one ``[thinking…]`` line is emitted.
    The subsequent delta and end_of_turn drive a clean exit.
    """
    frames = [
        {"type": "thinking"},
        {"type": "thinking_done"},
        {"type": "delta", "text": "hi"},
        {"type": "end_of_turn"},
        {"type": "bye"},
    ]
    reader = _make_reader_with_frames(frames)
    exit_code = await ask_module._stream_response(reader)
    out, err = capsys.readouterr()

    assert exit_code == 0
    assert out == "hi\n"
    # Exactly one stderr marker — thinking_done does NOT emit anything.
    assert err.count("[thinking…]") == 1
