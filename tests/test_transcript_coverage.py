"""Stable transcript ordering and explicit checkpoint-coverage regressions."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from anna.log import transcript_event
from anna.vault.checkpoint import write_checkpoint
from anna.vault.transcript_resume import (
    latest_checkpoint_coverage,
    transcript_tail_since,
    validated_checkpoint_coverage,
)

CONV = "slack:dm:coverage"


def _event(root: Path, text: str, direction: str = "inbound") -> dict:
    result = transcript_event(
        "slack",
        CONV,
        transcripts_dir=root,
        direction=direction,
        text=text,
    )
    assert result is not None
    return result


def test_stable_cursor_survives_restart_and_copied_mtimes(tmp_path: Path) -> None:
    first = _event(tmp_path, "first")
    second = _event(tmp_path, "second", "outbound")
    checkpoint = write_checkpoint(
        vault_root=tmp_path / "vault",
        transport="slack",
        conversation_key=CONV,
        summary="validated summary",
        coverage_end_sequence=second["sequence"],
        coverage_end_event_id=second["event_id"],
    )
    os.utime(checkpoint, (1, 1))
    # A fresh allocation repairs from JSONL, not the cursor hint or mtimes.
    third = _event(tmp_path, "after restart")
    assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]
    assert validated_checkpoint_coverage(tmp_path / "vault", tmp_path, CONV) == 2
    tail = transcript_tail_since(
        tmp_path, CONV, None, 8, 10_000, covered_through_sequence=latest_checkpoint_coverage(tmp_path / "vault", CONV)
    )
    assert [entry["text"] for entry in tail] == ["after restart"]


def test_mechanical_or_failed_checkpoint_never_hides_long_message(tmp_path: Path) -> None:
    event = _event(tmp_path, "x" * 700)
    write_checkpoint(
        vault_root=tmp_path / "vault",
        transport="slack",
        conversation_key=CONV,
        summary="raw excerpt only",
        kind="periodic",
    )
    assert latest_checkpoint_coverage(tmp_path / "vault", CONV) is None
    tail = transcript_tail_since(tmp_path, CONV, None, 8, 10_000, covered_through_sequence=None)
    assert tail[0]["event_id"] == event["event_id"]
    assert tail[0]["text"] == "x" * 700


def test_concurrent_events_get_unique_ordered_sequences(tmp_path: Path) -> None:
    results: list[dict] = []
    lock = threading.Lock()

    def write(index: int) -> None:
        event = _event(tmp_path, f"pending-{index}")
        with lock:
            results.append(event)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(event["sequence"] for event in results) == list(range(1, 9))
    assert len({event["event_id"] for event in results}) == 8


def test_legacy_checkpoint_has_no_invented_coverage(tmp_path: Path) -> None:
    path = tmp_path / "vault" / "Conversations" / "slack-dm-coverage" / "old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ncheckpoint_kind: closeout\n---\nold summary\n", encoding="utf-8")
    assert latest_checkpoint_coverage(tmp_path / "vault", CONV) is None


def test_coverage_with_missing_boundary_event_is_rejected(tmp_path: Path) -> None:
    _event(tmp_path, "actual")
    write_checkpoint(
        vault_root=tmp_path / "vault",
        transport="slack",
        conversation_key=CONV,
        summary="bad copied checkpoint",
        coverage_end_sequence=1,
        coverage_end_event_id="0" * 32,
    )
    assert latest_checkpoint_coverage(tmp_path / "vault", CONV) == 1
    assert validated_checkpoint_coverage(tmp_path / "vault", tmp_path, CONV) is None
