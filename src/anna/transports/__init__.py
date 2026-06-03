"""ChannelAdapter plugin discovery.

Phase 1 ships Slack and Telegram adapters. Future transports drop into this
directory and the setup wizard discovers them automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anna.config import AnnaConfig
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage
from anna.transports.cli import CLIAdapter
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation
from anna.transports.telegram import TelegramAdapter

if TYPE_CHECKING:  # pragma: no cover - import-only for typing
    from anna.runtime.voice import VoiceProcessor

__all__ = [
    "ChannelAdapter",
    "CLIAdapter",
    "InboundEvent",
    "OutboundMessage",
    "SlackAdapter",
    "TelegramAdapter",
    "ThreadParticipation",
    "build_enabled_adapters",
]


def build_enabled_adapters(
    config: AnnaConfig,
    *,
    voice: VoiceProcessor | None = None,
) -> dict[str, ChannelAdapter]:
    """Instantiate the adapters the operator enabled in anna.yaml.

    Returns a dict keyed by ``adapter.name`` so the router can map an inbound
    event back to its source.

    Adapter-internal dependencies (like Slack's thread-participation
    state) are constructed here too. Lazy I/O — the participation file
    is read inside :meth:`SlackAdapter.start` rather than here, mirroring
    the google_clients pattern in ``__main__.py``.

    ``voice`` is the process-wide :class:`~anna.runtime.voice.VoiceProcessor`
    (Phase 2.5), threaded into the Slack and Telegram adapters so they can
    transcribe inbound voice notes and synthesize outbound replies. It is
    ``None`` when voice is fully off (or for callers/tests that don't wire
    it); the adapters guard all voice logic on a non-``None`` value, so the
    default keeps every existing call site working unchanged. The CLI
    adapter is text-only and never receives it.
    """
    adapters: dict[str, ChannelAdapter] = {}
    if config.transports.slack.enabled:
        thread_participation = ThreadParticipation(
            state_path=config.anna_home / "state" / "slack_thread_participation.jsonl",
        )
        adapters["slack"] = SlackAdapter(
            config=config,
            thread_participation=thread_participation,
            voice=voice,
        )
    if config.transports.telegram.enabled:
        adapters["telegram"] = TelegramAdapter(config=config, voice=voice)
    if config.transports.cli.enabled:
        adapters["cli"] = CLIAdapter(config=config)
    return adapters
