"""Slack-alerts MCP server.

A tightly-scoped in-process MCP server that lets ANNA post arbitrary text
to a Slack channel through her OWN Slack adapter — the same delivery path
:class:`anna.runtime.alerter.AdminAlerter` uses. Because it rides the
already-connected adapter rather than an interactively-authenticated MCP
server, it works in headless/scheduled runs where no operator is present.

The single tool, ``slack_post``:

* Resolves the target channel from the ``channel_id`` argument, falling
  back to ``config.reports.slack_channel_id`` when the argument is empty.
* Sends an :class:`~anna.transports.base.OutboundMessage` keyed
  ``slack:dm:<channel>`` — the no-thread form that posts a fresh message
  to a channel (AdminAlerter uses exactly this key shape).
* Emits an ``audit.slack_post.dispatched`` event so the operator can
  reconstruct what ANNA posted.
* Returns a short text response (errors are returned as text, not raised,
  so the model can react instead of crashing the turn).

The server is instantiated per-worker by :class:`ConversationWorker`,
which passes its conversation_key as ``conv_key`` context so audit events
are stamped with the right caller.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.transports.base import ChannelAdapter, OutboundMessage


SLACK_ALERTS_TOOL_NAMES: tuple[str, ...] = ("slack_post",)


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class SlackAlertTools:
    """Bundles the config and adapter handles for one worker's Slack-post tool.

    The MCP tool factory in :func:`build_slack_alerts_server` closes over an
    instance of this class so the tool call sees the same config and adapter
    map the worker was constructed with.
    """

    def __init__(
        self,
        config: AnnaConfig,
        adapters: dict[str, ChannelAdapter],
    ) -> None:
        self.config = config
        self.adapters = adapters
        self._log = get_logger("anna.tools.slack_alerts")

    async def slack_post(
        self,
        *,
        text: str,
        channel_id: str = "",
        conv_key: str,
    ) -> dict[str, Any]:
        # Refuse to post an empty body — Slack rejects it anyway and a blank
        # post is never the intent. Cheap guard, do it first.
        if not text.strip():
            self._log.warning("slack_post.empty_text", conv_key=conv_key)
            return _text_response(
                "slack_post failed: text is empty. Pass a non-empty message body."
            )

        # Resolve the destination: an explicit channel_id wins, otherwise
        # fall back to the configured reports channel. Both empty is an
        # operator-config error — return text, don't raise.
        channel = (channel_id or "").strip() or self.config.reports.slack_channel_id
        if not channel:
            self._log.warning("slack_post.no_channel", conv_key=conv_key)
            return _text_response(
                "slack_post failed: no channel_id given and "
                "reports.slack_channel_id is unset. Pass a channel_id or set "
                "reports.slack_channel_id (or ANNA_REPORTS_SLACK_CHANNEL_ID)."
            )

        # The admin alert channel is reserved for AdminAlerter traffic — model
        # posts there would dilute or spoof operator alerts. Mirror the guard
        # ScheduleStore._check_destination uses for scheduled output.
        admin_channel = self.config.admin.slack_channel_id
        if admin_channel and channel == admin_channel:
            self._log.warning(
                "slack_post.admin_channel_refused", conv_key=conv_key, channel=channel
            )
            return _text_response(
                f"slack_post failed: refusing to post to the admin alert channel "
                f"{channel}. That channel is reserved for AdminAlerter traffic; "
                f"pick a different channel."
            )

        adapter = self.adapters.get("slack")
        if adapter is None:
            self._log.warning("slack_post.no_adapter", conv_key=conv_key)
            return _text_response(
                "slack_post failed: the Slack transport is not connected, so "
                "there is no adapter to post through."
            )

        # ``slack:dm:<channel>`` posts a fresh message to the channel with no
        # thread — the same conversation_key shape AdminAlerter uses for
        # one-shot admin posts. The adapter re-raises on a Slack API failure,
        # so we guard the send: a headless turn must survive a failed post
        # instead of crashing. Mirror AdminAlerter._dispatch's try/except.
        try:
            await adapter.send(
                OutboundMessage(
                    conversation_key=f"slack:dm:{channel}",
                    text=text,
                )
            )
        except Exception as exc:
            audit_event(
                "audit.slack_post.failed",
                audit_dir=self.config.audit_dir,
                actor="anna",
                conv_key=conv_key,
                fsync_on_write=self.config.logging.audit.fsync_on_write,
                channel=channel,
                error=str(exc),
            )
            self._log.error(
                "slack_post.failed", channel=channel, error=str(exc)
            )
            return _text_response(f"Slack post to {channel} failed: {exc}")

        audit_event(
            "audit.slack_post.dispatched",
            audit_dir=self.config.audit_dir,
            actor="anna",
            conv_key=conv_key,
            fsync_on_write=self.config.logging.audit.fsync_on_write,
            channel=channel,
            text_len=len(text),
        )
        self._log.info("slack_post.dispatched", channel=channel, text_len=len(text))
        return _text_response(f"posted to Slack channel {channel}")


def build_slack_alerts_server(*, tools: SlackAlertTools, conv_key: str) -> Any:
    """Construct the per-worker Slack-alerts MCP server.

    The ``conv_key`` is captured in the tool closure so every audit event
    fired by the tool is stamped with the right caller without the SDK
    having to pass it explicitly.
    """

    @tool(
        "slack_post",
        "Post a message to a Slack channel through ANNA's own Slack adapter "
        "(works in headless/scheduled runs). channel_id is optional: pass an "
        "empty string to fall back to the configured reports channel "
        "(reports.slack_channel_id). text is the message body and must be "
        "non-empty. The admin alert channel is reserved and will be rejected.",
        {
            "channel_id": str,
            "text": str,
        },
    )
    async def _slack_post(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.slack_post(
            text=args["text"],
            channel_id=args.get("channel_id") or "",
            conv_key=conv_key,
        )

    return create_sdk_mcp_server(
        name="anna_slack_alerts",
        version="1.0.0",
        tools=[_slack_post],
    )
