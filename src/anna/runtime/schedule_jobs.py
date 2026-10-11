"""Typed results for scheduled jobs.

The scheduler executes a *job* per occurrence and persists a ``JobResult``.  The
legacy agent-prompt job produces one from the worker reply; deterministic jobs
(the planned read-only heartbeat) can return structured ``data`` without going
through a model.  ``outcome`` is execution only - delivery is tracked separately
in the outbox.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Outcome = Literal["ok", "quiet", "failed"]
SideEffects = Literal["none", "possible", "unknown"]


@dataclass
class JobResult:
    outcome: Outcome
    output: str = ""                      # text to deliver when outcome == "ok"
    error: str | None = None
    quiet_reason: str | None = None       # why nothing is delivered (quiet outcome)
    tool_call_count: int | None = None
    side_effects: SideEffects = "unknown"
    data: dict[str, Any] = field(default_factory=dict)  # structured payload for typed jobs

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["output_chars"] = len(self.output)
        d.pop("output")  # delivery text lives in the outbox, not duplicated here
        return d
