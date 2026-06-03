"""Adapter outbound TTS hooks (Pass 3, subtask 8).

Covers the ``send()`` hooks on the Slack and Telegram adapters that
consult ``VoiceProcessor.maybe_synthesize_outbound`` and post a
synthesized voice note alongside (Slack) or instead of (Telegram, when
``voice_only``) the text reply.

Cases pinned in the plan:

* Slack posts text + audio when synth returns bytes;
* Slack posts text-only when synth returns None;
* Telegram sends voice-only when ``voice_only`` is true;
* Telegram sends voice + text when ``voice_only`` is false;
* Telegram falls back to text when synth returns None;
* a TTS failure (synth None) degrades to text-only.

The VoiceProcessor and the Slack client / Telegram bot are mocked; no
network and no real provider calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anna.config import AnnaConfig
from anna.transports.base import OutboundMessage
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation
from anna.transports.telegram import TelegramAdapter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeVoice:
    """Scriptable stand-in for VoiceProcessor.maybe_synthesize_outbound.

    ``synth`` is returned as-is (a ``(bytes, mime, ext)`` tuple, or None).
    ``synth_calls`` records each call's kwargs.
    """

    def __init__(self, synth: tuple[bytes, str, str] | None) -> None:
        self._synth = synth
        self.synth_calls: list[dict[str, Any]] = []

    async def maybe_synthesize_outbound(
        self, *, text: str, conv_key: str, transport: str
    ) -> tuple[bytes, str, str] | None:
        self.synth_calls.append(
            {"text": text, "conv_key": conv_key, "transport": transport}
        )
        return self._synth


class _StubSlackClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.post_calls.append(kwargs)
        return {"ok": True, "ts": "1716832700.000700"}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.upload_calls.append(kwargs)
        return {"ok": True}


class _StubTelegramBot:
    def __init__(self) -> None:
        self.message_calls: list[dict[str, Any]] = []
        self.voice_calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.message_calls.append(kwargs)

    async def send_voice(self, **kwargs: Any) -> None:
        self.voice_calls.append(kwargs)


class _FakeApplication:
    def __init__(self, bot: _StubTelegramBot) -> None:
        self.bot = bot
        self.updater = None


# ---------------------------------------------------------------------------
# Adapter builders
# ---------------------------------------------------------------------------


def _slack_adapter(
    tmp_path: Path, *, voice: _FakeVoice | None
) -> tuple[SlackAdapter, _StubSlackClient]:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    adapter = SlackAdapter(config=cfg, thread_participation=tp, voice=voice)
    client = _StubSlackClient()
    adapter._client = client  # type: ignore[attr-defined]
    return adapter, client


def _telegram_adapter(
    tmp_path: Path, *, voice: _FakeVoice | None, voice_only: bool = True
) -> tuple[TelegramAdapter, _StubTelegramBot]:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.voice.outbound.voice_only = voice_only
    adapter = TelegramAdapter(config=cfg, voice=voice)
    bot = _StubTelegramBot()
    adapter._application = _FakeApplication(bot)  # type: ignore[attr-defined]
    return adapter, bot


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


async def test_slack_posts_text_and_audio_when_synth_returns_bytes(
    tmp_path: Path,
) -> None:
    voice = _FakeVoice((b"OggS-bytes", "audio/ogg", ".ogg"))
    adapter, client = _slack_adapter(tmp_path, voice=voice)

    await adapter.send(
        OutboundMessage(conversation_key="slack:dm:U1", text="here you go")
    )

    # Text always posts.
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["text"] == "here you go"
    # Audio additionally uploaded.
    assert len(client.upload_calls) == 1
    up = client.upload_calls[0]
    assert up["content"] == b"OggS-bytes"
    assert up["filename"] == "voice.ogg"
    assert up["channel"] == "U1"
    assert voice.synth_calls[0]["transport"] == "slack"


async def test_slack_text_only_when_synth_returns_none(tmp_path: Path) -> None:
    voice = _FakeVoice(None)
    adapter, client = _slack_adapter(tmp_path, voice=voice)

    await adapter.send(
        OutboundMessage(conversation_key="slack:dm:U1", text="just text")
    )

    assert len(client.post_calls) == 1
    assert client.upload_calls == []


async def test_slack_text_only_when_no_voice_processor(tmp_path: Path) -> None:
    adapter, client = _slack_adapter(tmp_path, voice=None)

    await adapter.send(
        OutboundMessage(conversation_key="slack:dm:U1", text="byte-for-byte")
    )

    assert len(client.post_calls) == 1
    assert client.upload_calls == []


async def test_slack_upload_failure_does_not_raise(tmp_path: Path) -> None:
    voice = _FakeVoice((b"OggS-bytes", "audio/ogg", ".ogg"))
    adapter, client = _slack_adapter(tmp_path, voice=voice)

    async def _boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("slack upload is down")

    client.files_upload_v2 = _boom  # type: ignore[assignment]

    # Text already posted; upload failure must not propagate.
    await adapter.send(
        OutboundMessage(conversation_key="slack:dm:U1", text="text survives")
    )

    assert len(client.post_calls) == 1


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def test_telegram_voice_only_suppresses_text(tmp_path: Path) -> None:
    voice = _FakeVoice((b"OggS-bytes", "audio/ogg", ".ogg"))
    adapter, bot = _telegram_adapter(tmp_path, voice=voice, voice_only=True)

    await adapter.send(
        OutboundMessage(conversation_key="telegram:dm:42", text="spoken only")
    )

    assert len(bot.voice_calls) == 1
    assert bot.voice_calls[0]["voice"] == b"OggS-bytes"
    assert bot.voice_calls[0]["chat_id"] == 42
    # voice_only -> no text message.
    assert bot.message_calls == []


async def test_telegram_voice_and_text_when_not_voice_only(tmp_path: Path) -> None:
    voice = _FakeVoice((b"OggS-bytes", "audio/ogg", ".ogg"))
    adapter, bot = _telegram_adapter(tmp_path, voice=voice, voice_only=False)

    await adapter.send(
        OutboundMessage(conversation_key="telegram:dm:42", text="both please")
    )

    assert len(bot.voice_calls) == 1
    assert len(bot.message_calls) == 1
    assert bot.message_calls[0]["text"] == "both please"


async def test_telegram_falls_back_to_text_when_synth_none(tmp_path: Path) -> None:
    voice = _FakeVoice(None)
    adapter, bot = _telegram_adapter(tmp_path, voice=voice, voice_only=True)

    await adapter.send(
        OutboundMessage(conversation_key="telegram:dm:42", text="text fallback")
    )

    assert bot.voice_calls == []
    assert len(bot.message_calls) == 1
    assert bot.message_calls[0]["text"] == "text fallback"


async def test_telegram_send_voice_failure_falls_back_to_text(tmp_path: Path) -> None:
    voice = _FakeVoice((b"OggS-bytes", "audio/ogg", ".ogg"))
    adapter, bot = _telegram_adapter(tmp_path, voice=voice, voice_only=True)

    async def _boom(**kwargs: Any) -> None:
        raise RuntimeError("telegram send_voice failed")

    bot.send_voice = _boom  # type: ignore[assignment]

    await adapter.send(
        OutboundMessage(conversation_key="telegram:dm:42", text="text after voice fail")
    )

    # Voice failed -> text still sent (deliberate ordering).
    assert len(bot.message_calls) == 1
    assert bot.message_calls[0]["text"] == "text after voice fail"


async def test_telegram_text_only_when_no_voice_processor(tmp_path: Path) -> None:
    adapter, bot = _telegram_adapter(tmp_path, voice=None, voice_only=True)

    await adapter.send(
        OutboundMessage(conversation_key="telegram:dm:42", text="byte-for-byte")
    )

    assert bot.voice_calls == []
    assert len(bot.message_calls) == 1
