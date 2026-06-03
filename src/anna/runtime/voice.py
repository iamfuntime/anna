"""Phase 2.5 voice messages — in-process orchestrator and providers.

This module houses the runtime-level voice capability the Slack and
Telegram adapters consume. It sits cleanly upstream of the router:
inbound audio is transcribed and the transcript is substituted into the
``InboundEvent.text`` before dispatch, so the worker sees a normal text
turn. The CLI transport, scheduler, and sub-agent runtime are
voice-agnostic and unaffected.

Three pieces live here:

* The provider Protocols (:class:`TranscriptionProvider`,
  :class:`TTSProvider`) plus their error types
  (:class:`TranscriptionError`, :class:`TTSError`). Providers are
  explicitly named in the config block; there is no plugin discovery.
* The concrete providers. :class:`WhisperOpenAIProvider` is a real
  httpx wrapper over OpenAI ``/v1/audio/transcriptions``;
  :class:`OpenAITTSProvider` is the matching httpx wrapper over OpenAI
  ``/v1/audio/speech`` (Opus-in-OGG output);
  :class:`FasterWhisperLocalProvider` (gated by the ``voice-local``
  extras) is a stub for now.
* :class:`VoiceProcessor`, the process-wide orchestrator constructed
  once in ``__main__.py`` and handed to every adapter that might process
  voice. It owns the size/duration gates, the retry loop, the audit
  events, the audio-file lifecycle, and the recent-voice-inbound cache
  the outbound path consults.

See Inbox/2026-06-02-ANNA-Voice-Messages-Plan.md for the full design.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger

if TYPE_CHECKING:  # pragma: no cover - import-only for typing
    pass


_OPENAI_TRANSCRIBE_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
_OPENAI_SPEECH_ENDPOINT = "https://api.openai.com/v1/audio/speech"


# ---------------------------------------------------------------------------
# Protocols + errors
# ---------------------------------------------------------------------------


class TranscriptionError(Exception):
    """Raised by a transcription provider (or the gates) on failure.

    Carries an optional ``status`` (HTTP status code when the failure
    originated from an upstream call) so :meth:`VoiceProcessor` can decide
    whether the failure is retryable. ``retryable`` lets a gate or a
    non-HTTP failure mark itself explicitly.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class TTSError(Exception):
    """Raised by a TTS provider on failure."""


@runtime_checkable
class TranscriptionProvider(Protocol):
    name: str  # "whisper-openai" | "faster-whisper-local" | "elevenlabs" | "claude-audio"

    async def transcribe(
        self,
        *,
        audio_path: Path,
        mime_type: str,
        hint_language: str | None = None,
    ) -> str:
        """Return the transcribed text. Raises TranscriptionError on failure.

        Implementations MUST be idempotent: the same audio_path may be
        passed twice (e.g., a retry after a transient HTTP error) and
        must produce the same result with no side effects on the file.
        """
        ...


@runtime_checkable
class TTSProvider(Protocol):
    name: str  # "openai-tts" | "elevenlabs" | "azure-tts"
    output_mime_type: str  # "audio/ogg" | "audio/mpeg" | ...
    output_extension: str  # ".ogg" | ".mp3" | ...

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str | None = None,
    ) -> bytes:
        """Return the synthesized audio as bytes. Raises TTSError on failure."""
        ...


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class WhisperOpenAIProvider:
    """OpenAI Whisper transcription over ``/v1/audio/transcriptions``.

    A thin httpx wrapper around the OpenAI audio-transcriptions endpoint.
    The API key is read from the env var named in the config
    (``voice.inbound.api_key_env``, default ``OPENAI_API_KEY``) at
    construction time. Native codecs (Opus-in-OGG from Telegram,
    WebM-Opus from Slack) are accepted directly; no server-side
    transcoding.

    The provider never sets its own wall-clock timeout — the
    :class:`VoiceProcessor` owns the ``asyncio.wait_for`` bound so the
    timeout semantics live in one place. Transport errors and non-2xx
    responses raise :class:`TranscriptionError` with the upstream status
    attached so the orchestrator's retry policy can distinguish transient
    (5xx / timeout) from terminal (4xx / unsupported codec).
    """

    name = "whisper-openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._log = get_logger("anna.voice.whisper")
        # Lazily created, reused for the lifetime of the process. A
        # caller-supplied client (tests) wins.
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def transcribe(
        self,
        *,
        audio_path: Path,
        mime_type: str,
        hint_language: str | None = None,
    ) -> str:
        client = await self._get_client()
        data: dict[str, str] = {"model": self._model}
        if hint_language:
            data["language"] = hint_language

        try:
            audio_bytes = audio_path.read_bytes()
        except OSError as exc:
            raise TranscriptionError(
                f"could not read audio file {audio_path}: {exc}",
                retryable=False,
            ) from exc

        files = {"file": (audio_path.name, audio_bytes, mime_type)}
        try:
            resp = await client.post(
                _OPENAI_TRANSCRIBE_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=data,
                files=files,
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionError(
                f"whisper transcribe timed out: {exc}",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError(
                f"whisper transcribe transport error: {exc}",
                retryable=True,
            ) from exc

        if resp.status_code >= 500:
            raise TranscriptionError(
                f"whisper got HTTP {resp.status_code} — upstream outage",
                status=resp.status_code,
                retryable=True,
            )
        if resp.status_code >= 400:
            raise TranscriptionError(
                f"whisper got HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
                retryable=False,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise TranscriptionError(
                f"whisper returned a non-JSON body: {exc}",
                retryable=False,
            ) from exc

        text = payload.get("text")
        if not isinstance(text, str):
            raise TranscriptionError(
                "whisper response missing 'text' field",
                retryable=False,
            )
        return text.strip()


class FasterWhisperLocalProvider:
    """Local (offline) transcription via faster-whisper.

    Stub for Pass 1. The full implementation lazily loads a
    ``faster_whisper.WhisperModel`` (gated by the ``voice-local`` extras:
    ``faster-whisper`` + ``ctranslate2``) and runs inference off the event
    loop via ``asyncio.to_thread``. Until then, constructing it is fine
    but :meth:`transcribe` raises so a misconfigured deployment fails
    loudly rather than silently.
    """

    name = "faster-whisper-local"

    def __init__(self, *, model: str) -> None:
        self._model = model

    async def transcribe(
        self,
        *,
        audio_path: Path,
        mime_type: str,
        hint_language: str | None = None,
    ) -> str:
        raise NotImplementedError(
            "FasterWhisperLocalProvider is not implemented yet; install the "
            "voice-local extras and set voice.inbound.provider: whisper-openai "
            "in the meantime"
        )


class OpenAITTSProvider:
    """OpenAI text-to-speech over ``/v1/audio/speech``.

    A thin httpx wrapper around the OpenAI audio-speech endpoint, mirroring
    the :class:`WhisperOpenAIProvider` httpx/error style. Output is
    Opus-in-OGG (``response_format="opus"``) so Telegram's ``send_voice``
    accepts the bytes directly and Slack can upload them as an ``.ogg``
    attachment.

    The API key is read from the env var named in the config
    (``voice.outbound.api_key_env``, default ``OPENAI_API_KEY``) at
    construction time. The provider never sets its own wall-clock timeout —
    the :class:`VoiceProcessor` owns the ``asyncio.wait_for`` bound. Any
    transport error or non-2xx response raises :class:`TTSError`.
    """

    name = "openai-tts"
    output_mime_type = "audio/ogg"
    output_extension = ".ogg"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._log = get_logger("anna.voice.tts")
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str | None = None,
    ) -> bytes:
        client = await self._get_client()
        payload: dict[str, str] = {
            "model": self._model,
            "voice": voice_id or "alloy",
            "input": text,
            # Opus-in-OGG: Telegram send_voice wants OGG/Opus; Slack uploads
            # the same bytes as an .ogg attachment.
            "response_format": "opus",
        }
        try:
            resp = await client.post(
                _OPENAI_SPEECH_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise TTSError(f"openai-tts synthesize timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TTSError(f"openai-tts synthesize transport error: {exc}") from exc

        if resp.status_code >= 400:
            raise TTSError(
                f"openai-tts got HTTP {resp.status_code}: {resp.text[:200]}"
            )

        audio = resp.content
        if not audio:
            raise TTSError("openai-tts returned an empty audio body")
        return audio


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class VoiceProcessor:
    """Process-wide orchestrator for voice inbound + outbound.

    Constructed once in ``__main__.py`` and handed to every adapter that
    might process voice (Slack, Telegram for v1). Holds the configured
    inbound provider and the configured outbound provider; both are
    optional (a deployment can enable inbound-only or outbound-only).

    See the class-signatures section of
    Inbox/2026-06-02-ANNA-Voice-Messages-Plan.md for the per-event flow.
    """

    def __init__(
        self,
        *,
        config: AnnaConfig,
        inbound_provider: TranscriptionProvider | None,
        outbound_provider: TTSProvider | None,
    ) -> None:
        self._config = config
        self._inbound = inbound_provider
        self._outbound = outbound_provider
        self._log = get_logger("anna.voice")
        self._audit_dir = config.audit_dir
        self._fsync = config.logging.audit.fsync_on_write
        # Per-conv_key "most-recent inbound was voice" cache. Maps
        # conv_key -> monotonic expiry timestamp. The outbound path reads
        # it; mark_voice_inbound writes it.
        self._recent_voice_inbounds: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Audit helper (mirrors the runtime-module pattern)
    # ------------------------------------------------------------------

    def _audit(
        self,
        event: str,
        *,
        conv_key: str,
        level: str = "INFO",
        **fields: Any,
    ) -> None:
        audit_event(
            event,
            audit_dir=self._audit_dir,
            actor="anna",
            conv_key=conv_key,
            fsync_on_write=self._fsync,
            level=level,
            **fields,
        )

    # ------------------------------------------------------------------
    # Recent-voice-inbound cache
    # ------------------------------------------------------------------

    def mark_voice_inbound(self, *, conv_key: str) -> None:
        """Cache that the most-recent inbound on this conv_key was voice.

        Called by the adapter right after a successful transcribe so the
        outbound path can decide whether to synthesize. TTL governed by
        voice.outbound.recent_voice_window_seconds (default 600).
        """
        ttl = self._config.voice.outbound.recent_voice_window_seconds
        self._recent_voice_inbounds[conv_key] = time.monotonic() + ttl

    def _recent_voice_active(self, conv_key: str) -> bool:
        """True when this conv_key has an unexpired voice-inbound mark."""
        expiry = self._recent_voice_inbounds.get(conv_key)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            # Lazily evict so the cache doesn't grow without bound.
            self._recent_voice_inbounds.pop(conv_key, None)
            return False
        return True

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: TranscriptionError) -> bool:
        """Decide whether a transcription failure should be retried.

        Transient: HTTP 5xx and timeouts/transport errors (the provider
        flags these with ``retryable=True`` or a 5xx status). Terminal:
        HTTP 4xx (bad request, unsupported codec, auth) — no retry burns
        another doomed call.
        """
        if exc.status is not None:
            return exc.status >= 500
        return exc.retryable

    async def transcribe_inbound(
        self,
        *,
        audio_path: Path,
        mime_type: str,
        conv_key: str,
        message_id: str,
        duration_seconds: float | None = None,
    ) -> str:
        """Transcribe a downloaded inbound voice file.

        Enforces the size/duration gates before any provider call, runs
        the configured retry loop with exponential backoff (transient
        failures only), wraps each attempt in an ``asyncio.wait_for``
        timeout, emits the ``audit.voice.inbound`` and
        ``audit.voice.transcribe`` events, and honors the
        ``keep_audio_files=false`` unlink-after-call path.

        Raises :class:`TranscriptionError` on any terminal failure (gate
        rejection, exhausted retries, unsupported codec) so the adapter
        can route through the polite operator-facing error path.
        """
        cfg = self._config.voice.inbound

        if self._inbound is None:
            raise TranscriptionError(
                "voice inbound is disabled (no transcription provider configured)",
                retryable=False,
            )

        provider = self._inbound

        try:
            size_bytes = audio_path.stat().st_size
        except OSError as exc:
            raise TranscriptionError(
                f"could not stat audio file {audio_path}: {exc}",
                retryable=False,
            ) from exc

        self._audit(
            "audit.voice.inbound",
            conv_key=conv_key,
            message_id=message_id,
            provider=provider.name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            duration_seconds=duration_seconds,
        )

        # --- Gates: reject before burning a transcribe call. -----------
        try:
            if size_bytes > cfg.max_audio_size_bytes:
                self._audit(
                    "audit.voice.transcribe",
                    conv_key=conv_key,
                    level="WARNING",
                    message_id=message_id,
                    provider=provider.name,
                    status="reject",
                    error="size_cap",
                    size_bytes=size_bytes,
                    max_audio_size_bytes=cfg.max_audio_size_bytes,
                    attempt=0,
                )
                raise TranscriptionError(
                    f"voice note is {size_bytes} bytes, over the "
                    f"{cfg.max_audio_size_bytes}-byte cap",
                    retryable=False,
                )
            if (
                duration_seconds is not None
                and duration_seconds > cfg.max_duration_seconds
            ):
                self._audit(
                    "audit.voice.transcribe",
                    conv_key=conv_key,
                    level="WARNING",
                    message_id=message_id,
                    provider=provider.name,
                    status="reject",
                    error="duration_cap",
                    duration_seconds=duration_seconds,
                    max_duration_seconds=cfg.max_duration_seconds,
                    attempt=0,
                )
                raise TranscriptionError(
                    f"voice note is {duration_seconds:.0f}s, over the "
                    f"{cfg.max_duration_seconds}s cap",
                    retryable=False,
                )

            # --- Retry loop. -------------------------------------------
            # retry_attempts is the number of *extra* attempts, so the
            # total budget is retry_attempts + 1.
            total_attempts = cfg.retry_attempts + 1
            last_exc: TranscriptionError | None = None
            for attempt in range(1, total_attempts + 1):
                started = time.monotonic()
                try:
                    transcript = await asyncio.wait_for(
                        provider.transcribe(
                            audio_path=audio_path,
                            mime_type=mime_type,
                            hint_language=cfg.hint_language,
                        ),
                        timeout=cfg.timeout_seconds,
                    )
                except TimeoutError as exc:
                    last_exc = TranscriptionError(
                        f"transcribe timed out after {cfg.timeout_seconds}s",
                        retryable=True,
                    )
                    latency = time.monotonic() - started
                    self._audit(
                        "audit.voice.transcribe",
                        conv_key=conv_key,
                        level="WARNING",
                        message_id=message_id,
                        provider=provider.name,
                        status="fail",
                        error="timeout",
                        latency_seconds=round(latency, 3),
                        attempt=attempt,
                    )
                    if attempt < total_attempts:
                        await asyncio.sleep(self._backoff_seconds(attempt))
                        continue
                    raise last_exc from exc
                except TranscriptionError as exc:
                    last_exc = exc
                    latency = time.monotonic() - started
                    self._audit(
                        "audit.voice.transcribe",
                        conv_key=conv_key,
                        level="WARNING",
                        message_id=message_id,
                        provider=provider.name,
                        status="fail",
                        error=str(exc),
                        latency_seconds=round(latency, 3),
                        attempt=attempt,
                    )
                    if attempt < total_attempts and self._is_retryable(exc):
                        await asyncio.sleep(self._backoff_seconds(attempt))
                        continue
                    raise

                latency = time.monotonic() - started
                self._audit(
                    "audit.voice.transcribe",
                    conv_key=conv_key,
                    message_id=message_id,
                    provider=provider.name,
                    status="ok",
                    latency_seconds=round(latency, 3),
                    transcript_chars=len(transcript),
                    attempt=attempt,
                )
                return transcript

            # Loop fell through (only reachable if total_attempts <= 0,
            # which the config validator forbids). Defensive re-raise.
            assert last_exc is not None  # noqa: S101
            raise last_exc
        finally:
            if not cfg.keep_audio_files:
                try:
                    audio_path.unlink()
                except OSError:
                    # Best-effort cleanup; the housekeeping sweep is the
                    # backstop for any straggler.
                    self._log.warning(
                        "voice.unlink_failed",
                        audio_path=str(audio_path),
                        conv_key=conv_key,
                    )

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Exponential backoff: 0.5s, 1s, 2s, ... for attempt 1, 2, 3, ..."""
        return 0.5 * float(2 ** (attempt - 1))

    # ------------------------------------------------------------------
    # Outbound (stub — full impl lands in Pass 3)
    # ------------------------------------------------------------------

    async def maybe_synthesize_outbound(
        self,
        *,
        text: str,
        conv_key: str,
        transport: str,
    ) -> tuple[bytes, str, str] | None:
        """Returns (audio_bytes, mime_type, extension) when:

        * voice.outbound.enabled is true
        * transport is in voice.outbound.transports
        * the most-recent inbound on this conv_key was voice
          (within recent_voice_window_seconds)

        Returns None otherwise (the adapter falls back to a text-only
        send). The adapter is responsible for the actual upload/send.

        On :class:`TTSError`, emits ``audit.voice.outbound`` with
        ``status="fail"`` and returns ``None`` — outbound failure must
        degrade to text, never raise.
        """
        cfg = self._config.voice.outbound

        # Decision gates — any miss returns None for a clean text-only send.
        if not cfg.enabled:
            return None
        if self._outbound is None:
            return None
        if transport not in cfg.transports:
            return None
        if len(text) > cfg.max_synthesis_chars:
            return None
        if not self._recent_voice_active(conv_key):
            return None

        provider = self._outbound
        started = time.monotonic()
        try:
            audio = await asyncio.wait_for(
                provider.synthesize(
                    text=text,
                    voice_id=cfg.voice_id,
                ),
                timeout=cfg.timeout_seconds,
            )
        except (TTSError, TimeoutError, asyncio.TimeoutError) as exc:
            latency = time.monotonic() - started
            self._audit(
                "audit.voice.outbound",
                conv_key=conv_key,
                level="WARNING",
                provider=provider.name,
                status="fail",
                error=str(exc) or "timeout",
                latency_seconds=round(latency, 3),
                text_chars=len(text),
                transport=transport,
            )
            return None

        latency = time.monotonic() - started
        self._audit(
            "audit.voice.outbound",
            conv_key=conv_key,
            provider=provider.name,
            status="ok",
            latency_seconds=round(latency, 3),
            text_chars=len(text),
            audio_bytes=len(audio),
            transport=transport,
        )
        return (audio, provider.output_mime_type, provider.output_extension)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_voice_processor(config: AnnaConfig) -> VoiceProcessor:
    """Construct the process-wide :class:`VoiceProcessor` from config.

    Resolves the inbound transcription provider and the outbound TTS
    provider from the ``voice:`` config block, reading API keys from the
    environment by the configured env-var names. A provider whose
    sub-block is disabled (or whose key is missing) resolves to ``None``
    so a deployment can run inbound-only, outbound-only, or neither; the
    :class:`VoiceProcessor` degrades cleanly in each case.
    """
    inbound_provider = _build_inbound_provider(config)
    outbound_provider = _build_outbound_provider(config)
    return VoiceProcessor(
        config=config,
        inbound_provider=inbound_provider,
        outbound_provider=outbound_provider,
    )


def _build_inbound_provider(config: AnnaConfig) -> TranscriptionProvider | None:
    cfg = config.voice.inbound
    if not cfg.enabled:
        return None
    if cfg.provider == "whisper-openai":
        api_key = os.environ.get(cfg.api_key_env, "").strip()
        if not api_key:
            get_logger("anna.voice").warning(
                "voice.inbound.missing_api_key",
                env_var=cfg.api_key_env,
                provider=cfg.provider,
            )
            return None
        return WhisperOpenAIProvider(api_key=api_key, model=cfg.model)
    if cfg.provider == "faster-whisper-local":
        return FasterWhisperLocalProvider(model=cfg.model)
    return None


def _build_outbound_provider(config: AnnaConfig) -> TTSProvider | None:
    cfg = config.voice.outbound
    if not cfg.enabled:
        return None
    if cfg.provider == "openai-tts":
        api_key = os.environ.get(cfg.api_key_env, "").strip()
        if not api_key:
            get_logger("anna.voice").warning(
                "voice.outbound.missing_api_key",
                env_var=cfg.api_key_env,
                provider=cfg.provider,
            )
            return None
        return OpenAITTSProvider(api_key=api_key, model=cfg.model)
    return None


__all__ = [
    "TranscriptionProvider",
    "TTSProvider",
    "TranscriptionError",
    "TTSError",
    "WhisperOpenAIProvider",
    "FasterWhisperLocalProvider",
    "OpenAITTSProvider",
    "VoiceProcessor",
    "build_voice_processor",
]
