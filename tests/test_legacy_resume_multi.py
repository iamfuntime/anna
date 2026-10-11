"""Legacy (main-era) transcript lines across MULTIPLE resumes of the coverage-aware build.

Synthetic transcripts / vault under a temp home only.  Each "session" is a fresh
worker over the same files: resume (assemble the tail), one or more completed
turns, then a closeout-style checkpoint written the way ``_closeout`` writes it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anna.log import transcript_event
from anna.vault.checkpoint import write_checkpoint
from anna.vault.transcript_resume import legacy_checkpoint_cutoff
from tests.test_worker_periodic_flush import (
    _FakeAssistantMessage,
    _FakeResultMessage,
    _FakeTextBlock,
    _FakeToolUseBlock,
    _ImmediateClient,
    _make_event,
    _make_worker,
)

# Real "now" minus a few days: legacy lines are in the past, transcript_event stamps the present.
_T0 = datetime.now(UTC).timestamp() - 3 * 86400


@pytest.fixture(autouse=True)
def _sdk(monkeypatch):
    import claude_agent_sdk as sdk

    for name, cls in (("AssistantMessage", _FakeAssistantMessage), ("ResultMessage", _FakeResultMessage),
                      ("TextBlock", _FakeTextBlock), ("ToolUseBlock", _FakeToolUseBlock)):
        monkeypatch.setattr(sdk, name, cls, raising=False)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _legacy_lines(w, stamps_texts: list[tuple[float, str, str]]) -> None:
    """Write unsequenced main-format transcript lines (one daily file per call)."""
    d = w._config.transcripts_dir / w.conversation_key.replace(":", "-")
    d.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(stamps_texts[0][0], tz=UTC).strftime("%Y-%m-%d")
    with (d / f"{day}.jsonl").open("a", encoding="utf-8") as fp:
        for ts, direction, text in stamps_texts:
            fp.write(json.dumps({"ts": _iso(ts), "direction": direction, "conv_key": w.conversation_key,
                                 "text": text}) + "\n")


def _main_checkpoint(w, epoch: float, kind: str = "closeout") -> Path:
    stamp = datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d-%H%M%S")
    d = w._config.vault.resolved_path / "Conversations" / w.conversation_key.replace(":", "-")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stamp}.md"
    path.write_text(
        f"---\ncreated: {stamp[:10]}\ntags:\n  - domain/anna\n  - type/checkpoint\n"
        f"transport: slack\nconversation_key: {w.conversation_key}\ncheckpoint_kind: {kind}\n---\n\n"
        "# Checkpoint\n\nmain summary\n", encoding="utf-8")
    return path


def _resume(tmp_path: Path):
    """A fresh worker resuming the conversation; returns (worker, tail texts)."""
    w = _make_worker(tmp_path, [])
    block = w._assemble_transcript_tail_block(w._config.vault.resolved_path)
    tail = [ln.split(":** ", 1)[1] for ln in block.splitlines() if ln.startswith("**")]
    return w, tail


async def _turn(w, text: str = "q") -> dict:
    """One completed turn: inbound event + reply, processed through ``_handle``."""
    w._client = _ImmediateClient([_FakeTextBlock(text="ok")])
    rec = transcript_event("slack", w.conversation_key, transcripts_dir=w._config.transcripts_dir,
                           direction="inbound", text=text)
    ev = _make_event()
    ev.raw.update(anna_transcript_sequence=rec["sequence"], anna_transcript_event_id=rec["event_id"])
    await w._handle(ev)
    transcript_event("slack", w.conversation_key, transcripts_dir=w._config.transcripts_dir,
                     direction="outbound", text=f"reply to {text}")
    return rec


async def _closeout(w) -> Path:
    """Write the checkpoint exactly as ``_closeout`` does."""
    seq, event_id = w._closeout_coverage()
    path = await w._write_checkpoint_now(
        "summary", kind="closeout", coverage_end_sequence=seq, coverage_end_event_id=event_id,
        legacy_cutoff_ts=w._closeout_legacy_cutoff() if seq else None)
    # Sessions here close within one second; the stamp (not the uuid) must order them.
    n = len(list(path.parent.glob("*.md")))
    stamp = datetime.fromtimestamp(datetime.now(UTC).timestamp() - 7200 + 60 * n, tz=UTC)
    renamed = path.with_name(f"{stamp:%Y-%m-%d-%H%M%S}-{path.stem.split('-', 4)[-1]}.md")
    return path.rename(renamed)


def _seed(tmp_path: Path):
    w = _make_worker(tmp_path, [])
    _legacy_lines(w, [(_T0 - 300, "inbound", "OLD1"), (_T0 - 299, "outbound", "OLD1A")])
    _main_checkpoint(w, _T0)
    _legacy_lines(w, [(_T0 + 60, "inbound", "CRASHQ"), (_T0 + 61, "outbound", "CRASHA")])
    return w


async def test_legacy_boundary_survives_multiple_new_build_resumes(tmp_path: Path) -> None:
    _seed(tmp_path)
    w1, tail = _resume(tmp_path)
    assert tail == ["CRASHQ", "CRASHA"]  # only genuinely uncovered legacy lines, once
    await _turn(w1, "n1")
    ckpt = await _closeout(w1)
    text = ckpt.read_text()
    assert "coverage_legacy_cutoff_ts:" in text and "checkpoint_schema: 2" in text
    assert "NOT summarized" not in text  # the legacy lines were shown, so nothing was skipped
    for n in (2, 3, 4):
        w, tail = _resume(tmp_path)
        assert tail == [], f"resume {n} replayed already-summarized lines: {tail}"
        await _turn(w, f"n{n}")
        await _closeout(w)


async def test_unsummarized_legacy_lines_are_not_lost_when_session_never_covers_them(tmp_path: Path) -> None:
    _seed(tmp_path)
    w1, tail = _resume(tmp_path)
    assert tail == ["CRASHQ", "CRASHA"]
    # failed first turn: no processed sequence, so the closeout carries no coverage and no marker
    w1._coverage_frozen = True
    await _turn(w1, "failed")
    ckpt = await _closeout(w1)
    assert "coverage_legacy_cutoff_ts" not in ckpt.read_text()
    _w2, tail2 = _resume(tmp_path)
    assert tail2[:2] == ["CRASHQ", "CRASHA"]  # still there: never silently dropped


async def test_trimmed_unshown_legacy_lines_are_reported_unsummarized_and_do_not_replay(tmp_path: Path) -> None:
    w = _make_worker(tmp_path, [])
    _main_checkpoint(w, _T0)
    for i in range(12):  # more uncovered legacy exchanges than the resume window shows
        _legacy_lines(w, [(_T0 + 60 + i * 10, "inbound", f"LQ{i}"), (_T0 + 61 + i * 10, "outbound", f"LA{i}")])
    w1 = _make_worker(tmp_path, [])
    w1._config.checkpoint.tail_max_turns = 3
    w1._assemble_transcript_tail_block(w1._config.vault.resolved_path)
    await _turn(w1, "n1")
    text = (await _closeout(w1)).read_text()
    assert "coverage_unsummarized_events:" in text  # honest about the unshown ones
    w2, tail = _resume(tmp_path)
    assert tail == []  # and they do not replay forever


async def test_failed_first_turn_checkpoint_never_time_hides_sequenced_events(tmp_path: Path) -> None:
    w1, _ = _resume(tmp_path)
    await _turn(w1, "n1")
    await _closeout(w1)  # explicit coverage through the reply of n1
    w2, _ = _resume(tmp_path)
    transcript_event("slack", w2.conversation_key, transcripts_dir=w2._config.transcripts_dir,
                     direction="inbound", text="FAILEDQ")  # never processed
    ckpt = write_checkpoint(vault_root=w2._config.vault.resolved_path, transport="slack",
                            conversation_key=w2.conversation_key, summary="s")  # coverage-less
    assert ckpt.exists()
    assert legacy_checkpoint_cutoff(w2._config.vault.resolved_path, w2.conversation_key) is None
    _w3, tail = _resume(tmp_path)
    assert tail == ["FAILEDQ"]


async def test_periodic_checkpoint_is_not_legacy_provenance(tmp_path: Path) -> None:
    w = _seed(tmp_path)
    vault = w._config.vault.resolved_path
    # a periodic, coverage-less NEW-build checkpoint written after the legacy lines
    write_checkpoint(vault_root=vault, transport="slack", conversation_key=w.conversation_key,
                     summary="p", kind="periodic")
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) == pytest.approx(_T0, abs=1)
    _w, tail = _resume(tmp_path)
    assert tail == ["CRASHQ", "CRASHA"]


async def test_new_build_sequenced_events_are_never_hidden_by_legacy_boundary(tmp_path: Path) -> None:
    w = _seed(tmp_path)
    rec = transcript_event("slack", w.conversation_key, transcripts_dir=w._config.transcripts_dir,
                           direction="inbound", text="SEQ1")
    assert isinstance(rec["sequence"], int)
    _w, tail = _resume(tmp_path)
    assert "SEQ1" in tail


def test_future_odd_and_tampered_checkpoints_are_not_time_coverage(tmp_path: Path) -> None:
    w = _seed(tmp_path)
    vault = w._config.vault.resolved_path
    d = vault / "Conversations" / w.conversation_key.replace(":", "-")
    now = datetime.now(UTC).timestamp()
    good = legacy_checkpoint_cutoff(vault, w.conversation_key)
    assert good == pytest.approx(_T0, abs=1)
    # a stray main-shaped note stamped a few minutes ahead (mtime = now) hides nothing
    _main_checkpoint(w, now + 240)
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) == good
    # arbitrary markdown with a stamp-shaped name is not trusted
    stamp = datetime.fromtimestamp(_T0 + 500, tz=UTC).strftime("%Y-%m-%d-%H%M%S")
    (d / f"{stamp}.md").write_text("# my notes\n", encoding="utf-8")
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) == good
    # a uuid-named file with a forged marker but no coverage keys is ignored
    forged = datetime.fromtimestamp(now - 100, tz=UTC).strftime("%Y-%m-%d-%H%M%S") + "-" + "a" * 32 + ".md"
    (d / forged).write_text("---\ncoverage_legacy_cutoff_ts: 9999999999\n---\n", encoding="utf-8")
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) == good
    # uppercase uuid / odd names / a restored OLDER mtime on a main file
    (d / (stamp + "-" + "A" * 32 + ".md")).write_text("x", encoding="utf-8")
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) == good
    main_path = d / datetime.fromtimestamp(_T0, tz=UTC).strftime("%Y-%m-%d-%H%M%S.md")
    os.utime(main_path, (_T0 - 500, _T0 - 500))
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) is None


def test_forged_future_marker_on_coverage_checkpoint_is_rejected(tmp_path: Path) -> None:
    w = _seed(tmp_path)
    vault = w._config.vault.resolved_path
    rec = transcript_event("slack", w.conversation_key, transcripts_dir=w._config.transcripts_dir,
                           direction="inbound", text="SEQ1")
    write_checkpoint(vault_root=vault, transport="slack", conversation_key=w.conversation_key, summary="s",
                     coverage_end_sequence=rec["sequence"], coverage_end_event_id=rec["event_id"],
                     legacy_cutoff_ts=datetime.now(UTC).timestamp() + 86400)
    assert legacy_checkpoint_cutoff(vault, w.conversation_key) == pytest.approx(_T0, abs=1)
