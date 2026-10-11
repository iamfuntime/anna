"""Tests for anna.runtime.delivered_posts.

A post delivered into a conversation from OUTSIDE that conversation's own
worker (a scheduled run's final reply, the ``slack_post`` tool) must be
appended to that conversation's transcript as an ``outbound`` row tagged
with its origin. These tests cover the destination -> conversation-key
mapping and the row write itself; the scheduler and ``slack_post`` call
sites are covered in test_scheduler.py and test_slack_alerts_server.py.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from anna.config import AnnaConfig, IdentityAliasEntry
from anna.log import _lock_for
from anna.runtime.delivered_posts import (
    conv_key_for_destination,
    record_delivered_post,
)
from anna.runtime.router import ConversationRouter
from anna.runtime.supervisor import Supervisor
from anna.transports.base import InboundEvent


def _make_config(
    tmp_path: Path, identities: list[IdentityAliasEntry] | None = None
) -> AnnaConfig:
    cfg = AnnaConfig()
    if identities is not None:
        cfg.identities = identities
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    return cfg


def _today_file(cfg: AnnaConfig, safe_key: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return cfg.transcripts_dir / safe_key / f"{today}.jsonl"


def _read_rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Destination -> conversation key
# ---------------------------------------------------------------------------


def test_slack_user_id_maps_to_dm_conv_key(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert (
        conv_key_for_destination(cfg, transport="slack", channel="USP2QLB41")
        == "slack:dm:USP2QLB41"
    )
    # Enterprise Grid user IDs start with W.
    assert (
        conv_key_for_destination(cfg, transport="slack", channel="W012ABCDE")
        == "slack:dm:W012ABCDE"
    )


def test_slack_channel_and_dm_channel_ids_have_no_conv_key(tmp_path: Path) -> None:
    """A shared channel is keyed per thread and a ``D…`` DM channel ID is not
    the user ID the DM conversation is keyed by — neither maps."""
    cfg = _make_config(tmp_path)
    for channel in ("C07MLJPHX9V", "G012PRIVATE", "D0123DMCHAN", "", "   "):
        assert (
            conv_key_for_destination(cfg, transport="slack", channel=channel) is None
        ), channel


def test_telegram_private_chat_maps_and_group_does_not(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert (
        conv_key_for_destination(cfg, transport="telegram", channel="123456789")
        == "telegram:dm:123456789"
    )
    assert (
        conv_key_for_destination(cfg, transport="telegram", channel="-1001234567")
        is None
    )


def test_unknown_transport_has_no_conv_key(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert conv_key_for_destination(cfg, transport="cli", channel="seth") is None


def test_identity_alias_matches_router_normalization(tmp_path: Path) -> None:
    """The recorded key must be the one the router rewrites the operator's
    inbound to, or the row lands in a transcript no worker ever reads."""
    identities = [
        IdentityAliasEntry(
            canonical="seth", slack_user_id="USP2QLB41", telegram_chat_id="123456789"
        )
    ]
    cfg = _make_config(tmp_path, identities)
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    router = ConversationRouter(
        config=cfg, supervisor=Supervisor(config=cfg), adapters={}
    )

    for transport, ident in (("slack", "USP2QLB41"), ("telegram", "123456789")):
        inbound = InboundEvent(
            transport=transport,
            conversation_key=f"{transport}:dm:{ident}",
            sender_id=ident,
            sender_display="Seth",
            text="hello",
            is_dm=True,
            is_thread=False,
        )
        expected = router._normalize_conv_key(inbound)
        assert expected == "user:seth"
        assert (
            conv_key_for_destination(cfg, transport=transport, channel=ident)
            == expected
        )

    # Transport-scoped: a Slack user ID that is not the aliased one keeps its
    # per-transport key.
    assert (
        conv_key_for_destination(cfg, transport="slack", channel="UOTHER")
        == "slack:dm:UOTHER"
    )


# ---------------------------------------------------------------------------
# Row write
# ---------------------------------------------------------------------------


def test_record_appends_outbound_row_tagged_with_source(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    recorded = record_delivered_post(
        cfg,
        transport="slack",
        channel="USP2QLB41",
        text="DocuSign: the offer letter was signed.",
        source="schedule:heartbeat",
    )

    assert recorded == "slack:dm:USP2QLB41"
    rows = _read_rows(_today_file(cfg, "slack-dm-USP2QLB41"))
    assert len(rows) == 1
    row = rows[0]
    # Same shape as the router's outbound line, plus the origin tag.
    assert row["direction"] == "outbound"
    assert row["conv_key"] == "slack:dm:USP2QLB41"
    assert row["text"] == "DocuSign: the offer letter was signed."
    assert row["source"] == "schedule:heartbeat"
    assert row["ts"].endswith("Z")


def test_record_appends_after_existing_rows(tmp_path: Path) -> None:
    """The write is an append to the day's file — earlier lines survive."""
    cfg = _make_config(tmp_path)
    path = _today_file(cfg, "slack-dm-USP2QLB41")
    path.parent.mkdir(parents=True)
    earlier = {
        "ts": "2026-10-07T14:00:00.000Z",
        "direction": "inbound",
        "conv_key": "slack:dm:USP2QLB41",
        "text": "earlier message",
    }
    path.write_text(json.dumps(earlier) + "\n", encoding="utf-8")

    record_delivered_post(
        cfg,
        transport="slack",
        channel="USP2QLB41",
        text="scheduled alert",
        source="schedule:heartbeat",
    )

    rows = _read_rows(path)
    assert [r["text"] for r in rows] == ["earlier message", "scheduled alert"]


def test_record_writes_under_alias_key(tmp_path: Path) -> None:
    cfg = _make_config(
        tmp_path, [IdentityAliasEntry(canonical="seth", slack_user_id="USP2QLB41")]
    )

    recorded = record_delivered_post(
        cfg,
        transport="slack",
        channel="USP2QLB41",
        text="scheduled alert",
        source="schedule:heartbeat",
    )

    assert recorded == "user:seth"
    rows = _read_rows(_today_file(cfg, "user-seth"))
    assert rows[0]["conv_key"] == "user:seth"
    assert not (cfg.transcripts_dir / "slack-dm-USP2QLB41").exists()


def test_record_skips_destination_without_conv_key(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    recorded = record_delivered_post(
        cfg,
        transport="slack",
        channel="C07MLJPHX9V",
        text="channel report",
        source="schedule:morning-brief",
    )

    assert recorded is None
    assert not cfg.transcripts_dir.exists()


def test_record_skips_blank_text(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    for text in ("", "   ", "\n\t"):
        assert (
            record_delivered_post(
                cfg,
                transport="slack",
                channel="USP2QLB41",
                text=text,
                source="schedule:heartbeat",
            )
            is None
        )
    assert not cfg.transcripts_dir.exists()


def test_record_respects_per_conversation_transcript_lock(tmp_path: Path) -> None:
    """The row goes through ``anna.log.transcript_event``, so it waits on the
    same per-conv_key write lock as the router's inbound/outbound lines and
    cannot interleave with a line being written for that conversation."""
    cfg = _make_config(tmp_path)
    path = _today_file(cfg, "slack-dm-ULOCKTEST")
    done = threading.Event()

    def _record() -> None:
        record_delivered_post(
            cfg,
            transport="slack",
            channel="ULOCKTEST",
            text="scheduled alert",
            source="schedule:heartbeat",
        )
        done.set()

    lock = _lock_for("slack:dm:ULOCKTEST")
    lock.acquire()
    try:
        thread = threading.Thread(target=_record)
        thread.start()
        # Held lock -> the writer is parked and nothing has reached the file.
        assert not done.wait(timeout=0.3)
        assert not path.exists() or path.read_text(encoding="utf-8") == ""
    finally:
        lock.release()
    thread.join(timeout=5)
    assert done.is_set()
    assert [r["text"] for r in _read_rows(path)] == ["scheduled alert"]
