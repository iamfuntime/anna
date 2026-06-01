"""ChannelAdapter plugin discovery.

Phase 1 ships Slack and Telegram adapters. Future transports drop into this
directory and the setup wizard discovers them automatically.
"""

from __future__ import annotations

from anna.config import AnnaConfig
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation
from anna.transports.telegram import TelegramAdapter

__all__ = [
    "ChannelAdapter",
    "InboundEvent",
    "OutboundMessage",
    "SlackAdapter",
    "TelegramAdapter",
    "ThreadParticipation",
    "build_enabled_adapters",
]


def build_enabled_adapters(config: AnnaConfig) -> dict[str, ChannelAdapter]:
    """Instantiate the adapters the operator enabled in anna.yaml.

    Returns a dict keyed by ``adapter.name`` so the router can map an inbound
    event back to its source.

    Adapter-internal dependencies (like Slack's thread-participation
    state) are constructed here too. Lazy I/O — the participation file
    is read inside :meth:`SlackAdapter.start` rather than here, mirroring
    the google_clients pattern in ``__main__.py``.
    """
    adapters: dict[str, ChannelAdapter] = {}
    if config.transports.slack.enabled:
        thread_participation = ThreadParticipation(
            state_path=config.anna_home / "state" / "slack_thread_participation.jsonl",
        )
        adapters["slack"] = SlackAdapter(
            config=config,
            thread_participation=thread_participation,
        )
    if config.transports.telegram.enabled:
        adapters["telegram"] = TelegramAdapter(config=config)
    return adapters
