"""Tests for the Phase 2.5 VoiceProcessor.transcribe_inbound path.

Covers the size/duration gates, the retry loop (transient retry,
exhausted retries, no-retry on terminal codec errors), and the
keep_audio_files=false unlink-after-call lifecycle. The transcription
provider is mocked so the tests never touch the network; the
WhisperOpenAIProvider's own httpx wiring is exercised separately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.voice import (
    TranscriptionError,
    TTSError,
    VoiceProcessor,
)


def _config(tmp_path: Path, **inbound_overrides: object) -> AnnaConfig:
    """Build an AnnaConfig rooted at tmp_path with voice.inbound overrides."""
    raw: dict[str, object] = {"voice": {"inbound": dict(inbound_overrides)}}
    cfg = AnnaConfig.model_validate(raw)
    return cfg.model_copy(update={"anna_home": tmp_path})


def _audio_file(tmp_path: Path, *, size: int = 1024, name: str = "clip.ogg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00" * size)
    return path


class _MockProvider:
    """A scriptable transcription provider.

    ``responses`` is a list consumed one entry per call. A ``str`` entry
    is returned as the transcript; an ``Exception`` entry is raised.
    """

    name = "mock-provider"

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[Path] = []

    async def transcribe(
        self,
        *,
        audio_path: Path,
        mime_type: str,
        hint_language: str | None = None,
    ) -> str:
        self.calls.append(audio_path)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, str)
        return item


def _read_audit_events(tmp_path: Path) -> list[dict[str, object]]:
    audit_dir = tmp_path / "audit"
    events: list[dict[str, object]] = []
    if not audit_dir.is_dir():
        return events
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


async def test_happy_path_returns_transcript(tmp_path: Path) -> None:
    provider = _MockProvider(["hello from a voice note"])
    proc = VoiceProcessor(
        config=_config(tmp_path),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    result = await proc.transcribe_inbound(
        audio_path=audio,
        mime_type="audio/ogg",
        conv_key="telegram:dm:42",
        message_id="m1",
        duration_seconds=3.0,
    )

    assert result == "hello from a voice note"
    assert provider.calls == [audio]
    # File persisted by default (keep_audio_files defaults true).
    assert audio.exists()
    events = _read_audit_events(tmp_path)
    names = [e["event"] for e in events]
    assert "audit.voice.inbound" in names
    transcribe = [e for e in events if e["event"] == "audit.voice.transcribe"]
    assert len(transcribe) == 1
    assert transcribe[0]["status"] == "ok"
    assert transcribe[0]["transcript_chars"] == len(result)
    assert transcribe[0]["attempt"] == 1


async def test_size_cap_rejects_before_provider_call(tmp_path: Path) -> None:
    provider = _MockProvider(["should not be called"])
    proc = VoiceProcessor(
        config=_config(tmp_path, max_audio_size_bytes=100),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path, size=500)

    with pytest.raises(TranscriptionError) as excinfo:
        await proc.transcribe_inbound(
            audio_path=audio,
            mime_type="audio/ogg",
            conv_key="telegram:dm:42",
            message_id="m1",
        )

    assert "cap" in str(excinfo.value)
    assert provider.calls == []  # gate fired before the provider call
    events = _read_audit_events(tmp_path)
    rejects = [
        e
        for e in events
        if e["event"] == "audit.voice.transcribe" and e.get("status") == "reject"
    ]
    assert rejects and rejects[0]["error"] == "size_cap"


async def test_duration_cap_rejects_before_provider_call(tmp_path: Path) -> None:
    provider = _MockProvider(["should not be called"])
    proc = VoiceProcessor(
        config=_config(tmp_path, max_duration_seconds=60),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    with pytest.raises(TranscriptionError) as excinfo:
        await proc.transcribe_inbound(
            audio_path=audio,
            mime_type="audio/ogg",
            conv_key="telegram:dm:42",
            message_id="m1",
            duration_seconds=120.0,
        )

    assert "cap" in str(excinfo.value)
    assert provider.calls == []
    events = _read_audit_events(tmp_path)
    rejects = [
        e
        for e in events
        if e["event"] == "audit.voice.transcribe" and e.get("status") == "reject"
    ]
    assert rejects and rejects[0]["error"] == "duration_cap"


async def test_transient_failure_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skip the real backoff sleeps so the test stays fast.
    monkeypatch.setattr(VoiceProcessor, "_backoff_seconds", staticmethod(lambda _a: 0.0))
    provider = _MockProvider(
        [
            TranscriptionError("upstream 503", status=503, retryable=True),
            "second attempt wins",
        ]
    )
    proc = VoiceProcessor(
        config=_config(tmp_path, retry_attempts=2),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    result = await proc.transcribe_inbound(
        audio_path=audio,
        mime_type="audio/ogg",
        conv_key="telegram:dm:42",
        message_id="m1",
    )

    assert result == "second attempt wins"
    assert len(provider.calls) == 2
    events = _read_audit_events(tmp_path)
    transcribe = [e for e in events if e["event"] == "audit.voice.transcribe"]
    statuses = [e["status"] for e in transcribe]
    assert statuses == ["fail", "ok"]
    assert transcribe[-1]["attempt"] == 2


async def test_transient_failure_exhausts_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VoiceProcessor, "_backoff_seconds", staticmethod(lambda _a: 0.0))
    provider = _MockProvider(
        [
            TranscriptionError("503 a", status=503, retryable=True),
            TranscriptionError("503 b", status=503, retryable=True),
            TranscriptionError("503 c", status=503, retryable=True),
        ]
    )
    proc = VoiceProcessor(
        config=_config(tmp_path, retry_attempts=2),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    with pytest.raises(TranscriptionError):
        await proc.transcribe_inbound(
            audio_path=audio,
            mime_type="audio/ogg",
            conv_key="telegram:dm:42",
            message_id="m1",
        )

    # retry_attempts=2 -> 3 total attempts.
    assert len(provider.calls) == 3
    events = _read_audit_events(tmp_path)
    fails = [
        e
        for e in events
        if e["event"] == "audit.voice.transcribe" and e.get("status") == "fail"
    ]
    assert len(fails) == 3


async def test_unsupported_codec_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VoiceProcessor, "_backoff_seconds", staticmethod(lambda _a: 0.0))
    provider = _MockProvider(
        [
            # HTTP 400 / unsupported codec is terminal — no retry.
            TranscriptionError("unsupported codec", status=400, retryable=False),
            "should never be reached",
        ]
    )
    proc = VoiceProcessor(
        config=_config(tmp_path, retry_attempts=2),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    with pytest.raises(TranscriptionError):
        await proc.transcribe_inbound(
            audio_path=audio,
            mime_type="audio/x-weird",
            conv_key="telegram:dm:42",
            message_id="m1",
        )

    assert len(provider.calls) == 1  # no retry on a 4xx
    events = _read_audit_events(tmp_path)
    fails = [
        e
        for e in events
        if e["event"] == "audit.voice.transcribe" and e.get("status") == "fail"
    ]
    assert len(fails) == 1


async def test_keep_audio_files_false_unlinks_after_call(tmp_path: Path) -> None:
    provider = _MockProvider(["transient note"])
    proc = VoiceProcessor(
        config=_config(tmp_path, keep_audio_files=False),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    result = await proc.transcribe_inbound(
        audio_path=audio,
        mime_type="audio/ogg",
        conv_key="telegram:dm:42",
        message_id="m1",
    )

    assert result == "transient note"
    assert not audio.exists()  # unlinked in the finally block


async def test_keep_audio_files_false_unlinks_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VoiceProcessor, "_backoff_seconds", staticmethod(lambda _a: 0.0))
    provider = _MockProvider(
        [TranscriptionError("bad codec", status=400, retryable=False)]
    )
    proc = VoiceProcessor(
        config=_config(tmp_path, keep_audio_files=False, retry_attempts=0),
        inbound_provider=provider,
        outbound_provider=None,
    )
    audio = _audio_file(tmp_path)

    with pytest.raises(TranscriptionError):
        await proc.transcribe_inbound(
            audio_path=audio,
            mime_type="audio/x-weird",
            conv_key="telegram:dm:42",
            message_id="m1",
        )

    assert not audio.exists()


def test_mark_voice_inbound_caches_and_expires(tmp_path: Path) -> None:
    cfg = AnnaConfig.model_validate(
        {"voice": {"outbound": {"recent_voice_window_seconds": 600}}}
    ).model_copy(update={"anna_home": tmp_path})
    proc = VoiceProcessor(config=cfg, inbound_provider=None, outbound_provider=None)

    assert proc._recent_voice_active("telegram:dm:42") is False
    proc.mark_voice_inbound(conv_key="telegram:dm:42")
    assert proc._recent_voice_active("telegram:dm:42") is True
    # A different conv_key is unaffected.
    assert proc._recent_voice_active("slack:dm:U1") is False


def test_clear_voice_inbound_drops_active_mark(tmp_path: Path) -> None:
    cfg = AnnaConfig.model_validate(
        {"voice": {"outbound": {"recent_voice_window_seconds": 600}}}
    ).model_copy(update={"anna_home": tmp_path})
    proc = VoiceProcessor(config=cfg, inbound_provider=None, outbound_provider=None)

    proc.mark_voice_inbound(conv_key="telegram:dm:42")
    assert proc._recent_voice_active("telegram:dm:42") is True

    proc.clear_voice_inbound(conv_key="telegram:dm:42")
    assert proc._recent_voice_active("telegram:dm:42") is False

    # Clearing an unknown conv_key is a harmless no-op.
    proc.clear_voice_inbound(conv_key="slack:dm:never-marked")


async def test_synthesize_outbound_none_after_clear(tmp_path: Path) -> None:
    provider = _MockTTSProvider(b"unused")
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path),
        inbound_provider=None,
        outbound_provider=provider,
    )
    proc.mark_voice_inbound(conv_key="telegram:dm:42")
    proc.clear_voice_inbound(conv_key="telegram:dm:42")

    result = await proc.maybe_synthesize_outbound(
        text="hello", conv_key="telegram:dm:42", transport="telegram"
    )

    assert result is None
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Subtask 7: maybe_synthesize_outbound
# ---------------------------------------------------------------------------


class _MockTTSProvider:
    """A scriptable TTS provider.

    ``result`` is returned by ``synthesize`` (bytes) or raised if it is an
    Exception. ``calls`` records the kwargs of each synthesize call.
    """

    name = "mock-tts"
    output_mime_type = "audio/ogg"
    output_extension = ".ogg"

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def synthesize(self, *, text: str, voice_id: str | None = None) -> bytes:
        self.calls.append({"text": text, "voice_id": voice_id})
        if isinstance(self._result, Exception):
            raise self._result
        assert isinstance(self._result, bytes)
        return self._result


def _outbound_config(tmp_path: Path, **outbound_overrides: object) -> AnnaConfig:
    raw: dict[str, object] = {"voice": {"outbound": dict(outbound_overrides)}}
    cfg = AnnaConfig.model_validate(raw)
    return cfg.model_copy(update={"anna_home": tmp_path})


async def test_synthesize_outbound_happy_path(tmp_path: Path) -> None:
    provider = _MockTTSProvider(b"OggS-audio-bytes")
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path, voice_id="alloy"),
        inbound_provider=None,
        outbound_provider=provider,
    )
    proc.mark_voice_inbound(conv_key="telegram:dm:42")

    result = await proc.maybe_synthesize_outbound(
        text="here is your reply",
        conv_key="telegram:dm:42",
        transport="telegram",
    )

    assert result is not None
    audio, mime, ext = result
    assert audio == b"OggS-audio-bytes"
    assert mime == "audio/ogg"
    assert ext == ".ogg"
    assert provider.calls == [{"text": "here is your reply", "voice_id": "alloy"}]
    events = _read_audit_events(tmp_path)
    outbound = [e for e in events if e["event"] == "audit.voice.outbound"]
    assert len(outbound) == 1
    assert outbound[0]["status"] == "ok"
    assert outbound[0]["provider"] == "mock-tts"
    assert outbound[0]["text_chars"] == len("here is your reply")
    assert outbound[0]["audio_bytes"] == len(b"OggS-audio-bytes")
    assert outbound[0]["transport"] == "telegram"


async def test_synthesize_outbound_disabled_returns_none(tmp_path: Path) -> None:
    provider = _MockTTSProvider(b"unused")
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path, enabled=False),
        inbound_provider=None,
        outbound_provider=provider,
    )
    proc.mark_voice_inbound(conv_key="telegram:dm:42")

    result = await proc.maybe_synthesize_outbound(
        text="hello", conv_key="telegram:dm:42", transport="telegram"
    )

    assert result is None
    assert provider.calls == []


async def test_synthesize_outbound_transport_not_in_allowlist(tmp_path: Path) -> None:
    provider = _MockTTSProvider(b"unused")
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path, transports=["telegram"]),
        inbound_provider=None,
        outbound_provider=provider,
    )
    proc.mark_voice_inbound(conv_key="slack:dm:U1")

    result = await proc.maybe_synthesize_outbound(
        text="hello", conv_key="slack:dm:U1", transport="slack"
    )

    assert result is None
    assert provider.calls == []


async def test_synthesize_outbound_text_too_long(tmp_path: Path) -> None:
    provider = _MockTTSProvider(b"unused")
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path, max_synthesis_chars=10),
        inbound_provider=None,
        outbound_provider=provider,
    )
    proc.mark_voice_inbound(conv_key="telegram:dm:42")

    result = await proc.maybe_synthesize_outbound(
        text="this text is definitely longer than ten chars",
        conv_key="telegram:dm:42",
        transport="telegram",
    )

    assert result is None
    assert provider.calls == []


async def test_synthesize_outbound_no_recent_voice_inbound(tmp_path: Path) -> None:
    provider = _MockTTSProvider(b"unused")
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path),
        inbound_provider=None,
        outbound_provider=provider,
    )
    # No mark_voice_inbound call -> cache miss.

    result = await proc.maybe_synthesize_outbound(
        text="hello", conv_key="telegram:dm:42", transport="telegram"
    )

    assert result is None
    assert provider.calls == []


async def test_synthesize_outbound_tts_failure_returns_none_and_audits(
    tmp_path: Path,
) -> None:
    provider = _MockTTSProvider(TTSError("openai-tts got HTTP 500"))
    proc = VoiceProcessor(
        config=_outbound_config(tmp_path),
        inbound_provider=None,
        outbound_provider=provider,
    )
    proc.mark_voice_inbound(conv_key="telegram:dm:42")

    # Must not raise — outbound failure degrades to text.
    result = await proc.maybe_synthesize_outbound(
        text="hello", conv_key="telegram:dm:42", transport="telegram"
    )

    assert result is None
    assert len(provider.calls) == 1
    events = _read_audit_events(tmp_path)
    outbound = [e for e in events if e["event"] == "audit.voice.outbound"]
    assert len(outbound) == 1
    assert outbound[0]["status"] == "fail"
    assert "500" in str(outbound[0]["error"])
    assert outbound[0]["transport"] == "telegram"
