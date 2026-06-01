"""Validate the optional ``stream_subscriber`` field on :class:`InboundEvent`.

Phase 2 §5 (CLI transport) introduces a per-event streaming-output
callback that the worker awaits once per ``TextBlock`` as
``client.receive_response()`` yields. Slack and Telegram leave it unset
and keep the existing buffered finalize path; the CLI adapter wires it
to forward live text deltas to the operator's TUI.

The field's role on the dataclass is intentionally narrow: it must
round-trip on construction, and it must mirror ``completion_future``'s
treatment so attaching it does not perturb equality with a no-stream
peer. Subtask 3 (the worker consumer) is a separate commit.
"""

from __future__ import annotations

import dataclasses

from anna.transports.base import InboundEvent


def _make_event(**overrides: object) -> InboundEvent:
    base: dict[str, object] = {
        "transport": "cli",
        "conversation_key": "cli:local:funtime",
        "sender_id": "funtime",
        "sender_display": "funtime",
        "text": "hello",
        "is_dm": True,
        "is_thread": False,
    }
    base.update(overrides)
    return InboundEvent(**base)  # type: ignore[arg-type]


def test_stream_subscriber_defaults_to_none() -> None:
    event = _make_event()
    assert event.stream_subscriber is None


def test_stream_subscriber_round_trips() -> None:
    async def _sub(_text: str) -> None:
        return None

    event = _make_event(stream_subscriber=_sub)
    assert event.stream_subscriber is _sub


def test_stream_subscriber_excluded_from_compare() -> None:
    """A subscriber-bearing event must compare equal to its no-stream peer.

    Mirrors the ``completion_future`` field's contract so the router and
    transcript layers can treat the events interchangeably.
    """

    async def _sub(_text: str) -> None:
        return None

    bare = _make_event()
    with_sub = _make_event(stream_subscriber=_sub)
    assert bare == with_sub


def test_stream_subscriber_field_metadata_matches_completion_future() -> None:
    """The two optional callback fields share compare/hash/repr treatment."""

    fields = {f.name: f for f in dataclasses.fields(InboundEvent)}
    fut = fields["completion_future"]
    sub = fields["stream_subscriber"]
    assert sub.compare == fut.compare == False  # noqa: E712
    assert sub.repr == fut.repr == False  # noqa: E712
