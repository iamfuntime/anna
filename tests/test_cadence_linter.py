"""Tests for the :class:`CadenceLinter` from subtask 4 of the
Cadence-Visibility Hooks plan (Inbox/2026-06-02).

Three cases per the plan:

* **match** — text containing a known bad-cadence phrase triggers one
  warning with the right pattern + matched_substring fields, plus one
  audit line of the same shape.
* **no-match** — clean text triggers zero warnings and zero audit
  events.
* **multiple matches** — text hitting two distinct patterns emits two
  distinct audit lines, one per match.

The linter writes to two surfaces (the structured operational stream
via ``structlog`` and the daily JSONL audit file via
:func:`anna.log.audit_event`). The tests assert against the audit
file directly because that path is the most user-visible artifact and
because ``audit_event`` already mirrors the same payload onto the
structured stream — so verifying the JSONL is the cleanest single
assertion. The structured-log path is then sanity-checked via a
monkeypatched bound logger so we know both surfaces fire.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.runtime.visibility import CadenceLinter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, patterns: list[str] | None = None) -> AnnaConfig:
    """Build an :class:`AnnaConfig` rooted in ``tmp_path`` with overridable
    ``runtime.visibility.lint_patterns``. Default keeps the shipped list so
    the match test exercises a real pattern (``^Synthesizing:``).
    """
    overrides: dict[str, Any] = {"auth": {"mode": "max"}}
    if patterns is not None:
        overrides["runtime"] = {"visibility": {"lint_patterns": patterns}}
    cfg = AnnaConfig.model_validate(overrides)
    return cfg.model_copy(update={"anna_home": tmp_path})


def _audit_events(audit_dir: Path) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = audit_dir / f"audit-{today}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class _CapturingLogger:
    """Minimal stand-in for the structlog bound logger used by the linter.

    Captures every ``warning`` call so the test can verify the structured
    stream fired alongside the audit-file write.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))


@pytest.fixture
def capture_log(monkeypatch: pytest.MonkeyPatch) -> _CapturingLogger:
    """Replace the linter module's bound logger with a capturing stub.

    The linter binds its logger in ``__init__`` via
    ``get_logger("anna.visibility.lint")``. Patching ``get_logger`` on the
    module is the lowest-friction way to capture every warning emitted by
    a freshly-constructed linter without touching the global structlog
    configuration.
    """
    cap = _CapturingLogger()
    monkeypatch.setattr(
        "anna.runtime.visibility.get_logger",
        lambda name: cap,
    )
    return cap


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_lint_match_emits_warning_and_audit_line(
    tmp_path: Path,
    capture_log: _CapturingLogger,
) -> None:
    """A text containing ``Synthesizing:`` at line-start (one of the
    shipped patterns) emits exactly one structured warning AND exactly
    one audit line, both carrying the pattern source and the matched
    substring (truncated to <=80 chars).
    """
    cfg = _make_config(tmp_path)
    linter = CadenceLinter(config=cfg)

    linter.lint(
        "Synthesizing: pulling the threads together",
        transport="slack",
        conv_key="slack:dm:U123",
    )

    # Structured stream
    assert len(capture_log.warnings) == 1
    event, kwargs = capture_log.warnings[0]
    assert event == "worker.cadence_lint.warn"
    assert kwargs["pattern"] == r"^Synthesizing:"
    assert kwargs["matched_substring"] == "Synthesizing:"
    assert kwargs["conv_key"] == "slack:dm:U123"
    assert kwargs["transport"] == "slack"

    # Audit JSONL
    events = _audit_events(cfg.audit_dir)
    warn_events = [e for e in events if e["event"] == "worker.cadence_lint.warn"]
    assert len(warn_events) == 1
    rec = warn_events[0]
    assert rec["pattern"] == r"^Synthesizing:"
    assert rec["matched_substring"] == "Synthesizing:"
    assert rec["conv_key"] == "slack:dm:U123"
    assert rec["transport"] == "slack"
    assert rec["level"] == "WARNING"


def test_lint_clean_text_emits_nothing(
    tmp_path: Path,
    capture_log: _CapturingLogger,
) -> None:
    """A reply with no cadence pattern hits neither the structured log
    nor the audit file.
    """
    cfg = _make_config(tmp_path)
    linter = CadenceLinter(config=cfg)

    linter.lint(
        "Here are the three tickets in scope today: A, B, C.",
        transport="telegram",
        conv_key="telegram:dm:42",
    )

    assert capture_log.warnings == []
    assert _audit_events(cfg.audit_dir) == []


def test_lint_multiple_matches_emit_distinct_audit_lines(
    tmp_path: Path,
    capture_log: _CapturingLogger,
) -> None:
    """A reply hitting two distinct patterns emits two distinct audit
    lines (and two structured warnings), one per match. The pattern
    source on each line identifies which rule fired.
    """
    cfg = _make_config(
        tmp_path,
        patterns=[r"^Synthesizing:", r"backgrounded so\b"],
    )
    linter = CadenceLinter(config=cfg)

    text = (
        "Synthesizing: the answer.\n"
        "I have backgrounded so the work continues offline."
    )
    linter.lint(text, transport="slack", conv_key="slack:dm:U999")

    assert len(capture_log.warnings) == 2
    patterns_logged = {kw["pattern"] for _, kw in capture_log.warnings}
    assert patterns_logged == {r"^Synthesizing:", r"backgrounded so\b"}

    events = _audit_events(cfg.audit_dir)
    warn_events = [e for e in events if e["event"] == "worker.cadence_lint.warn"]
    assert len(warn_events) == 2
    audit_patterns = {e["pattern"] for e in warn_events}
    assert audit_patterns == {r"^Synthesizing:", r"backgrounded so\b"}
    # Each audit line carries the matched substring for its own pattern.
    by_pattern = {e["pattern"]: e for e in warn_events}
    assert by_pattern[r"^Synthesizing:"]["matched_substring"] == "Synthesizing:"
    assert by_pattern[r"backgrounded so\b"]["matched_substring"] == "backgrounded so"
    # Both lines share the same conv_key/transport.
    for e in warn_events:
        assert e["conv_key"] == "slack:dm:U999"
        assert e["transport"] == "slack"
