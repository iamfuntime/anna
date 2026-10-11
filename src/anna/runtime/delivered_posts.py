"""Record out-of-band posts into the destination conversation's transcript.

The router writes a transcript line for every message that flows through a
conversation's own worker (see :meth:`ConversationRouter.dispatch` and
``_send_factory``). Two delivery paths bypass it and call
``adapter.send`` directly:

* the scheduler, routing a scheduled run's final reply to the schedule's
  destination (:meth:`anna.runtime.scheduler.Scheduler._send_to_destination`);
* the ``slack_post`` tool
  (:meth:`anna.tools.slack_alerts_server.SlackAlertTools.slack_post`).

Both run under some OTHER conversation key (``schedule:<id>:<date>`` for a
scheduled run), so the conversation the message actually landed in never
learned it had been sent. On 2026-10-07 the ``heartbeat`` schedule posted an
alert to the operator's DM, the operator replied 17 seconds later, and the
DM's worker spawned with no record of the post — the reply had no referent.

:func:`record_delivered_post` closes that gap: after a successful send it
appends an ``outbound`` row, tagged with a ``source`` field naming its
origin, to the destination conversation's transcript through the normal
:func:`anna.log.transcript_event` writer. The resume path
(:mod:`anna.vault.transcript_resume`) then folds the row into the next
worker's "Unsaved conversation tail" block, labelled by origin.

Only destinations that map to exactly one conversation key are recorded —
today that is a DM. See :func:`conv_key_for_destination`.
"""

from __future__ import annotations

from anna.config import AnnaConfig
from anna.log import get_logger, transcript_event

_log = get_logger("anna.delivered_posts")

# Slack user IDs start with ``U`` (or ``W`` on Enterprise Grid). Posting to a
# bare user ID opens the DM with that user, and that user's inbound DM
# messages key as ``slack:dm:<user_id>``.
_SLACK_USER_ID_PREFIXES = ("U", "W")


def conv_key_for_destination(
    config: AnnaConfig, *, transport: str, channel: str
) -> str | None:
    """Map a post destination to the conversation key replies will arrive on.

    ``channel`` is the raw destination ID the delivery paths wrap as
    ``<transport>:dm:<channel>`` for ``adapter.send`` — a Slack user or
    channel ID, or a Telegram chat ID. Returns the conversation key a human
    reply at that destination is routed to, or ``None`` when there is no
    single such conversation:

    * Slack user ID (``U…`` / ``W…``) -> ``slack:dm:<user_id>``.
    * Telegram private chat (positive numeric chat ID) ->
      ``telegram:dm:<chat_id>``.
    * Slack channel (``C…`` / ``G…``): ``None``. Channel conversations are
      keyed per thread (``slack:ch:<channel>:<thread_ts>``), and the thread
      a reply to this post would open is keyed by the post's own ``ts``,
      which ``adapter.send`` does not return.
    * Slack DM channel ID (``D…``): ``None``. The DM conversation is keyed
      by USER ID and resolving ``D…`` to its user needs a Slack API call.
    * Telegram group (negative chat ID) and anything unrecognized: ``None``.

    The identity alias is applied last, mirroring
    :meth:`ConversationRouter._normalize_conv_key`: when the DM identifier
    matches a configured ``identities`` entry the key becomes
    ``user:<canonical>``, because that is the key the router rewrites the
    operator's inbound to.
    """
    ident = (channel or "").strip()
    if not ident:
        return None

    if transport == "slack":
        if not ident.startswith(_SLACK_USER_ID_PREFIXES):
            return None
        for entry in config.identities:
            if entry.slack_user_id == ident:
                return f"user:{entry.canonical}"
        return f"slack:dm:{ident}"

    if transport == "telegram":
        if not ident.isdigit():
            return None
        for entry in config.identities:
            if entry.telegram_chat_id == ident:
                return f"user:{entry.canonical}"
        return f"telegram:dm:{ident}"

    return None


def record_delivered_post(
    config: AnnaConfig,
    *,
    transport: str,
    channel: str,
    text: str,
    source: str,
) -> str | None:
    """Append a delivered post to its destination conversation's transcript.

    Call this ONLY after ``adapter.send`` returned for a message that really
    went out — never for a suppressed run (quiet sentinel, blank output,
    leaked markup) and never for a failed send. ``source`` names the origin
    and is stored verbatim on the row: ``schedule:<schedule_id>`` for a
    scheduled run's final reply, ``slack_post:<caller conv_key>`` for the
    ``slack_post`` tool.

    Returns the conversation key the row was written under, or ``None`` when
    nothing was recorded (blank text, or a destination with no single
    conversation — see :func:`conv_key_for_destination`). The write itself
    goes through :func:`anna.log.transcript_event`, so it shares the
    per-conversation write lock and the row shape of every other transcript
    line, and a filesystem failure is logged there rather than raised.
    """
    if not text or not text.strip():
        return None
    conv_key = conv_key_for_destination(config, transport=transport, channel=channel)
    if conv_key is None:
        _log.debug(
            "delivered_post.not_recorded",
            transport=transport,
            channel=channel,
            source=source,
            reason="destination has no single conversation key",
        )
        return None
    transcript_event(
        channel=transport,
        conv_key=conv_key,
        transcripts_dir=config.transcripts_dir,
        direction="outbound",
        text=text,
        source=source,
    )
    _log.info(
        "delivered_post.recorded",
        transport=transport,
        conv_key=conv_key,
        source=source,
        text_len=len(text),
    )
    return conv_key
