"""``anna ask "<prompt>"`` one-shot CLI client.

Phase 2 §5 subtask 10. A short-lived process the operator runs as
``anna ask "what's in MEMORY.md?"`` that connects to the daemon's Unix
socket, sends a single ``user_message`` in ``oneshot`` mode, streams
the reply to stdout as ``delta`` frames arrive, and exits when the
daemon emits ``end_of_turn`` + ``bye``.

Streaming UX: each ``delta`` is written to stdout and flushed
immediately, with no trailing newline (the daemon's deltas may split
words). A single trailing newline is emitted on ``end_of_turn``.

``tool_call`` frames render as ``[tool: <name>]`` on stderr so the
operator sees activity without polluting stdout.

Ctrl-C closes the socket and exits 1. Per operator decision #1, no
SDK-level cancellation is sent — the daemon-side worker continues to
completion and the audit log records the full turn. The client just
stops listening.

A client-side wall-clock cap (default 5 minutes) bounds the worst case
in case the daemon never emits ``end_of_turn``; this is independent of
the daemon's own ``default_timeout_seconds``.

See ``Inbox/2026-06-01-ANNA-Phase-2-CLI-Transport-Plan.md`` for the
full design.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from anna.config import load_config

# Protocol version this client speaks. Must match the daemon's
# accepted version on the ``ack`` frame; on mismatch we exit non-zero
# rather than risk a confused half-handshake.
_PROTOCOL_VERSION = 1

# Wall-clock cap for the whole turn, in seconds. The daemon-side
# ``default_timeout_seconds`` is the real bound; this is a client-side
# safety net so the operator's terminal never hangs forever.
_DEFAULT_OVERALL_TIMEOUT_SECONDS = 300.0

# Handshake-only timeout: how long we wait for the ``ack`` after
# sending ``hello``. Short — if the daemon is alive it acks almost
# immediately. Distinct from the overall-turn cap.
_HELLO_ACK_TIMEOUT_SECONDS = 5.0

_USAGE = "usage: anna ask \"<prompt>\"\n"


def _parse_argv(argv: list[str]) -> str | None:
    """Return the prompt string from argv, or ``None`` if missing.

    ``argv`` is ``sys.argv[1:]`` (the prompt tokens after ``anna ask``).
    Multiple tokens are joined with spaces so shell users can write
    ``anna ask hello world`` without quotes, but quoted forms work too.
    Empty after stripping → treat as missing.
    """
    if not argv:
        return None
    prompt = " ".join(argv).strip()
    if not prompt:
        return None
    return prompt


async def _run(
    socket_path: Path,
    prompt: str,
    *,
    overall_timeout: float = _DEFAULT_OVERALL_TIMEOUT_SECONDS,
) -> int:
    """Open the socket, send the prompt, stream the reply, return exit code.

    Returns:
        0 on a clean ``bye``,
        1 on connection failure / error frame / timeout,
        (the no-prompt and protocol-mismatch cases return earlier in ``main``).
    """
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        sys.stderr.write(
            f"ANNA daemon not running at {socket_path}; "
            "try `systemctl --user start anna`\n"
        )
        sys.stderr.write(f"  ({type(exc).__name__}: {exc})\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"failed to connect to {socket_path}: {exc}\n")
        return 1

    try:
        # ---- Hello handshake -------------------------------------------------
        username = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
        await _send_frame(
            writer,
            {
                "type": "hello",
                "mode": "oneshot",
                "username": username,
                "protocol_version": _PROTOCOL_VERSION,
            },
        )

        try:
            ack = await asyncio.wait_for(
                _read_frame(reader),
                timeout=_HELLO_ACK_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            sys.stderr.write(
                f"timed out waiting for ack from {socket_path} "
                f"after {_HELLO_ACK_TIMEOUT_SECONDS:.0f}s\n"
            )
            return 1
        except EOFError:
            sys.stderr.write("connection closed before ack\n")
            return 1

        if ack is None or ack.get("type") != "ack":
            sys.stderr.write(f"expected ack, got: {ack!r}\n")
            return 1

        server_version = ack.get("protocol_version")
        if server_version != _PROTOCOL_VERSION:
            sys.stderr.write(
                f"protocol version mismatch: client={_PROTOCOL_VERSION}, "
                f"server={server_version!r}\n"
            )
            return 1

        # ---- Send the prompt -------------------------------------------------
        await _send_frame(writer, {"type": "user_message", "text": prompt})

        # ---- Stream the response ---------------------------------------------
        try:
            return await asyncio.wait_for(
                _stream_response(reader),
                timeout=overall_timeout,
            )
        except TimeoutError:
            sys.stderr.write(
                f"request exceeded timeout ({overall_timeout:.0f}s)\n"
            )
            return 1
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            # Best-effort. The remote side may already be gone.
            pass


async def _stream_response(reader: asyncio.StreamReader) -> int:
    """Read frames until ``bye`` / EOF, dispatching by ``type``.

    Returns the exit code: 0 on clean ``bye`` after ``end_of_turn``,
    1 on an ``error`` frame or unexpected EOF before ``end_of_turn``.
    """
    saw_end_of_turn = False
    error_seen = False

    while True:
        try:
            frame = await _read_frame(reader)
        except EOFError:
            # Server closed the connection. If we already saw
            # end_of_turn the bye frame may have been coalesced into
            # the close — treat as clean. Otherwise it's an unexpected
            # drop.
            if saw_end_of_turn and not error_seen:
                return 0
            if error_seen:
                return 1
            sys.stderr.write("connection closed before end_of_turn\n")
            return 1

        if frame is None:
            # Malformed frame; the parser already logged. Continue.
            continue

        frame_type = frame.get("type")
        if frame_type == "delta":
            text = frame.get("text", "")
            if isinstance(text, str) and text:
                sys.stdout.write(text)
                sys.stdout.flush()
        elif frame_type == "tool_call":
            name = frame.get("name", "?")
            sys.stderr.write(f"[tool: {name}]\n")
            sys.stderr.flush()
        elif frame_type == "thinking":
            # Lower-fidelity "working" marker for the one-shot client. Goes
            # to stderr so the captured stdout stays clean for scripting
            # (``anna ask ... > out.txt``). No erase logic: stderr/stdout
            # interleaving in a terminal handles itself, and the marker is
            # a transient operator-facing hint.
            sys.stderr.write("[thinking…]\n")
            sys.stderr.flush()
        elif frame_type == "thinking_done":
            # Nothing to do — the stderr line stays as a transient marker
            # and the next delta on stdout flows beneath it.
            continue
        elif frame_type == "final_text":
            # The daemon emits final_text + end_of_turn after the
            # streaming deltas to preserve audit-trail parity with
            # Slack/Telegram. The TUI/ask client ignores it — the
            # operator has already seen the content via deltas.
            continue
        elif frame_type == "end_of_turn":
            sys.stdout.write("\n")
            sys.stdout.flush()
            saw_end_of_turn = True
        elif frame_type == "error":
            message = frame.get("message", "(no message)")
            sys.stderr.write(f"error: {message}\n")
            sys.stderr.flush()
            error_seen = True
            # Continue to drain until close — the daemon may still
            # send a bye to wrap the session cleanly.
        elif frame_type == "bye":
            return 1 if error_seen else 0
        else:
            # Forward-compat: ignore frames we don't recognize.
            sys.stderr.write(f"[unknown frame type: {frame_type!r}]\n")
            sys.stderr.flush()


async def _send_frame(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    """Serialize ``frame`` as one NDJSON line and flush it."""
    payload = (json.dumps(frame) + "\n").encode("utf-8")
    writer.write(payload)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one NDJSON line and decode it.

    Returns the parsed dict, or ``None`` on a malformed line (the line
    is dropped and the caller keeps reading). Raises :class:`EOFError`
    if the peer closed the connection cleanly.
    """
    line = await reader.readline()
    if not line:
        raise EOFError()
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        sys.stderr.write("[malformed frame: non-utf8]\n")
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[malformed frame: {exc.msg}]\n")
        return None
    if not isinstance(obj, dict):
        sys.stderr.write(f"[malformed frame: not an object ({type(obj).__name__})]\n")
        return None
    return obj


def main() -> int:
    """``anna ask "<prompt>"`` entry point.

    Subtask 11 will route ``anna ask "..."`` → :func:`anna.cli.ask.main`
    via the subcommand dispatcher; for now ``sys.argv[1:]`` is the
    prompt tokens.

    Exit codes:
      0   clean response delivered
      1   connection error, server-side error frame, or timeout
      2   no prompt provided (usage)
    """
    prompt = _parse_argv(sys.argv[1:])
    if prompt is None:
        sys.stderr.write(_USAGE)
        return 2

    try:
        config = load_config()
    except Exception as exc:
        sys.stderr.write(f"failed to load anna config: {exc}\n")
        return 1

    socket_path = config.transports.cli.resolved_socket_path

    try:
        return asyncio.run(_run(socket_path, prompt))
    except KeyboardInterrupt:
        # Ctrl-C closes the socket (asyncio.run cleans up the task);
        # exit non-zero so scripts can detect the interrupt. The
        # daemon-side worker continues to completion (operator
        # decision #1) and the audit log records the turn.
        sys.stderr.write("\ninterrupted\n")
        return 1
