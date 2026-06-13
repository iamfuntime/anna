"""Runtime guard against leaked, unparsed tool-call markup.

Incident: a degraded model turn posted literal
``<invoke name="Bash">…</invoke>`` XML and a bare
``mcp__anna_google__gmail_list_unread`` line to Slack. The worker now
screens every interactive send (and the scheduler-driven completion
future) through a structural markup check that drops the message,
audits the event, and fires a best-effort admin alert.

These tests pin the detection helpers (``_contains_unparsed_toolcall_markup``
and ``_matched_markers``) and the ``_guarded_send`` suppression path. The
detection helpers are pure and synchronous, so they run without any SDK
or asyncio fixtures; the ``_guarded_send`` tests use a fake send sink.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import (
    ConversationWorker,
    _contains_unparsed_toolcall_markup,
    _matched_markers,
)
from anna.transports.base import OutboundMessage


# The literal blob from the incident: an unparsed invoke block plus a bare
# whole-line MCP tool name.
INCIDENT_TEXT = (
    "Let me check your inbox.\n"
    '<invoke name="Bash">\n'
    '<parameter name="command">ls</parameter>\n'
    "</invoke>\n"
    "mcp__anna_google__gmail_list_unread\n"
)


# ---------------------------------------------------------------------------
# _contains_unparsed_toolcall_markup
# ---------------------------------------------------------------------------


def test_incident_text_detected() -> None:
    assert _contains_unparsed_toolcall_markup(INCIDENT_TEXT) is True


@pytest.mark.parametrize(
    "text",
    [
        '<invoke name="Bash">',
        '<invoke  NAME = "Read">',  # spacing + case insensitivity
        "</invoke>",
        '<parameter name="command">ls</parameter>',
        "<function_calls>",
        "mcp__anna_google__gmail_list_unread",
        "  mcp__anna_self_edit__schedule_list  ",  # leading/trailing ws on its own line
    ],
)
def test_each_strong_marker_detected(text: str) -> None:
    assert _contains_unparsed_toolcall_markup(text) is True


def test_antml_function_calls_markers_detected() -> None:
    # Built at runtime so the literal namespaced tags survive verbatim.
    ns = "antml"
    open_tag = f"<{ns}:function_calls>"
    close_tag = f"</{ns}:function_calls>"
    assert _contains_unparsed_toolcall_markup(open_tag) is True
    assert _contains_unparsed_toolcall_markup(close_tag) is True


def test_namespaced_invoke_leak_detected() -> None:
    # The live harness emits the antml:-namespaced form. Build the prefix at
    # runtime (same trick as the antml function_calls test above) so the
    # literal namespaced tag survives the editing harness verbatim.
    ns = "antml"
    leak = f'<{ns}:invoke name="Bash">'
    assert _contains_unparsed_toolcall_markup(leak) is True


def test_bare_closing_function_calls_detected() -> None:
    # The bare (non-namespaced) CLOSING tag must also be caught.
    assert _contains_unparsed_toolcall_markup("</function_calls>") is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Sure — I checked your calendar and you're free this afternoon.",
        # Backticked <invoke> WITHOUT a name= attribute: prose, not markup.
        "The SDK wraps each tool call in an `<invoke>` element.",
        "We discuss the `<invoke>` and `<function_call>` concepts in prose.",
        # Inline mcp__ tool name mid-sentence, NOT on its own line.
        "I called `mcp__anna_google__gmail_list_unread` to read your inbox.",
        "The tool mcp__anna_google__gmail_list_unread returned three threads.",
    ],
)
def test_benign_text_not_detected(text: str) -> None:
    assert _contains_unparsed_toolcall_markup(text) is False


def test_none_text_not_detected() -> None:
    # The guard is called with attribute access that can be falsy; None must
    # return False, not raise.
    assert _contains_unparsed_toolcall_markup(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _matched_markers
# ---------------------------------------------------------------------------


def test_matched_markers_for_incident_text() -> None:
    markers = _matched_markers(INCIDENT_TEXT)
    assert r"<(?:antml:)?invoke\b[^>]*\bname\s*=" in markers
    assert r"</(?:antml:)?invoke>" in markers
    assert r"<(?:antml:)?parameter\b[^>]*\bname\s*=" in markers
    assert r"(?m)^\s*mcp__[a-z0-9_]+__[a-z0-9_]+\s*$" in markers


def test_matched_markers_single_pattern() -> None:
    assert _matched_markers("</invoke>") == [r"</(?:antml:)?invoke>"]


def test_matched_markers_empty_for_benign() -> None:
    assert _matched_markers("All clear, nothing to report.") == []


# ---------------------------------------------------------------------------
# _guarded_send: drops on markup, sends when clean
# ---------------------------------------------------------------------------


def _make_worker(tmp_path: Path, sent: list[OutboundMessage]) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    return ConversationWorker(
        conversation_key="slack:ch:C123:1700000000.0001",
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_send,
    )


@pytest.mark.asyncio
async def test_guarded_send_drops_markup(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text=INCIDENT_TEXT,
        )
    )

    # The underlying send sink never saw the leaked markup.
    assert sent == []


@pytest.mark.asyncio
async def test_guarded_send_passes_clean_text(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text="Here is your morning brief.",
        )
    )

    assert len(sent) == 1
    assert sent[0].text == "Here is your morning brief."


@pytest.mark.asyncio
async def test_guarded_send_fires_admin_alert_on_suppression(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    warned: list[str] = []

    class _FakeAlerter:
        async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
            warned.append(message)
            return True

    worker._alerter = _FakeAlerter()  # type: ignore[assignment]

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text=INCIDENT_TEXT,
        )
    )

    # The alert is now dispatched via asyncio.create_task so it does not block
    # the turn; yield to the loop so the scheduled task runs before we assert.
    await asyncio.sleep(0)

    assert sent == []
    assert len(warned) == 1
    assert "leaked tool-call markup" in warned[0]


@pytest.mark.asyncio
async def test_guarded_send_survives_failing_alerter(tmp_path: Path) -> None:
    """A raising alerter must not propagate out of the guard."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    class _BoomAlerter:
        async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
            raise RuntimeError("alerter down")

    worker._alerter = _BoomAlerter()  # type: ignore[assignment]

    # Should NOT raise — the alert is fire-and-forget with its own try/except,
    # so the raising warn is swallowed inside the scheduled task.
    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text=INCIDENT_TEXT,
        )
    )
    # Let the scheduled alert task run so its inner try/except handles the
    # raise (and no unretrieved-task-exception warning is produced).
    await asyncio.sleep(0)
    assert sent == []
