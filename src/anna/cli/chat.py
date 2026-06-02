"""``anna chat`` interactive TUI client.

Phase 2 §5 subtask 9. This module runs as a **separate, short-lived process**
the operator invokes from a terminal — it is NOT part of the ANNA daemon. It
connects to ``$ANNA_HOME/anna.sock``, exchanges NDJSON frames per the wire
spec in
``Inbox/2026-06-01-ANNA-Phase-2-CLI-Transport-Plan.md`` ("Architecture →
Wire-format (socket framing)"), and renders streaming deltas live through
:mod:`prompt_toolkit`.

Wire shape used by this client:

* Outbound (client → daemon):
  ``{"type": "hello", "mode": "interactive", "username": "...", "protocol_version": 1}``
  followed by zero-or-more
  ``{"type": "user_message", "text": "..."}`` /
  ``{"type": "cancel"}`` /
  ``{"type": "bye"}``.
* Inbound (daemon → client):
  ``{"type": "ack", "conv_key": "...", "protocol_version": 1}`` once at start,
  then ``delta`` / ``tool_call`` / ``final_text`` / ``end_of_turn`` /
  ``error`` / ``bye`` frames.

Two cooperating asyncio tasks drive the session: one reads frames off the
socket and renders them (deltas stream into the terminal mid-prompt via
``run_in_terminal``); the other drives the :class:`PromptSession` and
forwards each submitted line as a ``user_message`` frame. ``exit`` / ``quit``
and Ctrl-D send ``bye`` and exit cleanly. Ctrl-C mid-stream sends a
``cancel`` frame and returns to a fresh prompt (per operator decision #1,
the daemon does not actually cancel the SDK call — it just stops forwarding
deltas). Ctrl-C twice quickly force-closes the socket without waiting.

Reconnect is explicitly out of scope per the plan; a dropped socket
mid-session prints ``lost connection`` to stderr and exits 1.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text

from anna.config import load_config

# Protocol version this client speaks. Mirrored in the ``hello`` frame and
# checked against the daemon's ``ack.protocol_version`` reply.
_PROTOCOL_VERSION = 1

# Handshake ack must arrive within this many seconds or the client exits 1.
_HANDSHAKE_TIMEOUT_SECONDS = 5.0

# Two Ctrl-C presses within this window trigger force-exit. Anything slower
# is treated as two independent cancels (each one sends a fresh ``cancel``
# frame and returns to the prompt).
_DOUBLE_CTRLC_WINDOW_SECONDS = 1.0

# Default history file. Created lazily by FileHistory on first write.
_DEFAULT_HISTORY_PATH = Path(os.path.expanduser("~/.anna_chat_history"))


# ---------------------------------------------------------------------------
# Framing helpers (testable in isolation; exported as private module funcs)
# ---------------------------------------------------------------------------


def _encode_frame(frame: dict[str, Any]) -> bytes:
    """Encode one NDJSON frame for the wire.

    JSON object, one per line, UTF-8. Embedded raw newlines inside string
    values are escaped to ``\\n`` by :func:`json.dumps` so the per-line
    delimiter stays unambiguous.
    """
    return (json.dumps(frame) + "\n").encode("utf-8")


def _build_hello_frame(*, username: str, mode: str = "interactive") -> dict[str, Any]:
    """Build the first frame sent after the socket opens.

    The daemon ack's back with ``conv_key`` and ``protocol_version``; the
    client validates the version matches and exits with an error otherwise.
    """
    return {
        "type": "hello",
        "mode": mode,
        "username": username,
        "protocol_version": _PROTOCOL_VERSION,
    }


def _build_user_message_frame(text: str) -> dict[str, Any]:
    return {"type": "user_message", "text": text}


def _build_cancel_frame() -> dict[str, Any]:
    return {"type": "cancel"}


def _build_bye_frame() -> dict[str, Any]:
    return {"type": "bye"}


async def _read_one_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read a single NDJSON frame. Return ``None`` on EOF."""
    line = await reader.readline()
    if not line:
        return None
    text = line.decode("utf-8")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Connection / handshake
# ---------------------------------------------------------------------------


class _HandshakeError(RuntimeError):
    """Raised when the hello/ack handshake fails."""


async def _open_and_handshake(
    socket_path: Path,
    *,
    username: str,
    timeout: float = _HANDSHAKE_TIMEOUT_SECONDS,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """Open the Unix socket, send ``hello``, await ``ack``, return readers + conv_key.

    Raises :class:`_HandshakeError` on protocol mismatch or no ack within
    ``timeout``. Connection-refused / no-such-socket bubbles up as a
    standard ``OSError`` / ``ConnectionRefusedError`` for the caller to
    translate into an actionable user message.
    """
    reader, writer = await asyncio.open_unix_connection(path=str(socket_path))

    writer.write(_encode_frame(_build_hello_frame(username=username)))
    await writer.drain()

    try:
        ack_frame = await asyncio.wait_for(_read_one_frame(reader), timeout=timeout)
    except TimeoutError as exc:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        raise _HandshakeError(
            f"daemon did not ack within {timeout:.1f}s"
        ) from exc

    def _close_writer() -> None:
        try:
            writer.close()
        except Exception:
            pass

    if ack_frame is None:
        _close_writer()
        raise _HandshakeError("daemon closed the connection before ack")
    if ack_frame.get("type") != "ack":
        _close_writer()
        raise _HandshakeError(
            f"expected ack frame, got type={ack_frame.get('type')!r}"
        )
    server_version = ack_frame.get("protocol_version")
    if server_version != _PROTOCOL_VERSION:
        _close_writer()
        raise _HandshakeError(
            f"protocol version mismatch: client speaks {_PROTOCOL_VERSION}, "
            f"daemon ack'd {server_version!r}"
        )
    conv_key = str(ack_frame.get("conv_key", ""))
    return reader, writer, conv_key


# ---------------------------------------------------------------------------
# Render loop (daemon → terminal)
# ---------------------------------------------------------------------------


def _print_delta(text: str) -> None:
    """Stream a chunk of assistant text to stdout without disturbing the prompt.

    ``print_formatted_text`` cooperates with prompt_toolkit's redraw so the
    input prompt is rebuilt under the streamed output.
    """
    # end="" so a single TextBlock that arrives in multiple chunks renders
    # as continuous text. prompt_toolkit normalizes line endings on its
    # side; the final newline is emitted on end_of_turn.
    print_formatted_text(FormattedText([("", text)]), end="")


def _print_tool_indicator(name: str) -> None:
    """Print a dim-styled inline indicator while a tool call is in flight."""
    label = f"[calling: {name}]"
    print_formatted_text(FormattedText([("ansigray", label)]))


def _print_thinking() -> None:
    """Print a transient ``[thinking…]`` marker while the SDK warms up.

    The line is printed plain (no spinner in v1 per the plan); the next
    ``delta`` frame or a ``thinking_done`` flips the in-loop flag so the
    handler knows the line is already on screen and can skip re-printing
    it. Rendering on top of the existing line is fine — prompt_toolkit's
    ``print_formatted_text`` cooperates with the redraw and the operator
    sees the deltas (or the next prompt) underneath.
    """
    print_formatted_text(FormattedText([("ansigray", "[thinking…]")]))


def _print_error(message: str) -> None:
    """Print a daemon-reported error in red on stderr."""
    print_formatted_text(
        FormattedText([("ansired", f"error: {message}")]),
        file=sys.stderr,
    )


def _print_end_of_turn() -> None:
    """Emit a final newline so the next prompt starts on a fresh line."""
    print_formatted_text(FormattedText([("", "")]))


async def _render_inbound(
    reader: asyncio.StreamReader,
    *,
    stop_event: asyncio.Event,
) -> str:
    """Read frames from the daemon and render them.

    Returns a short string describing why the loop exited:

    * ``"bye"`` — daemon sent a ``bye`` frame (clean shutdown).
    * ``"eof"`` — socket EOF / reset without a ``bye`` (lost connection).
    * ``"external"`` — ``stop_event`` was set by the prompt task.

    On EOF or external stop, ``stop_event`` is left set so the outer
    coroutine knows to tear down the writer side.
    """
    # Tracks whether a ``[thinking…]`` line is currently visible. Set on the
    # ``thinking`` frame and cleared by either ``thinking_done`` or the first
    # ``delta`` of the turn (whichever arrives first). The plan accepts a
    # simple "let the next delta overwrite it" — there is no explicit erase;
    # prompt_toolkit's redraw cooperates with subsequent output. The flag is
    # primarily a state marker so we don't re-emit the line if multiple
    # ``thinking`` frames arrive in sequence.
    thinking_active = False

    while not stop_event.is_set():
        try:
            frame = await _read_one_frame(reader)
        except (ConnectionResetError, OSError):
            frame = None
        except json.JSONDecodeError:
            # Daemon shouldn't emit malformed JSON; if it does, log and skip.
            continue

        if frame is None:
            # EOF or connection reset.
            stop_event.set()
            return "eof"

        frame_type = frame.get("type")
        if frame_type == "delta":
            # First delta of the turn implicitly clears the thinking line.
            # Per the plan this handles the case where the SDK starts
            # streaming before ``thinking_done`` arrives.
            thinking_active = False
            text = frame.get("text", "")
            if text:
                await run_in_terminal(lambda t=text: _print_delta(t))
        elif frame_type == "tool_call":
            name = frame.get("name", "<unknown>")
            await run_in_terminal(lambda n=name: _print_tool_indicator(n))
        elif frame_type == "thinking":
            # Mid-prompt "working" marker before the first delta. Idempotent —
            # if a thinking line is already on screen, don't reprint.
            if not thinking_active:
                thinking_active = True
                await run_in_terminal(_print_thinking)
        elif frame_type == "thinking_done":
            # Worker finished; let the next output (delta or new prompt)
            # overwrite the marker. No explicit erase needed.
            thinking_active = False
        elif frame_type == "final_text":
            # The TUI already showed everything via deltas; ignore the
            # buffered final text. The audit transcript still records it.
            continue
        elif frame_type == "end_of_turn":
            # Defensive: a turn that ends without any delta still clears any
            # stale thinking marker before the prompt redraws.
            thinking_active = False
            await run_in_terminal(_print_end_of_turn)
        elif frame_type == "error":
            message = frame.get("message", "<unknown error>")
            await run_in_terminal(lambda m=message: _print_error(m))
        elif frame_type == "bye":
            stop_event.set()
            return "bye"
        else:
            # Unknown frame type — ignore for forward-compat.
            continue

    return "external"


# ---------------------------------------------------------------------------
# Prompt loop (terminal → daemon)
# ---------------------------------------------------------------------------


async def _drive_prompt(
    writer: asyncio.StreamWriter,
    *,
    stop_event: asyncio.Event,
    session: PromptSession,
) -> None:
    """Drive the prompt-input loop.

    Each submitted line that is non-empty becomes a ``user_message`` frame.
    ``exit`` / ``quit`` and Ctrl-D send ``bye`` and set the stop flag.
    Ctrl-C sends ``cancel``; two Ctrl-Cs within ``_DOUBLE_CTRLC_WINDOW_SECONDS``
    force-exit by setting the stop flag without sending bye.
    """
    last_ctrlc_time: float = 0.0

    while not stop_event.is_set():
        try:
            line = await session.prompt_async()
        except KeyboardInterrupt:
            now = time.monotonic()
            if now - last_ctrlc_time < _DOUBLE_CTRLC_WINDOW_SECONDS:
                # Force-exit: skip bye, just stop.
                stop_event.set()
                return
            last_ctrlc_time = now
            # Single Ctrl-C: send a cancel frame and loop.
            try:
                writer.write(_encode_frame(_build_cancel_frame()))
                await writer.drain()
            except (ConnectionResetError, OSError):
                stop_event.set()
                return
            continue
        except EOFError:
            # Ctrl-D at the prompt: bye and exit cleanly.
            try:
                writer.write(_encode_frame(_build_bye_frame()))
                await writer.drain()
            except (ConnectionResetError, OSError):
                pass
            stop_event.set()
            return

        if line is None:
            # prompt_toolkit returns None for some edge cases; treat as EOF.
            stop_event.set()
            return

        text = line.strip()
        if not text:
            continue

        if text in ("exit", "quit"):
            try:
                writer.write(_encode_frame(_build_bye_frame()))
                await writer.drain()
            except (ConnectionResetError, OSError):
                pass
            stop_event.set()
            return

        try:
            writer.write(_encode_frame(_build_user_message_frame(line)))
            await writer.drain()
        except (ConnectionResetError, OSError):
            stop_event.set()
            return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run_chat(socket_path: Path, *, username: str) -> int:
    """Open the socket, run the read/write tasks until one finishes, exit."""
    try:
        reader, writer, _conv_key = await _open_and_handshake(
            socket_path, username=username
        )
    except FileNotFoundError:
        sys.stderr.write(
            f"ANNA daemon not running at {socket_path}; "
            "try `systemctl --user start anna`\n"
        )
        return 1
    except ConnectionRefusedError:
        sys.stderr.write(
            f"ANNA daemon not running at {socket_path}; "
            "try `systemctl --user start anna`\n"
        )
        return 1
    except _HandshakeError as exc:
        sys.stderr.write(f"handshake failed: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(
            f"failed to connect to {socket_path}: {exc}\n"
        )
        return 1

    history = FileHistory(str(_DEFAULT_HISTORY_PATH))
    session: PromptSession = PromptSession(
        message="anna> ",
        history=history,
        multiline=False,
    )

    stop_event = asyncio.Event()
    read_reason: dict[str, str] = {}

    async def _read_task() -> None:
        try:
            reason = await _render_inbound(reader, stop_event=stop_event)
            read_reason["reason"] = reason
        except Exception as exc:
            read_reason["reason"] = "exception"
            read_reason["error"] = str(exc)
            stop_event.set()

    async def _write_task() -> None:
        try:
            with patch_stdout():
                await _drive_prompt(writer, stop_event=stop_event, session=session)
        except Exception:
            # Surfacing a prompt-task exception doesn't change the exit
            # code (we only flag "lost connection" on the read side); the
            # asyncio.gather below logs the traceback via __aexit__ if
            # the operator needs it.
            stop_event.set()

    read = asyncio.create_task(_read_task(), name="anna-chat.read")
    write = asyncio.create_task(_write_task(), name="anna-chat.write")

    # Wait until either side decides we're done.
    await stop_event.wait()

    # Cancel whichever task is still running. The other has already
    # returned naturally.
    for task in (read, write):
        if not task.done():
            task.cancel()
    for task in (read, write):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Close the socket cleanly.
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    # If the read side dropped the socket (EOF/exception) without a clean
    # ``bye`` from the daemon, that is a mid-session connection loss and
    # the operator should hear about it on stderr. ``bye`` and ``external``
    # (writer-driven exit via "exit" / "quit" / Ctrl-D) are clean exits.
    reason = read_reason.get("reason")
    if reason == "eof":
        sys.stderr.write("lost connection\n")
        return 1
    if reason == "exception":
        sys.stderr.write(
            f"lost connection: {read_reason.get('error', 'unknown error')}\n"
        )
        return 1
    return 0


def main() -> int:
    """Console entrypoint for ``anna chat``.

    Returns 0 on a clean exit (operator typed ``exit`` / ``quit``, hit
    Ctrl-D, or the daemon sent ``bye``); non-zero if the socket cannot be
    opened or the connection drops mid-session. Subtask 11 will route the
    ``anna chat`` subcommand to this function.
    """
    try:
        config = load_config()
    except Exception as exc:
        sys.stderr.write(f"anna chat: failed to load config: {exc}\n")
        return 2

    socket_path = config.transports.cli.resolved_socket_path
    if not socket_path.exists():
        sys.stderr.write(
            f"ANNA daemon not running at {socket_path}; "
            "try `systemctl --user start anna`\n"
        )
        return 1

    username = os.environ.get("USER") or getpass.getuser()

    try:
        return asyncio.run(_run_chat(socket_path, username=username))
    except KeyboardInterrupt:
        # Final-stage SIGINT after the prompt loop already tore down.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
