"""Telegram inbound voice detection + download (Pass 2, subtask 6).

Telegram exposes voice as ``update.message.voice`` (a ``telegram.Voice``
with ``file_id``, ``duration``, ``mime_type`` "audio/ogg",
``file_size``). The adapter downloads via the existing PTB bot
(``bot.get_file`` + ``download_to_drive``), transcribes via the
VoiceProcessor, and substitutes the transcript into
``InboundEvent.text`` prefixed by the ``[voice transcript]:`` marker.

Mirrors the five Slack cases:

* a voice message -> transcript substitution (mock VoiceProcessor);
* a plain text message passes through unchanged;
* voice=None yields the polite "voice is off" reply;
* a transcribe failure yields a polite operator-facing error text;
* mark_voice_inbound is called after a successful transcribe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anna.config import AnnaConfig
from anna.transports.telegram import TelegramAdapter

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeVoice:
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


class _FakeBotFile:
    """Stand-in for the handle returned by ``bot.get_file``."""

    def __init__(self) -> None:
        self.download_paths: list[str] = []

    async def download_to_drive(self, custom_path: str) -> None:
        self.download_paths.append(custom_path)
        Path(custom_path).write_bytes(b"\x00\x01\x02")


class _FakeBot:
    def __init__(self) -> None:
        self.bot_file = _FakeBotFile()
        self.get_file_calls: list[str] = []

    async def get_file(self, file_id: str) -> _FakeBotFile:
        self.get_file_calls.append(file_id)
        return self.bot_file


class _FakeApplication:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot


class _FakeChat:
    def __init__(self, chat_id: int = 42, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type


class _FakeUser:
    def __init__(self) -> None:
        self.id = 7
        self.full_name = "Tester"


class _FakeVoiceObj:
    def __init__(
        self,
        *,
        file_id: str = "VOICE123",
        duration: int = 4,
        mime_type: str = "audio/ogg",
        file_size: int = 2048,
    ) -> None:
        self.file_id = file_id
        self.duration = duration
        self.mime_type = mime_type
        self.file_size = file_size


class _FakeMessage:
    def __init__(
        self,
        *,
        text: str | None = None,
        voice: _FakeVoiceObj | None = None,
        message_id: int = 100,
    ) -> None:
        self.text = text
        self.voice = voice
        self.message_id = message_id
        self.chat = _FakeChat()
        self.from_user = _FakeUser()
        self.message_thread_id = None


class _FakeUpdate:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


def _make_adapter(
    tmp_path: Path,
    *,
    voice: _FakeVoice | None = None,
    inbound_enabled: bool = True,
) -> TelegramAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.voice.inbound.enabled = inbound_enabled
    adapter = TelegramAdapter(config=cfg, voice=voice)
    adapter._application = _FakeApplication(_FakeBot())  # type: ignore[attr-defined]
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_voice_message_substitutes_transcript(tmp_path: Path) -> None:
    voice = _FakeVoice("hello from a telegram voice note")
    adapter = _make_adapter(tmp_path, voice=voice)
    update = _FakeUpdate(_FakeMessage(voice=_FakeVoiceObj()))

    inbound = await adapter._to_inbound_event(update)

    assert inbound.text == "[voice transcript]: hello from a telegram voice note"
    assert len(voice.transcribe_calls) == 1
    call = voice.transcribe_calls[0]
    assert call["mime_type"] == "audio/ogg"
    assert call["duration_seconds"] == 4.0
    assert Path(call["audio_path"]).exists()
    # The bot was asked for the right file_id.
    assert adapter._application.bot.get_file_calls == ["VOICE123"]


async def test_plain_text_passes_through_unchanged(tmp_path: Path) -> None:
    voice = _FakeVoice("should not be used")
    adapter = _make_adapter(tmp_path, voice=voice)
    update = _FakeUpdate(_FakeMessage(text="just typing"))

    inbound = await adapter._to_inbound_event(update)

    assert inbound.text == "just typing"
    assert voice.transcribe_calls == []
    assert voice.mark_calls == []
    # A text inbound clears any prior voice mark for this conv_key.
    assert voice.clear_calls == [inbound.conversation_key]


async def test_voice_none_passes_through_unchanged(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, voice=None)
    update = _FakeUpdate(_FakeMessage(voice=_FakeVoiceObj()))

    inbound = await adapter._to_inbound_event(update)

    assert inbound.text == (
        "[voice transcript]: (voice transcription is off — please type "
        "your message instead)"
    )


async def test_transcribe_failure_yields_polite_error(tmp_path: Path) -> None:
    voice = _FakeVoice(RuntimeError("whisper exploded"))
    adapter = _make_adapter(tmp_path, voice=voice)
    update = _FakeUpdate(_FakeMessage(voice=_FakeVoiceObj()))

    inbound = await adapter._to_inbound_event(update)

    assert "couldn't transcribe" in inbound.text
    assert inbound.text.startswith("[voice transcript]:")
    assert voice.mark_calls == []


async def test_mark_voice_inbound_called_on_success(tmp_path: Path) -> None:
    voice = _FakeVoice("noted")
    adapter = _make_adapter(tmp_path, voice=voice)
    update = _FakeUpdate(_FakeMessage(voice=_FakeVoiceObj()))

    inbound = await adapter._to_inbound_event(update)

    assert voice.mark_calls == [inbound.conversation_key]
    assert inbound.raw.get("voice_mime_type") == "audio/ogg"
    assert "voice_audio_path" in inbound.raw
