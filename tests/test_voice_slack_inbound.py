"""Slack inbound voice detection + download (Pass 2, subtask 5).

A Slack voice note arrives as a ``files[]`` entry on the message event
with ``mode == "audio"`` / ``mimetype == "audio/webm"``. The adapter
downloads it (bot-token-authed CDN GET), transcribes via the
VoiceProcessor, and substitutes the transcript into ``InboundEvent.text``
prefixed by the ``[voice transcript]:`` marker.

The five cases pinned in the plan:

* files[] audio payload -> transcript substitution (mock VoiceProcessor);
* a non-audio file attachment is ignored (text passes through);
* voice=None passes the event through unchanged;
* a transcribe failure yields a polite operator-facing error text;
* mark_voice_inbound is called after a successful transcribe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from anna.config import AnnaConfig
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeVoice:
    """Scriptable stand-in for VoiceProcessor.

    ``transcript`` is returned by ``transcribe_inbound``; if it is an
    Exception it is raised instead. ``mark_calls`` records the conv_keys
    passed to ``mark_voice_inbound``.
    """

    def __init__(self, transcript: object) -> None:
        self._transcript = transcript
        self.transcribe_calls: list[dict[str, Any]] = []
        self.mark_calls: list[str] = []
        self.clear_calls: list[str] = []

    async def transcribe_inbound(self, **kwargs: Any) -> str:
        self.transcribe_calls.append(kwargs)
        if isinstance(self._transcript, Exception):
            raise self._transcript
        assert isinstance(self._transcript, str)
        return self._transcript

    def mark_voice_inbound(self, *, conv_key: str) -> None:
        self.mark_calls.append(conv_key)

    def clear_voice_inbound(self, *, conv_key: str) -> None:
        self.clear_calls.append(conv_key)


def _make_adapter(
    tmp_path: Path,
    *,
    voice: _FakeVoice | None = None,
    inbound_enabled: bool = True,
) -> SlackAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.voice.inbound.enabled = inbound_enabled
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    return SlackAdapter(config=cfg, thread_participation=tp, voice=voice)


def _voice_event(
    *,
    channel: str = "D123",
    ts: str = "1716832500.000300",
) -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": channel,
        "user": "U_OP",
        "ts": ts,
        "text": "",
        "files": [
            {
                "id": "F123",
                "mode": "audio",
                "mimetype": "audio/webm",
                "filetype": "webm",
                "url_private_download": "https://files.slack.com/F123/download",
            }
        ],
    }


def _patch_download(
    monkeypatch: pytest.MonkeyPatch, *, content: bytes = b"\x00\x01\x02"
) -> None:
    """Patch httpx.AsyncClient so the CDN GET returns fake audio bytes."""

    class _FakeResponse:
        def __init__(self) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.requests: list[dict[str, Any]] = []

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_audio_payload_substitutes_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch)
    voice = _FakeVoice("hello from a slack voice note")
    adapter = _make_adapter(tmp_path, voice=voice)

    inbound = await adapter._to_inbound_event(_voice_event())

    assert inbound.text == "[voice transcript]: hello from a slack voice note"
    assert len(voice.transcribe_calls) == 1
    # The downloaded path was handed to the VoiceProcessor.
    call = voice.transcribe_calls[0]
    assert call["mime_type"] == "audio/webm"
    assert Path(call["audio_path"]).exists()


async def test_non_audio_file_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch)
    voice = _FakeVoice("should not be used")
    adapter = _make_adapter(tmp_path, voice=voice)

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "user": "U_OP",
        "ts": "1716832500.000300",
        "text": "here is a screenshot",
        "files": [
            {
                "id": "F999",
                "mode": "hosted",
                "mimetype": "image/png",
                "filetype": "png",
                "url_private_download": "https://files.slack.com/F999/download",
            }
        ],
    }

    inbound = await adapter._to_inbound_event(event)

    assert inbound.text == "here is a screenshot"
    assert voice.transcribe_calls == []
    assert voice.mark_calls == []
    # A non-voice (text) inbound clears any prior voice mark for this conv_key.
    assert voice.clear_calls == [inbound.conversation_key]


async def test_voice_none_passes_through_unchanged(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, voice=None)

    inbound = await adapter._to_inbound_event(_voice_event())

    # No VoiceProcessor wired -> polite "voice is off" reply, no crash.
    assert inbound.text == (
        "[voice transcript]: (voice transcription is off — please type "
        "your message instead)"
    )


async def test_transcribe_failure_yields_polite_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch)
    voice = _FakeVoice(RuntimeError("whisper exploded"))
    adapter = _make_adapter(tmp_path, voice=voice)

    inbound = await adapter._to_inbound_event(_voice_event())

    assert "couldn't transcribe" in inbound.text
    assert inbound.text.startswith("[voice transcript]:")
    # Failed transcribe must not mark voice-inbound for the outbound path.
    assert voice.mark_calls == []


async def test_mark_voice_inbound_called_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch)
    voice = _FakeVoice("noted")
    adapter = _make_adapter(tmp_path, voice=voice)

    inbound = await adapter._to_inbound_event(_voice_event())

    assert voice.mark_calls == [inbound.conversation_key]
    # The audio path + mime are stashed on raw for the outbound TTS path.
    assert inbound.raw.get("voice_mime_type") == "audio/webm"
    assert "voice_audio_path" in inbound.raw


async def test_file_share_subtype_reaches_voice_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a Slack voice note arrives as a ``message`` event with
    ``subtype == "file_share"``. The ``_handle_message_event`` filter must
    NOT drop it on the subtype guard — otherwise it never reaches voice
    detection and the operator's voice note is silently swallowed (the live
    bug behind file F0B8UC8KP96)."""
    _patch_download(monkeypatch)
    voice = _FakeVoice("transcribed via the real filter path")
    adapter = _make_adapter(tmp_path, voice=voice)

    captured: list[Any] = []

    async def _handler(inbound: Any) -> None:
        captured.append(inbound)

    adapter.subscribe(_handler)

    event = _voice_event()
    event["subtype"] = "file_share"
    await adapter._handle_message_event(event, body={})

    assert len(captured) == 1
    assert captured[0].text == "[voice transcript]: transcribed via the real filter path"
    assert voice.mark_calls == [captured[0].conversation_key]


async def test_message_changed_subtype_still_dropped(tmp_path: Path) -> None:
    """The guard must still drop edit/delete echoes — only ``file_share``
    is whitelisted."""
    voice = _FakeVoice("should never run")
    adapter = _make_adapter(tmp_path, voice=voice)

    captured: list[Any] = []

    async def _handler(inbound: Any) -> None:
        captured.append(inbound)

    adapter.subscribe(_handler)

    event = _voice_event()
    event["subtype"] = "message_changed"
    await adapter._handle_message_event(event, body={})

    assert captured == []
    assert voice.transcribe_calls == []
