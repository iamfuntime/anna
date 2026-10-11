"""Validate that the worker's system prompt includes recent checkpoints.

Per v3 §6, the worker reads the two most recent checkpoint files for the
conversation key on resume and splices them into the system prompt as
``# Recent checkpoints (resume context)``, oldest first. With zero
checkpoints, the section is omitted entirely.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.log import transcript_event
from anna.runtime.delivered_posts import record_delivered_post
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker


CONV_KEY = "slack:dm:UTEST"
TAIL_HEADING = "# Unsaved conversation tail (since last checkpoint)"


def _make_worker(tmp_path: Path, *, ephemeral: bool = False) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _noop_send(_msg):
        return None

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
        ephemeral=ephemeral,
    )


def _plant_checkpoint(vault_root: Path, stamp: str, summary: str) -> Path:
    safe = CONV_KEY.replace(":", "-")
    conv_dir = vault_root / "Conversations" / safe
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / f"{stamp}.md"
    path.write_text(f"# Checkpoint\n\n{summary}\n", encoding="utf-8")
    return path


def test_no_checkpoints_omits_resume_block(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=worker._config.vault.resolved_path,
        format_rule="(no rule)",
    )
    assert "Recent checkpoints" not in prompt


def test_resume_block_oldest_first_with_three_files_truncates_to_two(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path

    _plant_checkpoint(vault_root, "2026-05-29-1000", "oldest summary")
    _plant_checkpoint(vault_root, "2026-05-30-0800", "middle summary")
    _plant_checkpoint(vault_root, "2026-05-31-2200", "newest summary")

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )

    # The oldest of the FIVE files is excluded (only top-2-newest are
    # picked up); the included pair is "middle" and "newest", oldest first.
    assert "# Recent checkpoints (resume context)" in prompt
    assert "middle summary" in prompt
    assert "newest summary" in prompt
    assert "oldest summary" not in prompt
    # Oldest-first ordering within the included pair.
    assert prompt.index("middle summary") < prompt.index("newest summary")


def test_resume_block_placed_before_core_identity_files(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path
    _plant_checkpoint(vault_root, "2026-05-31-0900", "session summary")

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )

    resume_at = prompt.index("# Recent checkpoints (resume context)")
    core_at = prompt.index("# Core identity files")
    runtime_at = prompt.index("# Runtime paths")
    # Order required by the spec: scope, runtime, resume, core, channel.
    assert runtime_at < resume_at < core_at


def test_filename_stamp_appears_as_section_heading(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path
    _plant_checkpoint(vault_root, "2026-05-31-0900", "session summary")

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert "## 2026-05-31-0900" in prompt


# ---------------------------------------------------------------------------
# Fix 1: transcript-tail resume (subtasks 5, 9, 10, 11)
# ---------------------------------------------------------------------------


def _safe_key() -> str:
    return CONV_KEY.replace(":", "-").replace("/", "_")


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )


def _plant_transcript(
    transcripts_dir: Path,
    date: str,
    lines: list[dict],
) -> Path:
    """Write a daily JSONL transcript file for the conv_key."""
    conv_dir = transcripts_dir / _safe_key()
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / f"{date}.jsonl"
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _line(ts_epoch: float, direction: str, text: str) -> dict:
    return {
        "ts": _iso(ts_epoch),
        "direction": direction,
        "conv_key": CONV_KEY,
        "text": text,
    }


def test_tail_appended_after_checkpoint_when_transcript_newer(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path

    ckpt = _plant_checkpoint(vault_root, "2026-05-31-0900", "session summary")
    _set_mtime(ckpt, 1_000_000.0)

    # Transcript lines all NEWER than the checkpoint mtime.
    _plant_transcript(
        worker._config.transcripts_dir,
        "2026-05-31",
        [
            _line(1_000_100.0, "inbound", "what is the status"),
            _line(1_000_101.0, "outbound", "the deploy finished"),
        ],
    )

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )

    assert "# Recent checkpoints (resume context)" in prompt
    assert TAIL_HEADING in prompt
    assert "what is the status" in prompt
    assert "the deploy finished" in prompt
    # Tail appended AFTER the checkpoint block, still before core identity.
    ckpt_at = prompt.index("# Recent checkpoints (resume context)")
    tail_at = prompt.index(TAIL_HEADING)
    core_at = prompt.index("# Core identity files")
    runtime_at = prompt.index("# Runtime paths")
    assert runtime_at < ckpt_at < tail_at < core_at


def test_hard_crash_no_checkpoint_yields_bounded_tail(tmp_path: Path) -> None:
    # Subtask 10: transcript present, NO checkpoint at all.
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path

    _plant_transcript(
        worker._config.transcripts_dir,
        "2026-06-01",
        [
            _line(2_000_000.0, "inbound", "crash recovery question"),
            _line(2_000_001.0, "outbound", "recovered context here"),
        ],
    )

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )

    # No checkpoint block, but the tail must be present (and bounded by caps).
    assert "# Recent checkpoints (resume context)" not in prompt
    assert TAIL_HEADING in prompt
    assert "crash recovery question" in prompt
    assert "recovered context here" in prompt


def test_stale_checkpoint_older_than_lines_yields_tail(tmp_path: Path) -> None:
    # Subtask 10 variant: a checkpoint exists but is OLDER than the lines.
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path

    ckpt = _plant_checkpoint(vault_root, "2026-05-30-0800", "stale summary")
    _set_mtime(ckpt, 500_000.0)

    _plant_transcript(
        worker._config.transcripts_dir,
        "2026-05-31",
        [
            _line(600_000.0, "inbound", "newer than stale checkpoint"),
        ],
    )

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert TAIL_HEADING in prompt
    assert "newer than stale checkpoint" in prompt


def test_legacy_checkpoint_never_claims_transcript_coverage(tmp_path: Path) -> None:
    # Filesystem mtime is not an event-coverage cursor. A legacy checkpoint
    # cannot prove it captured these transcript lines, so replay them.
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path

    ckpt = _plant_checkpoint(vault_root, "2026-05-31-0900", "covers everything")
    _set_mtime(ckpt, 9_000_000.0)

    _plant_transcript(
        worker._config.transcripts_dir,
        "2026-05-31",
        [
            _line(8_000_000.0, "inbound", "already checkpointed"),
            _line(8_000_001.0, "outbound", "already saved reply"),
        ],
    )

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert "# Recent checkpoints (resume context)" in prompt
    assert TAIL_HEADING in prompt
    assert "already checkpointed" in prompt


def test_budget_respects_turn_and_token_caps(tmp_path: Path) -> None:
    # Subtask 11: a large transcript with no checkpoint is bounded.
    worker = _make_worker(tmp_path)
    worker._config.checkpoint.tail_max_turns = 3
    worker._config.checkpoint.tail_max_tokens = 100_000  # not the binding cap here
    vault_root = worker._config.vault.resolved_path

    lines: list[dict] = []
    base = 3_000_000.0
    for i in range(20):
        lines.append(_line(base + i * 2, "inbound", f"inbound message number {i}"))
        lines.append(_line(base + i * 2 + 1, "outbound", f"outbound reply number {i}"))
    _plant_transcript(worker._config.transcripts_dir, "2026-06-02", lines)

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )

    assert TAIL_HEADING in prompt
    # Only the newest 3 inbound-anchored turns survive (17, 18, 19).
    assert "inbound message number 19" in prompt
    assert "inbound message number 18" in prompt
    assert "inbound message number 17" in prompt
    assert "inbound message number 16" not in prompt
    assert "inbound message number 0" not in prompt


def test_budget_respects_token_cap(tmp_path: Path) -> None:
    # Subtask 11: token cap binds before the turn cap.
    worker = _make_worker(tmp_path)
    worker._config.checkpoint.tail_max_turns = 50
    worker._config.checkpoint.tail_max_tokens = 30
    vault_root = worker._config.vault.resolved_path

    lines: list[dict] = []
    base = 4_000_000.0
    for i in range(20):
        lines.append(
            _line(
                base + i,
                "inbound",
                f"message {i} " + "padding word " * 10,
            )
        )
    _plant_transcript(worker._config.transcripts_dir, "2026-06-02", lines)

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert TAIL_HEADING in prompt
    # The newest message is always kept; the oldest is dropped by the token cap.
    assert "message 19" in prompt
    assert "message 0 " not in prompt


def test_resume_from_transcript_disabled_omits_tail(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._config.checkpoint.resume_from_transcript = False
    vault_root = worker._config.vault.resolved_path

    _plant_transcript(
        worker._config.transcripts_dir,
        "2026-06-01",
        [_line(5_000_000.0, "inbound", "should not appear")],
    )

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert TAIL_HEADING not in prompt
    assert "should not appear" not in prompt


def test_ephemeral_worker_omits_tail(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path, ephemeral=True)
    vault_root = worker._config.vault.resolved_path

    _plant_transcript(
        worker._config.transcripts_dir,
        "2026-06-01",
        [_line(6_000_000.0, "inbound", "ephemeral one shot")],
    )

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert TAIL_HEADING not in prompt
    assert "ephemeral one shot" not in prompt


def test_broken_transcript_dir_falls_back_to_checkpoint(tmp_path: Path, monkeypatch) -> None:
    worker = _make_worker(tmp_path)
    vault_root = worker._config.vault.resolved_path
    _plant_checkpoint(vault_root, "2026-05-31-0900", "session summary")

    # Force the tail helper to raise; prompt assembly must still succeed and
    # return the checkpoint block alone.
    import anna.runtime.worker as worker_mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("transcript dir exploded")

    monkeypatch.setattr(worker_mod, "transcript_tail_since", _boom)

    prompt = worker._assemble_system_prompt(
        anna_home=worker._config.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert "# Recent checkpoints (resume context)" in prompt
    assert "session summary" in prompt
    assert TAIL_HEADING not in prompt


# ---------------------------------------------------------------------------
# Scheduled posts delivered into this conversation (2026-10-07 incident)
# ---------------------------------------------------------------------------


def test_scheduled_post_in_resume_context_with_origin_label(tmp_path: Path) -> None:
    """The DM worker that spawns for the operator's reply must see the
    scheduled post the reply is about, labelled as a scheduled post."""
    worker = _make_worker(tmp_path)
    cfg = worker._config
    vault_root = cfg.vault.resolved_path

    # The DM's last checkpoint predates the scheduled post.
    ckpt = _plant_checkpoint(vault_root, "2026-10-03-0945", "PDF read crashed twice")
    _set_mtime(ckpt, 1_000_000.0)

    # The scheduler delivers the heartbeat alert into the operator's DM ...
    alert = "DocuSign: Damon signed the offer letter 4 minutes ago."
    recorded = record_delivered_post(
        cfg,
        transport="slack",
        channel="UTEST",
        text=alert,
        source="schedule:heartbeat",
    )
    assert recorded == CONV_KEY
    # ... and the operator replies, which the router logs before it spawns
    # the worker.
    transcript_event(
        channel="slack",
        conv_key=CONV_KEY,
        transcripts_dir=cfg.transcripts_dir,
        direction="inbound",
        text="Fuck yea it did",
        sender_id="UTEST",
        sender_display="UTEST",
        is_dm=True,
        is_thread=False,
    )

    prompt = worker._assemble_system_prompt(
        anna_home=cfg.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )

    assert "# Recent checkpoints (resume context)" in prompt
    assert TAIL_HEADING in prompt
    labelled = f"**anna (scheduled: heartbeat):** {alert}"
    assert labelled in prompt
    assert "**you:** Fuck yea it did" in prompt
    # Chronological: the post the operator is replying to comes first, and
    # the whole tail sits after the checkpoint block.
    assert (
        prompt.index("PDF read crashed twice")
        < prompt.index(labelled)
        < prompt.index("**you:** Fuck yea it did")
    )
    # It is not mislabelled as a conversational reply.
    assert f"**anna:** {alert}" not in prompt


def test_scheduled_post_alone_in_resume_context_without_checkpoint(
    tmp_path: Path,
) -> None:
    """First contact in a DM that has only ever received a scheduled post."""
    worker = _make_worker(tmp_path)
    cfg = worker._config

    record_delivered_post(
        cfg,
        transport="slack",
        channel="UTEST",
        text="Calendar: standup moved to 10:30.",
        source="schedule:heartbeat",
    )

    prompt = worker._assemble_system_prompt(
        anna_home=cfg.anna_home,
        vault_root=cfg.vault.resolved_path,
        format_rule="(no rule)",
    )
    assert "Recent checkpoints" not in prompt
    assert TAIL_HEADING in prompt
    assert "**anna (scheduled: heartbeat):** Calendar: standup moved to 10:30." in prompt


def test_scheduled_post_covered_by_checkpoint_not_repeated(tmp_path: Path) -> None:
    """Same dedup rule as every other transcript line: a row the latest
    checkpoint explicitly covers (stable sequence + event id, never mtime)
    is not replayed in the tail."""
    worker = _make_worker(tmp_path)
    cfg = worker._config
    vault_root = cfg.vault.resolved_path

    record_delivered_post(
        cfg,
        transport="slack",
        channel="UTEST",
        text="an alert the checkpoint already covers",
        source="schedule:heartbeat",
    )
    rows = [
        json.loads(line)
        for path in cfg.transcripts_dir.rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    row = next(r for r in rows if r.get("source") == "schedule:heartbeat")
    ckpt = _plant_checkpoint(vault_root, "2099-01-01-0000", "covers the alert")
    ckpt.write_text(
        "---\n"
        f"coverage_end_sequence: {row['sequence']}\n"
        f"coverage_end_event_id: {row['event_id']}\n"
        "---\n# Checkpoint\n\ncovers the alert\n",
        encoding="utf-8",
    )

    prompt = worker._assemble_system_prompt(
        anna_home=cfg.anna_home,
        vault_root=vault_root,
        format_rule="(no rule)",
    )
    assert "covers the alert" in prompt
    assert TAIL_HEADING not in prompt
    assert "an alert the checkpoint already covers" not in prompt


def test_scheduled_post_with_legacy_checkpoint_is_replayed(tmp_path: Path) -> None:
    """A legacy checkpoint (no coverage metadata) cannot prove it covers the
    post; mtime is not a cursor, so the post is shown rather than lost."""
    worker = _make_worker(tmp_path)
    cfg = worker._config
    record_delivered_post(
        cfg, transport="slack", channel="UTEST",
        text="an alert nobody acknowledged", source="schedule:heartbeat",
    )
    ckpt = _plant_checkpoint(cfg.vault.resolved_path, "2099-01-01-0000", "legacy summary")
    _set_mtime(ckpt, datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp())
    prompt = worker._assemble_system_prompt(
        anna_home=cfg.anna_home, vault_root=cfg.vault.resolved_path, format_rule="(no rule)",
    )
    assert "an alert nobody acknowledged" in prompt
