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

from dataclasses import dataclass
from typing import Any

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import (
    ConversationWorker,
    _contains_unparsed_toolcall_markup,
    _markup_is_strong,
    _matched_markers,
    _should_suppress_markup,
)
from anna.transports.base import InboundEvent, OutboundMessage


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


@pytest.mark.parametrize(
    "text",
    [
        # The exact false-positive shape that caused the admin-channel cascade:
        # a reply EXPLAINING the suppression, quoting the markup in backticks.
        'a reply containing raw tool-call markup (the literal `<invoke name=...>` '
        "XML that should execute a tool, not get posted).",
        'The guard matched `<invoke name="Bash">` and `</invoke>` in my draft.',
        "It also flags a `<parameter name=\"command\">` fragment.",
        # Fenced block quoting the whole incident blob is prose, not a leak.
        "Here is what leaked:\n```\n<invoke name=\"Bash\">\n</invoke>\n```\nThat is why it dropped.",
        # Backticked whole-line mcp tool name is a quote, not a bare leak.
        "The bare line `mcp__anna_google__gmail_list_unread` is what tripped it.",
    ],
)
def test_quoted_markup_not_detected(text: str) -> None:
    # Markup wrapped in inline/fenced code spans is prose ABOUT the syntax,
    # never a genuine leak (those arrive bare). Must NOT be suppressed.
    assert _contains_unparsed_toolcall_markup(text) is False
    assert _matched_markers(text) == []


def test_bare_leak_still_detected_alongside_quoted() -> None:
    # A bare leak must still fire even if the same text also quotes markup.
    text = 'I quote `<invoke name="X">` here, but this is bare:\n<invoke name="Bash">'
    assert _contains_unparsed_toolcall_markup(text) is True


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
# _markup_is_strong / _should_suppress_markup: marker strength + tool gate
# ---------------------------------------------------------------------------


def test_lone_opening_invoke_is_weak() -> None:
    # A single opening-invoke tag, no closing tag, no parameter tag → WEAK.
    assert _contains_unparsed_toolcall_markup('Sure.\n<invoke name="Bash">') is True
    assert _markup_is_strong('Sure.\n<invoke name="Bash">') is False


@pytest.mark.parametrize(
    "text",
    [
        INCIDENT_TEXT,  # invoke + parameter + close + bare mcp line
        # Two distinct markers: opening invoke AND a parameter tag.
        '<invoke name="Bash">\n<parameter name="command">ls</parameter>',
        # Paired open + close is two distinct markers.
        '<invoke name="Bash"></invoke>',
        # A single self-sufficient marker (closing tag) with no invoke open.
        "Done.\n</invoke>",
        # A single self-sufficient marker (bare whole-line mcp tool name).
        "Checking.\nmcp__anna_google__gmail_list_unread",
    ],
)
def test_strong_markup_detected(text: str) -> None:
    assert _markup_is_strong(text) is True


@pytest.mark.parametrize("text", ["", "   ", "All clear.", None])
def test_no_markup_is_not_strong(text) -> None:
    assert _markup_is_strong(text) is False


def test_should_suppress_strong_markup_regardless_of_tool() -> None:
    # Behavior 1: full leaked markup as prose is suppressed whether or not a
    # tool ran (the heartbeat / INCIDENT_TEXT case).
    assert _should_suppress_markup(INCIDENT_TEXT, tool_used=False) is True
    assert _should_suppress_markup(INCIDENT_TEXT, tool_used=True) is True


def test_should_suppress_weak_markup_only_without_tool() -> None:
    # Behavior 2 vs 3: a lone partial fragment is delivered when a real tool
    # call executed, but suppressed when no tool ran (prose-instead-of-call).
    assert _should_suppress_markup(WEAK_TRAILING_REPLY, tool_used=True) is False
    assert _should_suppress_markup(WEAK_TRAILING_REPLY, tool_used=False) is True


def test_should_suppress_clean_text_never() -> None:
    assert _should_suppress_markup(CLEAN_REPLY, tool_used=False) is False
    assert _should_suppress_markup(CLEAN_REPLY, tool_used=True) is False


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
async def test_guarded_send_delivers_weak_fragment_when_tool_used(tmp_path: Path) -> None:
    # Behavior 2 at the send layer: a weak trailing fragment on a turn that
    # executed a real tool is delivered, not suppressed.
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text=WEAK_TRAILING_REPLY,
        ),
        tool_used=True,
    )

    assert len(sent) == 1
    assert sent[0].text == WEAK_TRAILING_REPLY


@pytest.mark.asyncio
async def test_guarded_send_drops_weak_fragment_without_tool(tmp_path: Path) -> None:
    # Behavior 3 at the send layer: the SAME weak fragment is suppressed when no
    # tool ran (default tool_used=False), preserving the prose-instead-of-call
    # protection.
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text=WEAK_TRAILING_REPLY,
        )
    )

    assert sent == []


@pytest.mark.asyncio
async def test_guarded_send_drops_strong_markup_even_when_tool_used(tmp_path: Path) -> None:
    # Behavior 1 at the send layer: strong leaked markup is dropped even on a
    # turn that executed a real tool.
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    await worker._guarded_send(
        OutboundMessage(
            conversation_key=worker.conversation_key,
            text=INCIDENT_TEXT,
        ),
        tool_used=True,
    )

    assert sent == []


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


@pytest.mark.asyncio
async def test_no_alert_when_suppression_in_admin_channel(tmp_path: Path) -> None:
    """The feedback-loop break: a suppression that happened in the admin
    channel must NOT fire another admin alert into that same channel."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    # Point the admin destination at the worker's own channel (C123).
    worker._config.admin.slack_channel_id = "C123"

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
    await asyncio.sleep(0)

    # Suppressed (fail-closed preserved) but NO alert — loop broken.
    assert sent == []
    assert warned == []


@pytest.mark.asyncio
async def test_alert_still_fires_outside_admin_channel(tmp_path: Path) -> None:
    """A suppression in a non-admin channel still alerts the admin channel."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)

    # Admin destination is a DIFFERENT channel than the worker's (C123).
    worker._config.admin.slack_channel_id = "CADMIN999"

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
    await asyncio.sleep(0)

    assert sent == []
    assert len(warned) == 1


# ---------------------------------------------------------------------------
# Bounded scheduled-turn regeneration (completion-future path only).
#
# A degraded scheduled generation can NARRATE its tool calls as literal markup
# instead of executing them — zero tools run, so the failed turn has no side
# effects and one fresh re-generation is safe and almost always succeeds. The
# worker makes AT MOST one retry before falling back to today's suppress +
# QUIET_SENTINEL behavior. These tests drive ``_handle`` through a fake SDK
# session that can return a different reply on the second query/receive cycle.
# ---------------------------------------------------------------------------


# A reply carrying a bare (unparsed) invoke leak — markup, not prose.
DIRTY_REPLY = (
    "Let me check your inbox.\n"
    '<invoke name="Bash">\n'
    '<parameter name="command">ls</parameter>\n'
    "</invoke>"
)
# A SECOND, DISTINCT dirty reply (a bare whole-line MCP tool name leak) so a
# both-dirty test can prove WHICH reply text gets suppressed.
DIRTY_REPLY_2 = (
    "Checking your inbox now.\n"
    "mcp__anna_google__gmail_list_unread"
)
# A WEAK leak: otherwise-good prose carrying a SINGLE stray/partial opening
# invoke fragment — no closing tag, no parameter tag, no second distinct
# marker. On a turn that executed a real tool this is delivered, not eaten.
WEAK_TRAILING_REPLY = (
    "Morning brief: two threads need a reply, calendar is clear.\n"
    '<invoke name="Bash">'
)
# A contract-legal compact check-in with no tool-call markup.
CLEAN_REPLY = "Morning brief: two threads need a reply, calendar is clear."


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    """A real tool invocation block. Its mere presence in the drain loop sets
    the worker's ``tool_used`` flag, which gates off regeneration."""

    name: str = "Bash"
    input: dict[str, Any] | None = None


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


@pytest.fixture
def _patch_sdk_types(monkeypatch):
    """Point the worker's lazily-imported SDK message types at local fakes so
    the (re)drain loops in ``_handle`` / ``_regenerate_scheduled_reply``
    recognise our fake AssistantMessage/TextBlock/ResultMessage shapes."""
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", _FakeToolUseBlock, raising=False)
    yield


class _FakeSequenceClient:
    """Fake SDK session that returns a different reply per query/receive cycle.

    ``replies`` is indexed by the number of ``receive_response`` calls so the
    first turn yields ``replies[0]`` and the regeneration yields ``replies[1]``
    (the last entry repeats if fewer replies than cycles are supplied). Set
    ``raise_on_query`` / ``raise_on_receive`` to a 1-based call index to force
    the regeneration's query/receive to blow up.
    """

    def __init__(
        self,
        replies: list[str],
        *,
        raise_on_query: int | None = None,
        raise_on_receive: int | None = None,
        first_uses_tool: bool = False,
    ) -> None:
        self._replies = list(replies)
        self.queries: list[str] = []
        self._receive_calls = 0
        self._raise_on_query = raise_on_query
        self._raise_on_receive = raise_on_receive
        # When set, the FIRST generation's AssistantMessage carries a real
        # ToolUseBlock alongside its (leaked-markup) text — i.e. a tool truly
        # executed this turn, so regeneration must be refused.
        self._first_uses_tool = first_uses_tool

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self._raise_on_query is not None and len(self.queries) == self._raise_on_query:
            raise RuntimeError("regen query boom")

    async def receive_response(self):
        self._receive_calls += 1
        if self._raise_on_receive is not None and self._receive_calls == self._raise_on_receive:
            raise RuntimeError("regen receive boom")
        idx = min(self._receive_calls - 1, len(self._replies) - 1)
        content: list[Any] = []
        if self._first_uses_tool and self._receive_calls == 1:
            content.append(_FakeToolUseBlock())
        content.append(_FakeTextBlock(text=self._replies[idx]))
        yield _FakeAssistantMessage(content=content)
        yield _FakeResultMessage()

    async def __aenter__(self):  # pragma: no cover
        return self

    async def __aexit__(self, *_a):  # pragma: no cover
        return None


def _make_scheduled_event(
    conv_key: str, future: asyncio.Future[str]
) -> InboundEvent:
    return InboundEvent(
        transport="slack",
        conversation_key=conv_key,
        sender_id="anna.scheduler",
        sender_display="ANNA Scheduler",
        text="Run the heartbeat.",
        is_dm=False,
        is_thread=False,
        completion_future=future,
    )


def _capture_audit(monkeypatch) -> list[str]:
    """Record the event names passed to ``worker.audit_event`` (the worker
    imports it by name, so patch it in the worker module)."""
    names: list[str] = []
    import anna.runtime.worker as worker_mod

    real = worker_mod.audit_event

    def _spy(event: str, **kwargs):
        names.append(event)
        return real(event, **kwargs)

    monkeypatch.setattr(worker_mod, "audit_event", _spy)
    return names


def _spy_suppress(worker: ConversationWorker) -> list[str]:
    """Replace ``_emit_markup_suppressed`` with an async spy that records the
    suppressed text instead of auditing/alerting."""
    calls: list[str] = []

    async def _fake(text: str, *, conv_key: str) -> None:
        calls.append(text)

    worker._emit_markup_suppressed = _fake  # type: ignore[assignment]
    return calls


@pytest.mark.asyncio
async def test_regeneration_rescues_dirty_first_generation(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """First generation leaks markup; the single retry returns clean text →
    the future resolves with that clean text (NOT the sentinel), the
    ``regenerated`` audit event fires, and suppression is never invoked."""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeSequenceClient([DIRTY_REPLY, CLEAN_REPLY])

    audit_names = _capture_audit(monkeypatch)
    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == CLEAN_REPLY
    assert future.result() != QUIET_SENTINEL
    # Exactly one retry: the original turn + the regeneration.
    assert len(worker._client.queries) == 2
    # The rescue was recorded and the fallback suppression never ran.
    assert "audit.reply.toolcall_markup_regenerating" in audit_names
    assert "audit.reply.toolcall_markup_regenerated" in audit_names
    assert suppressed == []
    assert sent == []


@pytest.mark.asyncio
async def test_regeneration_both_dirty_falls_back_to_sentinel(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """Both generations leak markup → the future resolves with QUIET_SENTINEL,
    suppression fires exactly once (the fallback), and only ONE retry is
    attempted (query called exactly twice)."""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    # Original and regenerated dirty replies are DISTINCT so the suppression
    # assertion below actually proves WHICH text was suppressed (the original,
    # not the regenerated one).
    worker._client = _FakeSequenceClient([DIRTY_REPLY, DIRTY_REPLY_2])

    audit_names = _capture_audit(monkeypatch)
    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == QUIET_SENTINEL
    # AT MOST one retry — no runaway loop.
    assert len(worker._client.queries) == 2
    # The regenerating marker fired but the success marker did NOT.
    assert "audit.reply.toolcall_markup_regenerating" in audit_names
    assert "audit.reply.toolcall_markup_regenerated" not in audit_names
    # Fallback suppression ran exactly once, on the ORIGINAL reply — NOT the
    # (also-dirty) regenerated one.
    assert suppressed == [DIRTY_REPLY]


@pytest.mark.asyncio
async def test_clean_first_generation_skips_regeneration(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """A clean first generation resolves the future directly with no second
    query and no regeneration audit markers (existing behavior unchanged)."""
    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    worker._client = _FakeSequenceClient([CLEAN_REPLY])

    audit_names = _capture_audit(monkeypatch)
    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == CLEAN_REPLY
    # No regeneration: only the original query ran.
    assert len(worker._client.queries) == 1
    assert "audit.reply.toolcall_markup_regenerating" not in audit_names
    assert "audit.reply.toolcall_markup_regenerated" not in audit_names
    assert suppressed == []


@pytest.mark.asyncio
async def test_regeneration_query_error_falls_back_to_sentinel(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """If the regeneration's query/receive raises, the worker logs and falls
    back to suppress + QUIET_SENTINEL — the future is resolved and no
    exception escapes ``_handle``."""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    # First turn yields the dirty reply; the SECOND query (regeneration) raises.
    worker._client = _FakeSequenceClient([DIRTY_REPLY], raise_on_query=2)

    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    # Must not raise out of _handle.
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == QUIET_SENTINEL
    # The retry was attempted (query called twice) then errored out.
    assert len(worker._client.queries) == 2
    # Fallback suppressed the ORIGINAL reply.
    assert suppressed == [DIRTY_REPLY]


@pytest.mark.asyncio
async def test_regeneration_receive_error_falls_back_to_sentinel(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """If the regeneration's ``receive_response`` drain raises, the worker logs
    and falls back to suppress + QUIET_SENTINEL — the future is resolved and no
    exception escapes ``_handle`` (mirrors the query-error path, but the failure
    happens during the re-drain rather than the re-query)."""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    # First receive yields the dirty reply; the SECOND receive (regeneration's
    # drain) raises after the re-query already landed.
    worker._client = _FakeSequenceClient([DIRTY_REPLY], raise_on_receive=2)

    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    # Must not raise out of _handle.
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == QUIET_SENTINEL
    # The retry's query landed (called twice) before the drain blew up.
    assert len(worker._client.queries) == 2
    # Fallback suppressed the ORIGINAL reply.
    assert suppressed == [DIRTY_REPLY]


@pytest.mark.asyncio
async def test_executed_tool_with_weak_trailing_fragment_is_delivered(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """Behavior 2 (scheduled path): a scheduled turn that ACTUALLY executed a
    tool (ToolUseBlock) and whose prose carries only a single WEAK/partial
    markup fragment (a lone opening invoke tag) is DELIVERED — the future
    resolves with that original reply text, NOT the sentinel. No suppression,
    no regeneration, no regenerating audit marker. (Softened guard: re-running
    would double-execute the tool, and the fragment is almost always ANNA
    quoting markup in otherwise-good prose.)"""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    # First generation runs a real tool AND trails a WEAK fragment. CLEAN_REPLY
    # is queued as a "regeneration" reply that must NEVER be used.
    worker._client = _FakeSequenceClient(
        [WEAK_TRAILING_REPLY, CLEAN_REPLY], first_uses_tool=True
    )

    audit_names = _capture_audit(monkeypatch)
    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    # Delivered: the original reply text, not the sentinel.
    assert future.result() == WEAK_TRAILING_REPLY
    assert future.result() != QUIET_SENTINEL
    # No regeneration: only the original query ran.
    assert len(worker._client.queries) == 1
    # Suppression never ran and neither audit marker fired.
    assert suppressed == []
    assert "audit.reply.toolcall_markup_regenerating" not in audit_names
    assert "audit.reply.toolcall_markup_regenerated" not in audit_names


@pytest.mark.asyncio
async def test_executed_tool_with_strong_markup_is_suppressed_not_regenerated(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """Invariant preserved: a scheduled turn that ACTUALLY executed a tool AND
    leaked STRONG markup (full invoke+parameter block) must NOT be regenerated
    — re-running would double-execute that tool. It goes straight to suppress +
    QUIET_SENTINEL with no regeneration query and no regenerating audit
    marker."""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    # First generation runs a real tool AND leaks STRONG markup. A clean
    # CLEAN_REPLY is queued as the "regeneration" reply that must NEVER be used.
    worker._client = _FakeSequenceClient(
        [DIRTY_REPLY, CLEAN_REPLY], first_uses_tool=True
    )

    audit_names = _capture_audit(monkeypatch)
    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == QUIET_SENTINEL
    # Regeneration NOT attempted: only the original query ran.
    assert len(worker._client.queries) == 1
    # Fallback suppression ran exactly once, on the ORIGINAL dirty reply.
    assert suppressed == [DIRTY_REPLY]
    # No regeneration was even attempted — neither audit marker fired.
    assert "audit.reply.toolcall_markup_regenerating" not in audit_names
    assert "audit.reply.toolcall_markup_regenerated" not in audit_names


@pytest.mark.asyncio
async def test_no_tool_full_markup_still_suppressed_via_regeneration(
    tmp_path: Path, monkeypatch, _patch_sdk_types
) -> None:
    """Behavior 3 (scheduled path): a turn with NO real tool call whose prose
    is full leaked markup still enters the guard. With both generations dirty it
    falls back to suppress + QUIET_SENTINEL — protection preserved even when the
    model substituted prose for a tool call."""
    from anna.runtime.scheduler import QUIET_SENTINEL

    sent: list[OutboundMessage] = []
    worker = _make_worker(tmp_path, sent)
    # No tool ran; both generations leak strong markup.
    worker._client = _FakeSequenceClient([DIRTY_REPLY, DIRTY_REPLY_2])

    suppressed = _spy_suppress(worker)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    await worker._handle(_make_scheduled_event(worker.conversation_key, future))

    assert future.done()
    assert future.result() == QUIET_SENTINEL
    # Suppressed the ORIGINAL dirty reply.
    assert suppressed == [DIRTY_REPLY]
