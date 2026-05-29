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
from dataclasses import dataclass
from typing import Literal


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
