"""Auth path resolution.

Per v3 section 1. ANNA inherits credentials in one of two ways:

* ``max`` mode reuses the Claude Code saved session (``claude login``). The
  SDK picks this up automatically when ``ANTHROPIC_API_KEY`` is unset.
* ``api_key`` mode reads ``ANTHROPIC_API_KEY`` from ``.env``. The SDK uses it
  on every request.

There is no automatic fallback at runtime. If MAX auth fails, ANNA emits an
admin alert via the surviving transports and the watchdog logs CRITICAL.
Silent fallback would invert the operator's billing intent.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anna.log import get_logger

_log = get_logger("anna.auth")


AuthMode = Literal["max", "api_key"]


def operator_credentials_path() -> Path:
    """Resolve the operator's real Claude Code credentials file.

    The bundled Claude CLI persists its OAuth session to
    ``<credentials dir>/.credentials.json``. ANNA shares that file with the
    operator's primary ``claude login`` so max-mode auth resolves and survives
    token rotation. This is the single source of truth for both the credentials
    file and (via :func:`operator_securestorage_dir`) the directory ANNA hands
    the CLI as ``CLAUDE_SECURESTORAGE_CONFIG_DIR``.
    """
    return Path(os.path.expanduser("~/.claude/.credentials.json"))


def operator_securestorage_dir() -> Path:
    """Resolve the operator's real ``~/.claude`` directory.

    This is the directory the bundled CLI reads/writes credentials in. ANNA
    passes it as ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` so OAuth token refresh
    (a temp-file + rename) lands directly on the shared ``.credentials.json``
    instead of clobbering a symlink. Derived as the parent of
    :func:`operator_credentials_path` so the value is never duplicated.
    """
    return operator_credentials_path().parent


@dataclass(frozen=True)
class AuthResult:
    mode: AuthMode
    api_key: str | None
    ready: bool
    reason: str


def resolve_auth(mode: AuthMode) -> AuthResult:
    """Inspect environment for the chosen auth path and return readiness.

    The runtime calls this once at startup and the watchdog calls it again on
    each cycle when the SDK heartbeat fails, so the recovery message can
    distinguish "credential never existed" from "credential expired."
    """
    if mode == "api_key":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return AuthResult(
                mode=mode,
                api_key=None,
                ready=False,
                reason="ANTHROPIC_API_KEY not set in environment",
            )
        return AuthResult(
            mode=mode,
            api_key=key,
            ready=True,
            reason="ANTHROPIC_API_KEY present",
        )

    # MAX mode. The SDK looks at the Claude Code config directory. We do not
    # crack that file ourselves; we just check that the directory exists and
    # let the SDK surface a real error if the session is stale.
    config_dir = os.path.expanduser("~/.config/claude-code")
    if not os.path.isdir(config_dir):
        # The SDK might still find credentials elsewhere; treat this as a
        # soft warning rather than a hard failure.
        return AuthResult(
            mode=mode,
            api_key=None,
            ready=True,
            reason="claude code config dir missing; SDK will look elsewhere",
        )
    return AuthResult(
        mode=mode,
        api_key=None,
        ready=True,
        reason="claude code session found",
    )


def auth_path_label(result: AuthResult) -> str:
    """Short human-readable label for log lines."""
    return f"{result.mode}:{'ready' if result.ready else 'not_ready'}"


def ensure_isolated_config_dir(runtime_dir: Path, mode: AuthMode) -> Path:
    """Prepare an isolated ``CLAUDE_CONFIG_DIR`` for ANNA's spawned CLIs.

    The bundled Claude CLI discovers host CLAUDE.md / skills / plugins / local
    MCP from ``CLAUDE_CONFIG_DIR`` (defaults to ``$HOME/.claude``). ANNA's
    daemon inherits the operator's HOME, so without an override every spawned
    subprocess leaks all of that. Pointing the subprocess at this isolated dir
    relocates that discovery off the operator's tree.

    Credentials are NOT seeded here. The bundled CLI resolves the credentials
    directory from a separate env var, ``CLAUDE_SECURESTORAGE_CONFIG_DIR``
    (see :func:`operator_securestorage_dir`), so both credential reads and the
    OAuth refresh-write land directly on the operator's shared
    ``~/.claude/.credentials.json``. There is no longer a credentials symlink to
    seed (or to be clobbered by the refresh's temp-file + rename).

    Idempotent: safe to call on every boot. Any stale ``.credentials.json``
    left in the runtime dir from the previous (symlink-seeding) design is
    removed so a previously-clobbered real file there cannot shadow the
    securestorage-resolved credentials. Cleanup failures downgrade to a WARNING
    rather than failing boot.

    Args:
        runtime_dir: The isolated dir to ready (typically
            ``config.claude_runtime_dir``).
        mode: The configured :data:`AuthMode`.

    Returns:
        ``runtime_dir`` (created, 0700).
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)

    # Remove any leftover .credentials.json (symlink, real file, or even a
    # directory) seeded by the previous design. With credentials now resolved
    # via CLAUDE_SECURESTORAGE_CONFIG_DIR, anything at this path in the runtime
    # dir is stale and could shadow the shared credentials. Tolerate its
    # absence and never fail boot.
    stale = runtime_dir / ".credentials.json"
    try:
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
        elif stale.is_symlink() or stale.exists():
            stale.unlink()
    except OSError as exc:
        _log.warning(
            "auth.isolated_config.stale_credentials_cleanup_failed",
            target=str(stale),
            runtime_dir=str(runtime_dir),
            error=str(exc),
        )

    return runtime_dir
