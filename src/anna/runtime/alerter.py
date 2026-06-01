"""Out-of-band operator alerter.

Per v3 §5.5 (test plan). When a transport fails repeatedly and the watchdog
restarts it, the operator should learn about it on the *other* transport.
When the SDK itself fails (auth, model unavailable), both channels are
notified because we cannot tell which side is healthy.

The alerter holds the adapters dict and a config object on construction.
At alert time it pings each candidate adapter, picks the first one whose
ping succeeds, and sends a message to the operator's configured admin
destination (Slack channel ID or Telegram chat ID).
"""

from __future__ import annotations

from typing import Iterable

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.transports.base import ChannelAdapter, OutboundMessage


class AdminAlerter:
    """Routes operator alerts to a surviving transport.

    The ``warn`` and ``critical`` methods both follow the same pattern:
    walk the adapters (optionally skipping one the caller knows is
    broken), find the first one that pings healthy AND has an admin
    destination configured, and send the message.
    """

    def __init__(
        self,
        *,
        config: AnnaConfig,
        adapters: dict[str, ChannelAdapter],
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._log = get_logger("anna.alerter")

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def warn(self, message: str, *, exclude_channel: str | None = None) -> bool:
        """Send a non-critical operator alert. Returns True if delivered."""
        return await self._dispatch(message=message, level="WARNING", exclude_channel=exclude_channel)

    async def critical(self, message: str, *, exclude_channel: str | None = None) -> bool:
        """Send a critical operator alert prefixed with ``[CRITICAL]``."""
        prefixed = f"[CRITICAL] {message}"
        return await self._dispatch(message=prefixed, level="CRITICAL", exclude_channel=exclude_channel)

    async def notify_startup(self, message: str) -> bool:
        """Send a boot-time alert tagged ``STARTUP`` in the audit log.

        Same routing as :meth:`warn` (first healthy adapter with a
        destination wins); the distinct level lets the audit log
        distinguish startup pings from transport-failure warnings.
        """
        return await self._dispatch(message=message, level="STARTUP", exclude_channel=None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        *,
        message: str,
        level: str,
        exclude_channel: str | None,
    ) -> bool:
        for name in self._candidate_order(exclude=exclude_channel):
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            destination = self._destination_for(name)
            if not destination:
                self._log.warning(
                    "alerter.no_destination",
                    transport=name,
                    note="admin destination unset for this transport — skipping",
                )
                continue
            healthy = False
            try:
                healthy = await adapter.health_check()
            except Exception as exc:
                self._log.warning(
                    "alerter.ping_failed",
                    transport=name,
                    error=str(exc),
                )
            if not healthy:
                continue
            try:
                await adapter.send(
                    OutboundMessage(
                        conversation_key=self._conv_key_for(name, destination),
                        text=message,
                    )
                )
            except Exception as exc:
                self._log.error(
                    "alerter.send_failed",
                    transport=name,
                    destination=destination,
                    error=str(exc),
                )
                continue
            audit_event(
                "audit.alerter.dispatched",
                audit_dir=self._config.audit_dir,
                actor="anna",
                fsync_on_write=self._config.logging.audit.fsync_on_write,
                level=level,
                transport=name,
                destination=destination,
                message_len=len(message),
            )
            self._log.info(
                "alerter.dispatched",
                transport=name,
                destination=destination,
                level=level,
            )
            return True

        self._log.error(
            "alerter.no_surviving_channel",
            level=level,
            attempted=list(self._candidate_order(exclude=exclude_channel)),
        )
        return False

    def _candidate_order(self, *, exclude: str | None) -> Iterable[str]:
        # Deterministic order so the operator sees consistent behavior.
        for name in ("slack", "telegram"):
            if name == exclude:
                continue
            if name in self._adapters:
                yield name

    def _destination_for(self, transport: str) -> str:
        if transport == "slack":
            return self._config.admin.slack_channel_id
        if transport == "telegram":
            return self._config.admin.telegram_chat_id
        return ""

    @staticmethod
    def _conv_key_for(transport: str, destination: str) -> str:
        # Synthesize a conv_key that the adapter's _channel_and_thread_for /
        # _chat_and_topic_for helpers can decode. For Slack admin alerts we
        # treat the channel as a one-shot post (no thread), and for Telegram
        # the destination is a chat_id.
        if transport == "slack":
            # Slack adapter understands ``slack:ch:<channel>:<ts>`` and
            # ``slack:dm:<user>``. The admin channel is a regular channel,
            # so we wrap as a ch key with a placeholder ts; the adapter
            # falls into the channel branch and uses thread_ts as the
            # placeholder. To avoid sending into a fake thread we instead
            # use the dm form which takes ``channel`` verbatim.
            return f"slack:dm:{destination}"
        if transport == "telegram":
            return f"telegram:dm:{destination}"
        return f"{transport}:dm:{destination}"
