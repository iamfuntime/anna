"""Slack adapter.

Uses :class:`slack_bolt.async_app.AsyncApp` over Socket Mode via
:class:`slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler`.

Conversation key derivation, per v3 section 2:

* DM (``channel_type == "im"``): ``slack:dm:<user_id>``.
* Channel thread reply: ``slack:ch:<channel_id>:<thread_ts>``.
* ``app_mention`` not in a thread: ``slack:ch:<channel_id>:<event_ts>:oneshot``.
* ``app_mention`` in a thread: same as channel thread reply.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from anna.config import AnnaConfig
from anna.log import get_logger
from anna.transports.mrkdwn import normalize_to_slack_mrkdwn
from anna.transports.base import (
    ChannelAdapter,
    ImageAttachment,
    InboundEvent,
    InboundHandler,
    OutboundMessage,
    SignalHandle,
)
from anna.transports.slack_thread_state import ThreadParticipation

if TYPE_CHECKING:  # pragma: no cover - import-only for typing
    from anna.runtime.voice import VoiceProcessor

# Anthropic-viewable raster image types. Slack also delivers svg+xml,
# heic/heif, tiff, and bmp as ``image/*`` files; those are NOT accepted
# by the model's image block, so we reject them with an operator-facing
# marker rather than shipping bytes the API will refuse.
_SUPPORTED_IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}


class SlackAdapter(ChannelAdapter):
    name = "slack"

    def __init__(
        self,
        *,
        config: AnnaConfig,
        thread_participation: ThreadParticipation,
        voice: VoiceProcessor | None = None,
    ) -> None:
        self._config = config
        self._log = get_logger("anna.transport.slack")
        self._handlers: list[InboundHandler] = []
        self._app: Any = None
        self._handler: Any = None
        self._handler_task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._connect_attempt = 0
        self._thread_participation = thread_participation
        self._voice = voice

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError as exc:
            self._log.error("channel.import_failed", channel="slack", error=str(exc))
            raise

        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        app_token = os.environ.get("SLACK_APP_TOKEN")
        if not bot_token or not app_token:
            raise RuntimeError(
                "Slack transport enabled but SLACK_BOT_TOKEN or SLACK_APP_TOKEN missing"
            )

        # Load the thread-participation set before the socket-mode
        # client connects so the first inbound message hits a populated
        # filter. ``load`` is idempotent and tolerates a missing file.
        await self._thread_participation.load()

        self._app = AsyncApp(token=bot_token)
        self._client = self._app.client
        self._register_listeners()

        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._connect_attempt += 1
        # start_async blocks. Run it in a task so start() returns control.
        self._handler_task = asyncio.create_task(
            self._handler.start_async(),
            name="slack.socket_mode",
        )
        self._log.info(
            "channel.connected",
            channel="slack",
            attempt=self._connect_attempt,
        )

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception as exc:
                self._log.warning("channel.close_failed", channel="slack", error=str(exc))
        if self._handler_task is not None:
            self._handler_task.cancel()
            try:
                await self._handler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._handler_task = None
        self._log.info("channel.disconnected", channel="slack", reason="clean")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, message: OutboundMessage) -> None:
        if self._client is None:
            raise RuntimeError("Slack adapter not started")
        channel, thread_ts = self._channel_and_thread_for(message.conversation_key)

        # Phase 2.5 outbound voice: attempt a voice upload first when the
        # recent inbound on this conv_key was voice and outbound is enabled
        # for Slack. On success, suppress the text post when ``voice_only``
        # is set (audio-only reply); otherwise post both. On any voice
        # failure, fall through cleanly to the normal text post below. This
        # mirrors the Telegram adapter's ``voice_only`` gating exactly.
        audio_sent = await self._maybe_upload_voice(
            message=message, channel=channel, thread_ts=thread_ts
        )

        # Post the text unless audio landed AND voice_only is set. When
        # voice_only is false the text always posts (legacy behavior, plus
        # audio); when audio failed/declined the text is the fallback so the
        # operator still gets a reply.
        if not (audio_sent and self._config.voice.outbound.voice_only):
            try:
                # Normalize GitHub-flavored Markdown to Slack mrkdwn at the
                # transport boundary — the model emits ``**bold**``, ``##
                # headings``, ``[text](url)`` links, and ```` ```lang ````
                # fences that Slack renders as literal characters otherwise.
                # Applied ONLY to the posted text; the voice-synth path above
                # uses the raw ``message.text``, and ``blocks`` (structured)
                # pass through untouched. This is the single shared send path
                # the slack_post MCP tool also routes through, so report cards
                # and skill posts are normalized here too.
                mrkdwn_text = normalize_to_slack_mrkdwn(message.text)
                kwargs: dict[str, Any] = {"channel": channel, "text": mrkdwn_text}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                if message.structured and "blocks" in message.structured:
                    kwargs["blocks"] = message.structured["blocks"]
                response = await self._client.chat_postMessage(**kwargs)
                self._log.debug(
                    "channel.message.sent",
                    channel="slack",
                    conv_key=message.conversation_key,
                    text_length=len(message.text),
                    ts=response.get("ts"),
                )
            except Exception as exc:
                self._log.error(
                    "channel.send_failed",
                    channel="slack",
                    conv_key=message.conversation_key,
                    text_length=len(message.text),
                    error=str(exc),
                )
                raise

        # Mark thread participation once a reply has actually landed —
        # either the audio upload or the text post above. A send failure
        # raises before reaching here, so we never mark a thread we failed
        # to post in. This still fires on a voice-only send (audio landed,
        # text suppressed). DMs are already conversational; their conv_keys
        # start with ``slack:dm:`` and have no thread_ts so they're skipped.
        if thread_ts and not message.conversation_key.startswith("slack:dm:"):
            await self._thread_participation.mark(
                channel_id=channel,
                thread_ts=thread_ts,
            )

    async def _maybe_upload_voice(
        self,
        *,
        message: OutboundMessage,
        channel: str,
        thread_ts: str | None,
    ) -> bool:
        """Synthesize + upload an OGG voice note for this reply.

        Returns ``True`` when a voice note was successfully uploaded (so the
        caller can suppress the text post when ``voice_only``), ``False``
        otherwise. Returns ``False`` when no VoiceProcessor is wired or
        :meth:`VoiceProcessor.maybe_synthesize_outbound` declines (outbound
        disabled, Slack not in the allowlist, text too long, no recent voice
        inbound, or TTS failure), as well as when DM-channel resolution or the
        upload itself raises — on any failure the caller falls through to the
        normal text post so the operator still gets a reply.
        """
        if self._voice is None:
            return False
        try:
            synth = await self._voice.maybe_synthesize_outbound(
                text=message.text,
                conv_key=message.conversation_key,
                transport="slack",
            )
        except Exception as exc:
            self._log.warning(
                "voice.outbound.synth_failed",
                channel="slack",
                conv_key=message.conversation_key,
                error=str(exc),
            )
            return False
        if synth is None:
            return False
        audio_bytes, _mime_type, extension = synth

        # ``chat.postMessage`` accepts a bare user ID as ``channel`` and opens
        # the DM implicitly, so :meth:`_channel_and_thread_for` returns the
        # user ID for ``slack:dm:`` keys. The file-upload API is stricter: its
        # ``channel_id`` must be a real conversation ID matching
        # ``^[CGDZ][A-Z0-9]{8,}$`` and rejects user IDs (``U``/``W``). Resolve
        # the user ID to the IM channel ID before uploading.
        upload_channel = channel
        if channel[:1] in ("U", "W"):
            try:
                opened = await self._client.conversations_open(users=channel)
                upload_channel = opened["channel"]["id"]
            except Exception as exc:
                self._log.warning(
                    "voice.outbound.dm_resolve_failed",
                    channel="slack",
                    conv_key=message.conversation_key,
                    error=str(exc),
                )
                return False

        upload_kwargs: dict[str, Any] = {
            "channel": upload_channel,
            "content": audio_bytes,
            "filename": f"voice{extension}",
        }
        if thread_ts:
            upload_kwargs["thread_ts"] = thread_ts
        try:
            await self._client.files_upload_v2(**upload_kwargs)
            self._log.debug(
                "voice.outbound.uploaded",
                channel="slack",
                conv_key=message.conversation_key,
                audio_bytes=len(audio_bytes),
            )
            return True
        except Exception as exc:
            # Upload failed: return False so send() falls back to the text
            # post (the operator still gets a reply).
            self._log.warning(
                "voice.outbound.upload_failed",
                channel="slack",
                conv_key=message.conversation_key,
                error=str(exc),
            )
            return False

    def _channel_and_thread_for(self, conv_key: str) -> tuple[str, str | None]:
        """Recover the Slack channel and thread_ts from a conversation_key.

        Mirror of :meth:`conversation_key_for`. Knows about three shapes:
        slack:dm:<user>, slack:ch:<channel>:<ts>, slack:ch:<channel>:<ts>:oneshot.
        """
        parts = conv_key.split(":")
        if len(parts) >= 3 and parts[0] == "slack" and parts[1] == "dm":
            # DMs do not use thread_ts. The Web API accepts the user ID as
            # ``channel`` and opens the DM if necessary.
            return (parts[2], None)
        if parts[0] == "slack" and parts[1] == "ch":
            channel = parts[2]
            thread_ts = parts[3]
            return (channel, thread_ts)
        raise ValueError(f"unrecognized slack conv_key: {conv_key}")

    # ------------------------------------------------------------------
    # Subscribe and listeners
    # ------------------------------------------------------------------

    def subscribe(self, handler: InboundHandler) -> None:
        self._handlers.append(handler)

    def _register_listeners(self) -> None:
        @self._app.event("app_mention")
        async def _on_mention(event: dict[str, Any], body: dict[str, Any]) -> None:
            await self._dispatch_event(event, body)

        @self._app.event("message")
        async def _on_message(event: dict[str, Any], body: dict[str, Any]) -> None:
            await self._handle_message_event(event, body)

    async def _handle_message_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        """Filter a ``message`` event and dispatch if it should reach
        a worker.

        Bot echoes and message-edit subtypes are dropped. DMs always
        dispatch. Channel messages only dispatch when they're thread
        replies in a thread ANNA has already posted in — top-level
        channel messages still require an ``app_mention``, which fires
        the ``_on_mention`` handler above.
        """
        # Ignore bot echoes and message-edit/delete subtypes. ``file_share``
        # is the exception: Slack delivers voice notes (and other file
        # attachments) as a ``message`` event with ``subtype == "file_share"``,
        # so it must pass through to the voice-detection path in
        # ``_to_inbound_event``. Dropping it here is what silently swallowed
        # inbound voice notes.
        subtype = event.get("subtype")
        if event.get("bot_id") or (subtype and subtype != "file_share"):
            return

        channel_type = event.get("channel_type", "")
        thread_ts = event.get("thread_ts")
        channel_id = event.get("channel", "")

        # DMs always dispatch — every DM is conversational by
        # definition. Existing behavior, unchanged.
        if channel_type == "im":
            await self._dispatch_event(event, body)
            return

        # Channel messages: only dispatch if it's a thread reply
        # AND ANNA has participated in that thread. Top-level channel
        # messages still require @-mention (which fires the
        # app_mention handler separately).
        if thread_ts is None:
            return
        if not self._thread_participation.has(
            channel_id=channel_id, thread_ts=thread_ts
        ):
            return

        await self._dispatch_event(event, body)

    async def _dispatch_event(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        try:
            inbound = await self._to_inbound_event(event)
        except Exception as exc:
            self._log.warning("channel.normalize_failed", channel="slack", error=str(exc))
            return
        self._log.debug(
            "channel.message.received",
            channel="slack",
            conv_key=inbound.conversation_key,
            sender_id=inbound.sender_id,
            text_length=len(inbound.text),
            is_dm=inbound.is_dm,
            is_thread=inbound.is_thread,
        )
        for handler in self._handlers:
            try:
                await handler(inbound)
            except Exception as exc:
                self._log.error("router.handler_failed", error=str(exc))

    async def _to_inbound_event(self, event: dict[str, Any]) -> InboundEvent:
        channel_type = event.get("channel_type", "")
        thread_ts = event.get("thread_ts")
        user_id = event.get("user", "")
        text = event.get("text", "")
        is_dm = channel_type == "im"
        is_thread = bool(thread_ts)

        conv_key = self.conversation_key_for(event)

        # Phase 2.5 voice: a Slack voice note arrives as a files[] entry
        # with mode == "audio" / mimetype == "audio/webm". When present,
        # rewrite the inbound text to the transcript (or a polite error
        # one-liner) before dispatch so the worker sees a normal turn.
        #
        # Voice takes precedence and is EXCLUSIVE: a turn carrying a voice
        # note never also carries images (image detection is skipped). Only
        # a non-voice turn runs image detection. Both branches clear the
        # voice-inbound mark for text-like turns so the outbound path does
        # not treat an image-only turn as a rolling voice window.
        images: list[ImageAttachment] = []
        voice_file = self._detect_voice_file(event)
        if voice_file is not None:
            text = await self._handle_voice_inbound(
                event=event,
                voice_file=voice_file,
                conv_key=conv_key,
            )
        else:
            image_files = self._detect_image_files(event, voice_file=voice_file)
            if image_files:
                text, images = await self._handle_image_inbound(
                    event=event,
                    image_files=image_files,
                    conv_key=conv_key,
                    caption=text,
                )
            if self._voice is not None:
                # Text / image (non-voice) inbound: clear any prior
                # voice-inbound mark so the outbound path treats the literal
                # most-recent inbound as text, not a rolling voice window.
                self._voice.clear_voice_inbound(conv_key=conv_key)

        return InboundEvent(
            transport="slack",
            conversation_key=conv_key,
            sender_id=user_id,
            sender_display=user_id,  # Display-name lookup is a Phase 2 enrichment.
            text=text,
            is_dm=is_dm,
            is_thread=is_thread,
            raw=event,
            images=images,
        )

    # ------------------------------------------------------------------
    # Phase 2.5 voice inbound
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_voice_file(event: dict[str, Any]) -> dict[str, Any] | None:
        """Return the first audio ``files[]`` entry, or None.

        A Slack voice note is a file with ``mode == "audio"`` (the codec
        is WebM-Opus, ``mimetype == "audio/webm"``). Any other file
        attachment (image, document, snippet) is ignored — voice is the
        only attachment type this adapter rewrites.
        """
        files = event.get("files")
        if not isinstance(files, list):
            return None
        for entry in files:
            if not isinstance(entry, dict):
                continue
            mode = entry.get("mode")
            mimetype = entry.get("mimetype", "")
            if mode == "audio" or (
                isinstance(mimetype, str) and mimetype.startswith("audio/")
            ):
                return entry
        return None

    async def _handle_voice_inbound(
        self,
        *,
        event: dict[str, Any],
        voice_file: dict[str, Any],
        conv_key: str,
    ) -> str:
        """Download + transcribe an inbound Slack voice note.

        Returns the text to carry on the ``InboundEvent``: the transcript
        prefixed by the ``[voice transcript]:`` marker on success, or a
        polite operator-facing one-liner on any failure (voice disabled,
        download error, transcribe error) so the operator gets a reply
        instead of a silent drop.
        """
        # Failure mode 1: audio arrives but voice inbound is off entirely
        # (no VoiceProcessor wired, or inbound disabled in config).
        if self._voice is None or not self._config.voice.inbound.enabled:
            self._log.info(
                "voice.inbound.disabled",
                channel="slack",
                conv_key=conv_key,
            )
            return (
                "[voice transcript]: (voice transcription is off — please type "
                "your message instead)"
            )

        message_id = str(event.get("ts", ""))
        mime_type = voice_file.get("mimetype") or "audio/webm"

        try:
            audio_path = await self._download_slack_voice(
                voice_file=voice_file,
                conv_key=conv_key,
                message_id=message_id,
            )
        except Exception as exc:
            self._log.warning(
                "voice.download_failed",
                channel="slack",
                conv_key=conv_key,
                error=str(exc),
            )
            return (
                "[voice transcript]: (couldn't download your voice note — "
                "please try again or type your message)"
            )

        try:
            transcript = await self._voice.transcribe_inbound(
                audio_path=audio_path,
                mime_type=mime_type,
                conv_key=conv_key,
                message_id=message_id,
                duration_seconds=voice_file.get("duration_ms") / 1000.0
                if isinstance(voice_file.get("duration_ms"), (int, float))
                else None,
            )
        except Exception as exc:
            self._log.warning(
                "voice.transcribe_failed",
                channel="slack",
                conv_key=conv_key,
                error=str(exc),
            )
            return (
                "[voice transcript]: (couldn't transcribe your voice note — "
                "please try again or type your message)"
            )

        # Stash the audio path + mime for the outbound TTS path (Pass 3)
        # and mark the conv_key so voice-in can produce voice-out.
        if self._config.voice.inbound.keep_audio_files:
            event["voice_audio_path"] = str(audio_path)
        event["voice_mime_type"] = mime_type
        self._voice.mark_voice_inbound(conv_key=conv_key)

        return f"[voice transcript]: {transcript}"

    async def _download_slack_voice(
        self,
        *,
        voice_file: dict[str, Any],
        conv_key: str,
        message_id: str,
    ) -> Path:
        """Download a Slack voice file to the per-conversation voice dir.

        Uses ``url_private_download`` with a ``Bearer <bot_token>`` header
        (Slack file CDN requires bot-token auth). Writes under
        ``$ANNA_HOME/transcripts/voice/<safe(conv_key)>/<msg_id>.<ext>``
        when ``keep_audio_files`` is true; otherwise to a tempfile the
        VoiceProcessor unlinks after transcribe.
        """
        ext = self._voice_extension(voice_file)
        dest = self._voice_dest_path(
            conv_key=conv_key, message_id=message_id, ext=ext
        )
        content = await self._download_slack_file(voice_file)
        dest.write_bytes(content)
        return dest

    async def _download_slack_file(
        self, file_entry: dict[str, Any], *, max_bytes: int | None = None
    ) -> bytes:
        """Download a Slack ``files[]`` entry and return its raw bytes.

        Uses ``url_private_download`` (falling back to ``url_private``)
        with a ``Bearer <bot_token>`` header — the Slack file CDN
        requires bot-token auth. Shared by the voice and image inbound
        paths; the voice wrapper writes the bytes to a dest path, the
        image path keeps them in memory for base64 encoding.

        Hardening (code-review fast-follow):

        * The download host is validated to be a Slack host BEFORE the
          ``Bearer`` token is attached, so the bot token is never leaked to
          an attacker-controlled URL smuggled into a ``files[]`` entry.
        * Redirects are not followed (``follow_redirects=False``) and a
          non-200 status raises, so a redirect cannot silently return a
          non-image body under an otherwise-valid image MIME.
        * When ``max_bytes`` is provided (the image path passes the
          per-image cap), a ``Content-Length`` larger than the cap aborts
          before the body is returned. The voice path passes ``None`` and
          keeps its prior behavior; the image handler's post-download length
          check remains the authoritative guard.
        """
        url = file_entry.get("url_private_download") or file_entry.get("url_private")
        if not url:
            raise RuntimeError("slack file has no download URL")

        # Never send the bot token to a non-Slack host. Slack serves files
        # from ``files.slack.com`` (and other ``*.slack.com`` CDN hosts).
        host = (urlparse(url).hostname or "").lower()
        if not (host == "files.slack.com" or host.endswith(".slack.com")):
            self._log.warning(
                "channel.download.bad_host", channel="slack", host=host
            )
            raise RuntimeError(
                f"refusing to send bot token to non-Slack host: {host!r}"
            )

        bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {bot_token}"},
            )
            resp.raise_for_status()
            # raise_for_status does not fire on 3xx; with redirects disabled a
            # redirect would otherwise return a redirect body. Require 200.
            status = getattr(resp, "status_code", 200)
            if status != 200:
                raise RuntimeError(f"unexpected status {status} downloading slack file")

            if max_bytes is not None:
                headers = getattr(resp, "headers", {}) or {}
                content_length = headers.get("Content-Length") or headers.get(
                    "content-length"
                )
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except (TypeError, ValueError):
                        declared = None
                    if declared is not None and declared > max_bytes:
                        raise RuntimeError(
                            f"content-length {declared} exceeds cap {max_bytes}"
                        )

            return resp.content

    @staticmethod
    def _voice_extension(voice_file: dict[str, Any]) -> str:
        filetype = voice_file.get("filetype")
        if isinstance(filetype, str) and filetype:
            return filetype.lstrip(".")
        mimetype = voice_file.get("mimetype", "")
        if isinstance(mimetype, str) and "/" in mimetype:
            return mimetype.split("/", 1)[1] or "webm"
        return "webm"

    def _voice_dest_path(
        self, *, conv_key: str, message_id: str, ext: str
    ) -> Path:
        if self._config.voice.inbound.keep_audio_files:
            safe = conv_key.replace(":", "-").replace("/", "_")
            voice_dir = self._config.transcripts_dir / "voice" / safe
            voice_dir.mkdir(parents=True, exist_ok=True)
            return voice_dir / f"{message_id}.{ext}"
        import tempfile

        fd, name = tempfile.mkstemp(suffix=f".{ext}", prefix="anna-voice-")
        os.close(fd)
        return Path(name)

    # ------------------------------------------------------------------
    # Phase 2.6 image inbound
    # ------------------------------------------------------------------

    def _detect_image_files(
        self,
        event: dict[str, Any],
        *,
        voice_file: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Classify every ``image/*`` ``files[]`` entry for this event.

        Returns a list of decision records, one per image attachment, in
        delivery order. Each record is ``{"entry", "mime", "status"}``
        where ``status`` is:

        * ``"ok"`` — supported subtype within the per-turn image count;
        * ``"unsupported"`` — an ``image/*`` subtype the model cannot
          view (svg+xml, heic/heif, tiff, bmp, …);
        * ``"overflow"`` — supported but beyond ``max_images``.

        The entry matched as the voice file (if any) is skipped — voice
        is exclusive, so in practice this is defensive. Size and total
        caps are NOT applied here; :meth:`_handle_image_inbound` owns
        those (it needs the downloaded byte length).
        """
        files = event.get("files")
        if not isinstance(files, list):
            return []

        voice_id = (
            voice_file.get("id") if isinstance(voice_file, dict) else None
        )
        max_images = self._config.images.inbound.max_images

        records: list[dict[str, Any]] = []
        accepted = 0
        for entry in files:
            if not isinstance(entry, dict):
                continue
            mimetype = entry.get("mimetype", "")
            if not (isinstance(mimetype, str) and mimetype.startswith("image/")):
                continue
            if voice_id is not None and entry.get("id") == voice_id:
                continue
            if mimetype not in _SUPPORTED_IMAGE_MIMES:
                records.append(
                    {"entry": entry, "mime": mimetype, "status": "unsupported"}
                )
                continue
            if accepted >= max_images:
                records.append(
                    {"entry": entry, "mime": mimetype, "status": "overflow"}
                )
                continue
            accepted += 1
            records.append({"entry": entry, "mime": mimetype, "status": "ok"})
        return records

    async def _handle_image_inbound(
        self,
        *,
        event: dict[str, Any],
        image_files: list[dict[str, Any]],
        conv_key: str,
        caption: str,
    ) -> tuple[str, list[ImageAttachment]]:
        """Download accepted inbound images and build the carried text.

        Mirrors the voice graceful-failure pattern: every skip or failure
        appends an operator-facing marker to the text so the model still
        gets a turn and the operator gets feedback — images are never
        silently dropped. Returns ``(text, images)`` where ``text`` is the
        caption (or ``"[image]"`` when a caption-less turn carried at least
        one accepted image) with any markers appended, and ``images`` is the
        list of successfully downloaded attachments.
        """
        cfg = self._config.images.inbound

        # Failure mode: images arrive but image understanding is off. No
        # download — return the marker only.
        if not cfg.enabled:
            self._log.info(
                "image.inbound.disabled",
                channel="slack",
                conv_key=conv_key,
                count=len(image_files),
            )
            return (
                self._compose_image_text(
                    caption,
                    ["[image received — image understanding is off]"],
                    accepted=0,
                ),
                [],
            )

        markers: list[str] = []
        images: list[ImageAttachment] = []
        total = 0

        for record in image_files:
            status = record["status"]
            entry = record["entry"]
            mime = record["mime"]

            if status == "unsupported":
                self._log.warning(
                    "image.unsupported",
                    channel="slack",
                    conv_key=conv_key,
                    mime=mime,
                )
                markers.append(
                    f"[unsupported image type: {mime} — please send "
                    "PNG/JPG/GIF/WebP]"
                )
                continue

            if status == "overflow":
                markers.append("[some images were skipped (limit reached)]")
                continue

            # Pre-check the Slack-reported size BEFORE downloading so a
            # wildly oversize image never hits the CDN.
            size = entry.get("size")
            if isinstance(size, int) and size > cfg.max_image_size_bytes:
                self._log.warning(
                    "image.oversize",
                    channel="slack",
                    conv_key=conv_key,
                    size=size,
                    stage="pre",
                )
                markers.append(f"[image too large ({size} bytes) — skipped]")
                continue

            try:
                data = await self._download_slack_file(
                    entry, max_bytes=cfg.max_image_size_bytes
                )
            except Exception as exc:
                self._log.warning(
                    "image.download_failed",
                    channel="slack",
                    conv_key=conv_key,
                    error=str(exc),
                )
                markers.append("[couldn't download an image — please try again]")
                continue

            # Post-check the actual byte length (Slack's ``size`` field can
            # be absent or stale).
            n = len(data)
            if n > cfg.max_image_size_bytes:
                self._log.warning(
                    "image.oversize",
                    channel="slack",
                    conv_key=conv_key,
                    size=n,
                    stage="post",
                )
                markers.append(f"[image too large ({n} bytes) — skipped]")
                continue

            # Aggregate cap across all accepted images on this turn.
            if total + n > cfg.max_total_bytes:
                self._log.warning(
                    "image.oversize",
                    channel="slack",
                    conv_key=conv_key,
                    size=n,
                    total=total,
                    stage="total",
                )
                markers.append("[some images were skipped (limit reached)]")
                continue

            total += n
            images.append(ImageAttachment(media_type=mime, data=data))

        return (
            self._compose_image_text(caption, markers, accepted=len(images)),
            images,
        )

    @staticmethod
    def _compose_image_text(
        caption: str, markers: list[str], *, accepted: int
    ) -> str:
        """Build the inbound text for an image turn.

        A present caption is preserved as the lead line. A caption-less
        turn that still landed at least one image gets a ``"[image]"``
        placeholder so both the transcript line and the SDK text block are
        non-empty. Markers (deduped, order-preserved) follow on their own
        lines so the operator sees any skip/failure feedback.
        """
        parts: list[str] = []
        caption = (caption or "").strip()
        if caption:
            parts.append(caption)
        elif accepted >= 1:
            parts.append("[image]")

        seen: set[str] = set()
        for marker in markers:
            if marker not in seen:
                seen.add(marker)
                parts.append(marker)

        return "\n".join(p for p in parts if p)

    @classmethod
    def conversation_key_for(cls, event: Any) -> str:
        """Map a raw Slack event dict to the canonical conversation_key."""
        if not isinstance(event, dict):
            raise TypeError("Slack event must be a dict")

        channel_type = event.get("channel_type", "")
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts")
        event_ts = event.get("ts", "")
        user_id = event.get("user", "")
        event_type = event.get("type", "")

        if channel_type == "im":
            return f"slack:dm:{user_id}"
        if thread_ts:
            return f"slack:ch:{channel_id}:{thread_ts}"
        if event_type == "app_mention":
            return f"slack:ch:{channel_id}:{event_ts}:oneshot"
        # Fallback: treat top-level channel messages as one-shot.
        return f"slack:ch:{channel_id}:{event_ts}:oneshot"

    # ------------------------------------------------------------------
    # Visibility hooks — Slack reactions as a "thinking" signal
    # ------------------------------------------------------------------

    async def start_thinking_signal(
        self, event: InboundEvent
    ) -> SignalHandle | None:
        """Post a Slack reaction on the inbound message as a "working" signal.

        Reads ``channel`` and ``ts`` straight off ``event.raw`` — the
        full Slack event dict is stashed there by
        :meth:`_to_inbound_event`. Both DM (``channel_type == "im"``)
        and channel-thread shapes carry ``channel`` and ``ts`` directly
        so the same call shape covers every conv_key variant.

        Emoji name is sourced from
        ``config.runtime.visibility.slack_emoji`` so the operator can
        swap it without code edits. Failures (network drop, 429,
        missing emoji on workspace) log a warning and return ``None``;
        the SDK turn continues uninterrupted.
        """

        channel = event.raw.get("channel") if event.raw else None
        ts = event.raw.get("ts") if event.raw else None
        if not channel or not ts:
            self._log.debug(
                "visibility.slack.reaction_skipped",
                conv_key=event.conversation_key,
                reason="missing_channel_or_ts",
            )
            return None

        emoji = self._config.runtime.visibility.slack_emoji or "thinking_face"

        try:
            await self._client.reactions_add(
                channel=channel, timestamp=ts, name=emoji
            )
        except Exception as exc:
            self._log.warning(
                "visibility.slack.reaction_add_failed",
                conv_key=event.conversation_key,
                channel=channel,
                ts=ts,
                emoji=emoji,
                error=str(exc),
            )
            return None

        return SignalHandle(
            transport="slack",
            conv_key=event.conversation_key,
            slack_channel=channel,
            slack_ts=ts,
            slack_emoji=emoji,
        )

    async def clear_thinking_signal(self, handle: SignalHandle) -> None:
        """Remove the Slack reaction posted by ``start_thinking_signal``.

        Exception-isolated: Slack returns ``reaction_not_found`` if the
        reaction was already cleared (e.g. by an operator removing it
        manually), and a network error here would otherwise propagate
        into the worker's ``finally`` block. Both fail at debug level —
        a dangling reaction is harmless, slightly ugly.
        """

        channel = handle.slack_channel
        ts = handle.slack_ts
        emoji = handle.slack_emoji
        if not channel or not ts or not emoji:
            self._log.debug(
                "visibility.slack.reaction_clear_skipped",
                conv_key=handle.conv_key,
                reason="missing_handle_fields",
            )
            return None

        try:
            await self._client.reactions_remove(
                channel=channel, timestamp=ts, name=emoji
            )
        except Exception as exc:
            self._log.debug(
                "visibility.slack.reaction_remove_failed",
                conv_key=handle.conv_key,
                channel=channel,
                ts=ts,
                emoji=emoji,
                error=str(exc),
            )
        return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            response = await self._client.auth_test()
            return bool(response.get("ok", False))
        except Exception as exc:
            self._log.warning("channel.health_check_failed", channel="slack", error=str(exc))
            return False
