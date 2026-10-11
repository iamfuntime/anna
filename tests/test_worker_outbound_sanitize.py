"""Code-side outbound text filters on the worker's send paths.

Three leaks reached Slack inside 48 hours, all of them behaviors the prompt
layer had already been told to avoid. Prompt rules are advisory; these tests
pin the structural replacements:

  1. 2026-09-16 DELEGATE FOOTER LEAK. A sub-agent's MLD-lite footer
     (MISTAKES / FRICTION / WISHES) shipped to Slack attached to the whole
     delegate report body, instead of ANNA relaying the result in her own
     words. ``strip_delegate_footer`` cuts from the first footer marker to end
     of text — but ONLY when two distinct markers are present, so ordinary
     prose that happens to use one of those words is never truncated.
  2. 2026-09-15 QUIET-SENTINEL LEAK. ``[[ANNA_NO_OUTPUT]]`` is a scheduler
     protocol token stripped only by the scheduler wrapper; on the interactive
     path it reached Seth's DM as 18 literal characters three times.
     ``strip_quiet_sentinel`` removes it everywhere, and a message that was
     nothing but the sentinel is DROPPED (no placeholder) with one audit row.
  3. 2026-09-16 UNCAPPED TERMINAL REPORT. A notification-terminal report went
     out at ~4.5k characters, crossed the Slack transport's own
     ``SLACK_MAX_CHARS = 3900`` split, and rendered as two messages with a
     list item orphaned at the top of the second. The notification-terminal
     path now character-caps; the interactive path deliberately does not.

The filters are pure module-level functions, so they test without an SDK, a
worker or an event loop. The send-path tests reuse the fake-send-sink harness
from tests/test_worker_markup_guard.py and the audit-log reader from
tests/test_worker_notification_guard.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anna.config import AnnaConfig

# The sentinel literal is imported from its canonical home rather than
# re-typed, so this file can never become a fourth independent copy of it.
from anna.runtime.scheduler import QUIET_SENTINEL
from anna.runtime.supervisor import Supervisor
from anna.runtime.visibility import NULL_VISIBILITY
from anna.runtime.worker import (
    ConversationWorker,
    _NotificationOnlyTurn,
    _truncate_notification_terminal,
    strip_delegate_footer,
    strip_quiet_sentinel,
)
from anna.transports.base import OutboundMessage

CONV_KEY = "slack:channel:dm:U123"

SANITIZED_EVENT = "audit.reply.outbound_sanitized"


# The 2026-09-16 shape: a relayed delegate body with the footer attached under
# a horizontal rule.
DELEGATE_REPORT = (
    "The audit finished. Three schedules had stale cron expressions and I "
    "fixed all three.\n"
    "\n"
    "---\n"
    "*MISTAKES* — I initially assumed the store was keyed by name, not id.\n"
    "*FRICTION* — schedule_list does not surface the raw cron string.\n"
    "*WISHES* — a dry-run mode for schedule_update.\n"
)

RELAYED_BODY = (
    "The audit finished. Three schedules had stale cron expressions and I "
    "fixed all three."
)


# ---------------------------------------------------------------------------
# strip_delegate_footer
# ---------------------------------------------------------------------------


def test_two_marker_footer_stripped() -> None:
    assert strip_delegate_footer(DELEGATE_REPORT) == RELAYED_BODY


def test_footer_preceded_by_rule_drops_the_rule_too() -> None:
    """The ``---`` belongs to the footer it introduces: leaving it behind would
    end every relayed delegate result on a dangling horizontal rule."""
    out = strip_delegate_footer(DELEGATE_REPORT)
    assert "---" not in out
    assert not out.endswith("\n")


@pytest.mark.parametrize(
    "footer",
    [
        "*MISTAKES* — a\n*FRICTION* — b",
        "**FRICTION** - a\n**WISHES** - b",
        "MISTAKES: a\nWISHES: b",
        "WISHES — a\nMISTAKES — b",
        "## MISTAKES\nnone\n## FRICTION\nnone",
        "_MISTAKES_\nnone\n_WISHES_\nnone",
        "  MISTAKES: indented\n  FRICTION: also indented",
    ],
)
def test_every_footer_shape_recognized(footer: str) -> None:
    """The footer is authored by several sub-agents and its shape varies, so
    the marker is matched structurally rather than by literal string."""
    assert strip_delegate_footer(f"Body text here.\n\n{footer}") == "Body text here."


@pytest.mark.parametrize(
    "text",
    [
        # THE false-positive case: one marker-shaped line is prose.
        "*MISTAKES* happen, and this one was mine.",
        "A single line mentioning WISHES: I wish the API were nicer.",
        "MISTAKES: I mislabeled the branch. Fixed now.",
        "**FRICTION** - the only thing slowing us down is the lockfile.",
        # Lowercase is prose, never a footer marker.
        "mistakes: none\nfriction: none\nwishes: none",
        # Marker word inline rather than line-anchored.
        "Two things: MISTAKES and WISHES are both worth writing down.",
    ],
)
def test_single_marker_prose_not_stripped(text: str) -> None:
    assert strip_delegate_footer(text) == text


@pytest.mark.parametrize("text", ["", "   ", None])
def test_footer_strip_never_raises_on_empty(text: str) -> None:
    assert strip_delegate_footer(text) == (text or "")


def test_footer_strip_cuts_from_the_first_marker() -> None:
    """Cut point is the FIRST marker, not the last: everything from there on is
    footer, including any prose a sub-agent interleaved between markers."""
    text = "Report.\nMISTAKES: one\nsome interleaved note\nFRICTION: two"
    assert strip_delegate_footer(text) == "Report."


# ---------------------------------------------------------------------------
# strip_quiet_sentinel
# ---------------------------------------------------------------------------


def test_sentinel_alone_becomes_empty() -> None:
    assert strip_quiet_sentinel(QUIET_SENTINEL) == ""


@pytest.mark.parametrize(
    "text",
    [
        QUIET_SENTINEL,
        f"  {QUIET_SENTINEL}  ",
        f"\n{QUIET_SENTINEL}\n",
        f"\n\n  {QUIET_SENTINEL}\n\n",
    ],
)
def test_sentinel_with_surrounding_whitespace_becomes_empty(text: str) -> None:
    assert strip_quiet_sentinel(text) == ""


def test_sentinel_on_its_own_line_leaves_no_blank_line() -> None:
    text = f"Deploy finished.\n{QUIET_SENTINEL}\nAll checks green."
    assert strip_quiet_sentinel(text) == "Deploy finished.\nAll checks green."


def test_sentinel_inline_does_not_join_words() -> None:
    text = f"Deploy finished. {QUIET_SENTINEL} All checks green."
    assert strip_quiet_sentinel(text) == "Deploy finished. All checks green."


def test_every_occurrence_removed() -> None:
    text = f"{QUIET_SENTINEL}\nreal prose\n{QUIET_SENTINEL}\nmore prose\n{QUIET_SENTINEL}"
    out = strip_quiet_sentinel(text)
    assert QUIET_SENTINEL not in out
    assert out == "real prose\nmore prose"


def test_sentinel_absent_returns_text_identically() -> None:
    text = "Nothing to strip here."
    assert strip_quiet_sentinel(text) is text


@pytest.mark.parametrize("text", ["", "   ", None])
def test_sentinel_strip_never_raises_on_empty(text: str) -> None:
    assert strip_quiet_sentinel(text) == (text or "")


def test_single_literal_source() -> None:
    """The literal must exist in exactly one place. The worker reads the
    scheduler's canonical constant (lazily, because scheduler imports worker
    and the reverse module-level import would be a cycle)."""
    from anna.runtime.worker import _quiet_sentinel_patterns

    assert _quiet_sentinel_patterns()[0] == QUIET_SENTINEL


# ---------------------------------------------------------------------------
# _truncate_notification_terminal
# ---------------------------------------------------------------------------

MARKER = " […]"


def test_under_the_cap_is_untouched() -> None:
    text = "Short report.\nTwo lines."
    assert _truncate_notification_terminal(text, 1200) is text


def test_exactly_at_the_cap_is_untouched() -> None:
    text = "x" * 100
    assert _truncate_notification_terminal(text, 100) is text


def test_cap_zero_means_no_truncation() -> None:
    """``0`` is the documented off switch: it restores pre-2026-09-16 behavior
    without a code revert."""
    text = "y" * 9000
    assert _truncate_notification_terminal(text, 0) is text


def test_over_the_cap_truncates_on_a_newline_with_the_marker() -> None:
    lines = [f"- item {i:03d} in the report" for i in range(60)]
    text = "\n".join(lines)
    out = _truncate_notification_terminal(text, 400)
    assert out.endswith(MARKER)
    assert len(out) <= 400
    # Cut landed on a line boundary: the last surviving item is whole.
    body = out[: -len(MARKER)]
    assert body.splitlines()[-1] in lines


def test_over_the_cap_truncates_on_a_sentence_when_there_is_no_newline() -> None:
    text = ("The daemon restarted cleanly. " * 40).strip()
    out = _truncate_notification_terminal(text, 300)
    assert out.endswith(MARKER)
    assert len(out) <= 300
    assert out[: -len(MARKER)].endswith("cleanly.")


def test_never_cuts_mid_word() -> None:
    text = " ".join(["alpha", "bravo", "charlie", "delta", "echo"] * 40)
    out = _truncate_notification_terminal(text, 120)
    assert out.endswith(MARKER)
    assert len(out) <= 120
    words = out[: -len(MARKER)].split()
    assert words[-1] in {"alpha", "bravo", "charlie", "delta", "echo"}


def test_single_marker_only_once() -> None:
    out = _truncate_notification_terminal("z" * 500 + " tail " * 50, 200)
    assert out.count("[…]") == 1


def test_unbroken_token_is_hard_sliced() -> None:
    """No boundary anywhere to honor. Slicing beats refusing to cap, because
    the cap is what keeps the transport from splitting the message."""
    out = _truncate_notification_terminal("q" * 500, 100)
    assert len(out) == 100
    assert out.endswith(MARKER)


# ---------------------------------------------------------------------------
# Harness: fake send sink + audit reader
# ---------------------------------------------------------------------------


def _make_worker(
    tmp_path: Path,
    sent: list[OutboundMessage],
    *,
    configure: object = None,
) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    # Applied BEFORE construction: the worker caches these flags in __init__
    # (the no-hot-reload contract), so mutating the config afterwards would
    # have no effect and the test would silently prove nothing.
    if configure is not None:
        configure(cfg)  # type: ignore[operator]
    supervisor = Supervisor(config=cfg)

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_send,
        visibility=NULL_VISIBILITY,
    )


def _audit_records(cfg: AnnaConfig) -> list[dict]:
    out: list[dict] = []
    audit_dir = cfg.audit_dir
    if not audit_dir.exists():
        return out
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _sanitized_rows(cfg: AnnaConfig) -> list[dict]:
    return [r for r in _audit_records(cfg) if r.get("event") == SANITIZED_EVENT]


# ---------------------------------------------------------------------------
# _guarded_send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_send_drops_a_bare_sentinel_and_audits_once(tmp_path: Path) -> None:
    """The 2026-09-15 DM leak. Non-empty before, empty after → no send at all,
    and no ``(no response)`` placeholder in its place."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(conversation_key=CONV_KEY, text=QUIET_SENTINEL)
    )

    assert sent == []
    rows = _sanitized_rows(worker._config)
    assert len(rows) == 1
    assert rows[0]["filters"] == ["quiet_sentinel"]
    assert rows[0]["before_chars"] == len(QUIET_SENTINEL)
    assert rows[0]["after_chars"] == 0
    assert rows[0]["transport"] == "slack"


@pytest.mark.asyncio
async def test_guarded_send_strips_sentinel_and_delivers_the_prose(tmp_path: Path) -> None:
    """The important half: the sentinel is removed but the real reply around it
    still ships. Over-strict handling here would silence ANNA mid-answer."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=CONV_KEY,
            text=f"Deploy finished.\n{QUIET_SENTINEL}\nAll checks green.",
        )
    )

    assert len(sent) == 1
    assert sent[0].text == "Deploy finished.\nAll checks green."
    assert QUIET_SENTINEL not in sent[0].text
    # Text survived, so this is not a dropped send: no audit row is owed.
    assert _sanitized_rows(worker._config) == []


@pytest.mark.asyncio
async def test_guarded_send_strips_the_delegate_footer(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(conversation_key=CONV_KEY, text=DELEGATE_REPORT)
    )

    assert len(sent) == 1
    assert sent[0].text == RELAYED_BODY
    for word in ("MISTAKES", "FRICTION", "WISHES"):
        assert word not in sent[0].text


@pytest.mark.asyncio
async def test_guarded_send_does_not_mutate_the_callers_message(tmp_path: Path) -> None:
    """``OutboundMessage`` is shared with the transports, so the cleaned text is
    a dataclasses.replace copy, never an in-place write."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    msg = OutboundMessage(
        conversation_key=CONV_KEY,
        text=f"Done. {QUIET_SENTINEL}",
    )

    await worker._guarded_send(msg)

    assert msg.text == f"Done. {QUIET_SENTINEL}"
    assert sent[0].text == "Done."
    assert sent[0] is not msg


@pytest.mark.asyncio
async def test_guarded_send_applies_no_character_cap(tmp_path: Path) -> None:
    """Interactive reply length is deliberately uncapped: a long answer to a
    long question is the product working, not a defect. The 2026-09-16 cap is
    scoped to the notification-terminal path alone."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    long_reply = "Here is the full analysis. " * 400  # ~10.8k chars

    await worker._guarded_send(
        OutboundMessage(conversation_key=CONV_KEY, text=long_reply)
    )

    assert len(sent) == 1
    assert sent[0].text == long_reply
    assert "[…]" not in sent[0].text


@pytest.mark.asyncio
async def test_guarded_send_leaves_clean_text_identical(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    text = "Nothing to sanitize. MISTAKES happen, that is all."

    await worker._guarded_send(OutboundMessage(conversation_key=CONV_KEY, text=text))

    assert [m.text for m in sent] == [text]
    assert _sanitized_rows(worker._config) == []


# ---------------------------------------------------------------------------
# _decide_notification_terminal: sanitation + character cap
# ---------------------------------------------------------------------------


def _ledger(terminal: str) -> _NotificationOnlyTurn:
    ledger = _NotificationOnlyTurn(turn_id="deadbeef")
    ledger.note_source("background_delegation")
    ledger.note_terminal_text(terminal)
    return ledger


def test_terminal_report_over_the_cap_is_truncated_with_the_marker(
    tmp_path: Path,
) -> None:
    """The 2026-09-16 incident shape: an over-long delegate report crossing the
    Slack transport's own SLACK_MAX_CHARS split point."""
    sent: list[OutboundMessage] = []

    def _cap_at_300(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.notification_terminal_max_chars = 300

    worker = _make_worker(tmp_path, sent, configure=_cap_at_300)
    lines = [f"- finding {i:03d}: the schedule was stale" for i in range(40)]
    ledger = _ledger("\n".join(lines))

    assert worker._decide_notification_terminal(
        ledger, ledger.terminal_text, conv_key=CONV_KEY
    )
    # The ledger carries the POST-filter text, so the audit row and the send
    # cannot disagree about what shipped.
    assert ledger.delivery_outcome == "delivered"
    assert len(ledger.delivered_text) <= 300
    assert ledger.delivered_text.endswith(" […]")
    assert ledger.delivered_text.splitlines()[-1].rstrip(" […]") in "\n".join(lines)


def test_terminal_report_under_the_cap_is_untouched(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    report = "Research finished. Two of the three sources agree."
    ledger = _ledger(report)

    assert worker._decide_notification_terminal(
        ledger, ledger.terminal_text, conv_key=CONV_KEY
    )
    assert ledger.delivered_text == report


def test_terminal_report_cap_zero_means_no_truncation(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []

    def _no_cap(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.notification_terminal_max_chars = 0

    worker = _make_worker(tmp_path, sent, configure=_no_cap)
    report = "long line of findings. " * 400  # ~9.2k chars
    ledger = _ledger(report)

    assert worker._decide_notification_terminal(
        ledger, ledger.terminal_text, conv_key=CONV_KEY
    )
    assert ledger.delivered_text == report.strip()
    assert "[…]" not in ledger.delivered_text


def test_terminal_report_footer_stripped_before_the_cap(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    ledger = _ledger(DELEGATE_REPORT)

    assert worker._decide_notification_terminal(
        ledger, ledger.terminal_text, conv_key=CONV_KEY
    )
    assert ledger.delivered_text == RELAYED_BODY


def test_terminal_report_that_sanitizes_to_nothing_is_empty(tmp_path: Path) -> None:
    """A report that was only a quiet sentinel records the EXISTING ``empty``
    outcome and returns False — the same silence a turn that ended on a tool
    call gets, rather than a new outcome name the audit reader must learn."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    ledger = _ledger(QUIET_SENTINEL)

    assert (
        worker._decide_notification_terminal(
            ledger, ledger.terminal_text, conv_key=CONV_KEY
        )
        is False
    )
    assert ledger.delivery_outcome == "empty"
    assert ledger.delivered_text == ""
    # And it did NOT burn one of the flood window's delivery slots.
    assert worker._notification_terminal_sends == []


# ---------------------------------------------------------------------------
# Config field
# ---------------------------------------------------------------------------


def test_config_default_cap_is_under_the_slack_split() -> None:
    from anna.transports.base import SLACK_MAX_CHARS

    cfg = AnnaConfig()
    assert cfg.runtime.visibility.notification_terminal_max_chars == 1200
    assert cfg.runtime.visibility.notification_terminal_max_chars < SLACK_MAX_CHARS


def test_config_rejects_a_negative_cap() -> None:
    """A negative value would read as "truncate to nothing" in the worker's
    slice arithmetic — exactly the silent report loss the cap exists to make
    visible — so it fails at load instead."""
    with pytest.raises(ValueError, match="notification_terminal_max_chars"):
        AnnaConfig.model_validate(
            {"runtime": {"visibility": {"notification_terminal_max_chars": -1}}}
        )


def test_worker_caches_the_cap_at_construction(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []

    def _cap_at_777(cfg: AnnaConfig) -> None:
        cfg.runtime.visibility.notification_terminal_max_chars = 777

    worker = _make_worker(tmp_path, sent, configure=_cap_at_777)
    assert worker._notification_terminal_max_chars == 777
