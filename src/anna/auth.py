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

    The only thing seeded into the dir is a symlink to the real
    ``~/.claude/.credentials.json`` so that max-mode OAuth still resolves. In
    ``api_key`` mode the dir is created empty (the key comes from the env).

    Idempotent: safe to call on every boot. An existing link/file at the
    target is replaced so the symlink self-heals if the source rotates. A
    directory squatting at the target is removed first, and any seeding failure
    downgrades to a WARNING rather than failing boot.

    Args:
        runtime_dir: The isolated dir to ready (typically
            ``config.claude_runtime_dir``).
        mode: The configured :data:`AuthMode`.

    Returns:
        ``runtime_dir`` (created, 0700, optionally seeded).
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)

    if mode != "max":
        # api_key mode: the SDK reads ANTHROPIC_API_KEY from the env, so there
        # is nothing to seed. The empty isolated dir is enough to relocate
        # host discovery.
        return runtime_dir

    source = Path(os.path.expanduser("~/.claude/.credentials.json"))
    target = runtime_dir / ".credentials.json"

    if not source.exists():
        # Max mode but no host credentials. The SDK may still find credentials
        # elsewhere; warn and continue rather than failing boot.
        _log.warning(
            "auth.isolated_config.credentials_missing",
            source=str(source),
            runtime_dir=str(runtime_dir),
        )
        return runtime_dir

    # Replace any stale link/file so the symlink self-heals across credential
    # rotations and across a switch from a previously-copied file. Seeding must
    # never fail boot: a directory squatting at the target, a permission error,
    # or any other OSError downgrades to a WARNING and leaves credentials
    # unlinked (the caller derives credentials_linked from is_symlink()).
    try:
        if target.is_dir() and not target.is_symlink():
            # A real directory at the credential path cannot be unlink()'d;
            # remove the tree so the symlink can take its place.
            shutil.rmtree(target)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(source)
    except OSError as exc:
        _log.warning(
            "auth.isolated_config.seed_failed",
            source=str(source),
            target=str(target),
            runtime_dir=str(runtime_dir),
            error=str(exc),
        )
    return runtime_dir
