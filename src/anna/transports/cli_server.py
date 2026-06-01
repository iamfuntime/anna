"""Unix-domain-socket server for the CLI transport.

Per the Phase 2 §5 CLI Transport plan. The daemon hosts this server at
``$ANNA_HOME/anna.sock`` with owner-only permissions (mode 0600); the
operator's ``anna chat`` / ``anna ask`` invocations connect to it as
short-lived client processes.

Wire format: newline-delimited JSON (NDJSON). One JSON object per line,
UTF-8 encoded. Embedded newlines inside string values are escaped to
``\\n`` by :func:`json.dumps` so the per-line delimiter is unambiguous.

This module is intentionally standalone — it knows only about the wire
format and the :class:`ClientSession` shape. Conversation-key derivation,
inbound-event construction, and router dispatch are the responsibility of
the :class:`CLIAdapter` that wraps this server (subtask 5).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from anna.log import get_logger

_log = get_logger("anna.transport.cli_server")


@dataclass
class ClientSession:
    """One accepted connection.

    The adapter holds these in a dict keyed by ``conv_key`` so outbound
    frames can be routed back to the right client. Mutable because
    ``closed`` and ``write_lock`` are stateful asyncio primitives; not
    frozen, and not ``compare=False``-decorated because sessions are
    looked up by conv_key rather than compared structurally.
    """

    conv_key: str
    username: str
    mode: Literal["interactive", "oneshot"]
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock
    closed: asyncio.Event


OnSession = Callable[[ClientSession], Awaitable[None]]
OnInbound = Callable[[ClientSession, dict[str, Any]], Awaitable[None]]


# Frame types the server recognizes after the ``hello`` handshake. Lines
# whose ``type`` is not in this set are logged and dropped; the connection
# stays open so a well-formed follow-up still dispatches.
_VALID_INBOUND_TYPES = frozenset({"user_message", "cancel", "bye"})


class UnixSocketServer:
    """Async Unix-domain-socket server speaking NDJSON.

    One task per accepted connection. The first line on every connection
    must be a ``hello`` frame carrying ``mode`` and ``username``; from
    that the server constructs a :class:`ClientSession` and hands it to
    the adapter via ``on_session``. Every subsequent line is parsed and
    forwarded to ``on_inbound``.

    The server does not synthesize the ``conv_key`` — the adapter derives
    it from ``mode`` + ``username`` (and may apply identity aliasing).
    The server simply records the value the adapter computes back onto
    the session before dispatching inbound frames.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        on_session: OnSession,
        on_inbound: OnInbound,
    ) -> None:
        self._socket_path = socket_path
        self._on_session = on_session
        self._on_inbound = on_inbound
        self._server: asyncio.AbstractServer | None = None
        self._connection_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind the Unix socket and start accepting connections.

        Any stale socket file at ``socket_path`` is unlinked first (the
        previous shutdown may not have cleaned it up). The socket is
        chmod'd to ``0o600`` immediately after the bind succeeds so it is
        never world-readable, not even for the window between bind and
        the first accept.
        """
        # Defensive unlink: ``missing_ok=True`` swallows FileNotFoundError;
        # other OSErrors (EACCES on a socket owned by another user, etc.)
        # surface to the caller because we cannot safely overwrite them.
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "cli_server.stale_unlink_failed",
                socket_path=str(self._socket_path),
                error=str(exc),
            )
            raise

        # Ensure the parent directory exists; otherwise start_unix_server
        # fails with a confusing FileNotFoundError on the bind.
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path),
        )
        # Tighten permissions immediately. asyncio.start_unix_server
        # creates the socket with the process umask, which is typically
        # 0o022 (mode 0o755). We want owner-only.
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError as exc:
            # If chmod fails the socket would be world-readable — refuse
            # to keep running. Close the server we just bound.
            _log.error(
                "cli_server.chmod_failed",
                socket_path=str(self._socket_path),
                error=str(exc),
            )
            await self._close_server()
            raise

        _log.info(
            "cli_server.bind",
            socket_path=str(self._socket_path),
            mode="0o600",
        )

    async def stop(self) -> None:
        """Close the server, cancel in-flight per-connection tasks, and unlink the socket."""
        await self._close_server()

        # Best-effort unlink. The socket file should not survive across
        # daemon restarts.
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "cli_server.unlink_failed",
                socket_path=str(self._socket_path),
                error=str(exc),
            )

        _log.info("cli_server.stop", socket_path=str(self._socket_path))

    async def _close_server(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("cli_server.wait_closed_failed", error=str(exc))
        self._server = None

        # Cancel any per-connection tasks still running. The accepted
        # connections will see CancelledError inside their read loop.
        for task in list(self._connection_tasks):
            task.cancel()
        for task in list(self._connection_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._connection_tasks.clear()

    @property
    def is_serving(self) -> bool:
        """Return whether the server is bound and accepting connections."""
        return self._server is not None and self._server.is_serving()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """One accepted connection.

        Reads the ``hello`` frame, builds a session, fires ``on_session``
        once, then loops on ``reader.readline()`` calling ``on_inbound``
        per valid frame. Malformed lines are logged and dropped without
        terminating the connection. EOF or ``ConnectionResetError`` ends
        the loop cleanly.
        """
        # Track the task so stop() can cancel mid-read loops.
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)

        peer = _peer_repr(writer)
        _log.info("cli_server.accept", peer=peer)

        session: ClientSession | None = None
        try:
            # ---- Hello handshake ----------------------------------------------------
            try:
                hello_line = await reader.readline()
            except (ConnectionResetError, OSError) as exc:
                _log.warning(
                    "cli_server.hello_read_failed",
                    peer=peer,
                    error=str(exc),
                )
                return
            if not hello_line:
                _log.warning("cli_server.hello_eof", peer=peer)
                return

            try:
                hello = _parse_frame(hello_line)
            except ValueError as exc:
                _log.warning(
                    "cli_server.frame_drop",
                    peer=peer,
                    stage="hello",
                    reason=str(exc),
                )
                return

            if hello.get("type") != "hello":
                _log.warning(
                    "cli_server.frame_drop",
                    peer=peer,
                    stage="hello",
                    reason="expected hello frame",
                    got_type=hello.get("type"),
                )
                return

            mode = hello.get("mode")
            username = hello.get("username")
            if mode not in ("interactive", "oneshot") or not isinstance(username, str) or not username:
                _log.warning(
                    "cli_server.frame_drop",
                    peer=peer,
                    stage="hello",
                    reason="invalid hello fields",
                    mode=mode,
                    username=username,
                )
                return

            # The adapter (subtask 5) computes the real conv_key from
            # mode + username and may overwrite session.conv_key in
            # ``on_session``. We seed with a placeholder shaped like the
            # un-aliased form so logs are still useful if the adapter
            # never replaces it.
            placeholder_conv_key = (
                f"cli:local:{username}" if mode == "interactive" else f"cli:oneshot:{username}"
            )

            session = ClientSession(
                conv_key=placeholder_conv_key,
                username=username,
                mode=mode,
                writer=writer,
                write_lock=asyncio.Lock(),
                closed=asyncio.Event(),
            )

            _log.info(
                "cli_server.client_hello",
                peer=peer,
                username=username,
                mode=mode,
                protocol_version=hello.get("protocol_version"),
            )

            try:
                await self._on_session(session)
            except Exception as exc:
                _log.error(
                    "cli_server.on_session_failed",
                    peer=peer,
                    username=username,
                    error=str(exc),
                )
                return

            # ---- Inbound read loop --------------------------------------------------
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionResetError, OSError) as exc:
                    _log.info(
                        "cli_server.client_close",
                        peer=peer,
                        conv_key=session.conv_key,
                        reason="connection_reset",
                        error=str(exc),
                    )
                    break
                if not line:
                    _log.info(
                        "cli_server.client_close",
                        peer=peer,
                        conv_key=session.conv_key,
                        reason="eof",
                    )
                    break

                try:
                    frame = _parse_frame(line)
                except ValueError as exc:
                    _log.warning(
                        "cli_server.frame_drop",
                        peer=peer,
                        conv_key=session.conv_key,
                        reason=str(exc),
                    )
                    continue

                frame_type = frame.get("type")
                if frame_type not in _VALID_INBOUND_TYPES:
                    _log.warning(
                        "cli_server.frame_drop",
                        peer=peer,
                        conv_key=session.conv_key,
                        reason="unrecognized type",
                        got_type=frame_type,
                    )
                    continue

                try:
                    await self._on_inbound(session, frame)
                except Exception as exc:
                    _log.error(
                        "cli_server.on_inbound_failed",
                        peer=peer,
                        conv_key=session.conv_key,
                        error=str(exc),
                    )
                    # Keep reading; one failing inbound shouldn't drop
                    # the whole session.

                if frame_type == "bye":
                    _log.info(
                        "cli_server.client_close",
                        peer=peer,
                        conv_key=session.conv_key,
                        reason="bye",
                    )
                    break
        finally:
            if session is not None:
                session.closed.set()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if task is not None:
                self._connection_tasks.discard(task)

    # ------------------------------------------------------------------
    # Outbound framing
    # ------------------------------------------------------------------

    @staticmethod
    async def send_frame(
        writer: asyncio.StreamWriter,
        lock: asyncio.Lock,
        frame: dict[str, Any],
    ) -> None:
        """Serialize and send a frame, holding ``lock`` for the duration.

        The lock ensures concurrent writes (e.g., a streaming ``delta``
        racing an ``end_of_turn``) don't interleave at the byte level on
        the wire. JSON encoding uses default settings so embedded
        newlines in strings are escaped to ``\\n`` and the per-line
        delimiter (``\\n``) stays unambiguous.
        """
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        async with lock:
            writer.write(payload)
            await writer.drain()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_frame(line: bytes) -> dict[str, Any]:
    """Decode one NDJSON line into a dict.

    Raises ``ValueError`` if the line is not valid UTF-8, not valid JSON,
    or not a JSON object. The caller logs and drops on failure.
    """
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"non-utf8: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"frame must be a JSON object, got {type(obj).__name__}")
    return obj


def _peer_repr(writer: asyncio.StreamWriter) -> str:
    """Best-effort peer-name for log lines. Unix sockets typically have no peername."""
    try:
        peer = writer.get_extra_info("peername")
        if peer:
            return repr(peer)
    except Exception:
        pass
    return "unix-peer"
