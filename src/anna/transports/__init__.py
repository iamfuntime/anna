"""ChannelAdapter plugin discovery.

Phase 1 ships Slack and Telegram adapters. Future transports drop into this
directory and the setup wizard discovers them automatically.
"""

from __future__ import annotations

from anna.config import AnnaConfig
from anna.transports.base import ChannelAdapter, InboundEvent, OutboundMessage
from anna.transports.slack import SlackAdapter
from anna.transports.telegram import TelegramAdapter

__all__ = [
    "ChannelAdapter",
    "InboundEvent",
    "OutboundMessage",
    "SlackAdapter",
    "TelegramAdapter",
    "build_enabled_adapters",
]


def build_enabled_adapters(config: AnnaConfig) -> dict[str, ChannelAdapter]:
    """Instantiate the adapters the operator enabled in anna.yaml.

    Returns a dict keyed by ``adapter.name`` so the router can map an inbound
    event back to its source.
    """
    adapters: dict[str, ChannelAdapter] = {}
    if config.transports.slack.enabled:
        adapters["slack"] = SlackAdapter(config=config)
    if config.transports.telegram.enabled:
        adapters["telegram"] = TelegramAdapter(config=config)
    return adapters
