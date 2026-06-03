"""Tests for anna.vault.transcript_resume."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from anna.vault.transcript_resume import (
    render_tail_block,
    transcript_tail_since,
)

CONV_KEY = "slack:dm:USP2QLB41"
SAFE_KEY = "slack-dm-USP2QLB41"


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _line(epoch: float, direction: str, text: str) -> dict:
    obj = {
        "ts": _iso(epoch),
        "direction": direction,
        "conv_key": CONV_KEY,
        "text": text,
    }
    if direction == "inbound":
        obj["sender_id"] = "USP2QLB41"
        obj["sender_display"] = "USP2QLB41"
        obj["is_dm"] = True
        obj["is_thread"] = False
    return obj


def _write_day(transcripts_dir: Path, day: str, objs: list[dict]) -> Path:
    d = transcripts_dir / SAFE_KEY
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{day}.jsonl"
    p.write_text(
        "\n".join(json.dumps(o) for o in objs) + "\n",
        encoding="utf-8",
    )
    return p


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 1500)
    assert result == []


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    d = tmp_path / SAFE_KEY
    d.mkdir(parents=True)
    p = d / "2026-06-03.jsonl"
    good = _line(1000.0, "inbound", "hello")
    p.write_text(
        "\n".join(
            [
                json.dumps(good),
                "{not json at all",
                "",
                "42",  # valid JSON but not a dict
                json.dumps(_line(1001.0, "outbound", "hi back")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 1500)
    assert [o["text"] for o in result] == ["hello", "hi back"]


def test_since_none_keeps_whole_tail(tmp_path: Path) -> None:
    objs = [
        _line(1000.0, "inbound", "one"),
        _line(1001.0, "outbound", "two"),
        _line(1002.0, "inbound", "three"),
    ]
    _write_day(tmp_path, "2026-06-03", objs)
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 1500)
    assert [o["text"] for o in result] == ["one", "two", "three"]


def test_mtime_filter_excludes_older(tmp_path: Path) -> None:
    objs = [
        _line(1000.0, "inbound", "old"),
        _line(2000.0, "outbound", "at boundary"),
        _line(3000.0, "inbound", "new"),
    ]
    _write_day(tmp_path, "2026-06-03", objs)
    # since_mtime = 2000.0 -> strictly newer means only "new" survives.
    result = transcript_tail_since(tmp_path, CONV_KEY, 2000.0, 8, 1500)
    assert [o["text"] for o in result] == ["new"]


def test_max_turns_cap(tmp_path: Path) -> None:
    objs: list[dict] = []
    t = 1000.0
    for i in range(5):
        objs.append(_line(t, "inbound", f"q{i}"))
        t += 1
        objs.append(_line(t, "outbound", f"a{i}"))
        t += 1
    _write_day(tmp_path, "2026-06-03", objs)
    # Cap to 2 inbound-anchored exchanges -> most recent two q/a pairs.
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 2, 100000)
    texts = [o["text"] for o in result]
    assert texts == ["q3", "a3", "q4", "a4"]


def test_max_tokens_cap(tmp_path: Path) -> None:
    objs = [
        _line(1000.0, "inbound", "alpha beta gamma delta"),  # 4 tokens
        _line(1001.0, "outbound", "one two three"),  # 3 tokens
        _line(1002.0, "inbound", "final"),  # 1 token
    ]
    _write_day(tmp_path, "2026-06-03", objs)
    # Budget 4 tokens: newest "final"(1) + "one two three"(3) = 4 fits;
    # adding the 4-token inbound would overflow -> trimmed oldest first.
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 4)
    assert [o["text"] for o in result] == ["one two three", "final"]


def test_max_tokens_keeps_at_least_one(tmp_path: Path) -> None:
    objs = [_line(1000.0, "inbound", "a b c d e f g h i j")]  # 10 tokens
    _write_day(tmp_path, "2026-06-03", objs)
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 1)
    assert len(result) == 1


def test_multi_day_reads_two_files(tmp_path: Path) -> None:
    _write_day(
        tmp_path,
        "2026-06-01",
        [_line(100.0, "inbound", "very old day1")],
    )
    _write_day(
        tmp_path,
        "2026-06-02",
        [
            _line(1000.0, "inbound", "day2 q"),
            _line(1001.0, "outbound", "day2 a"),
        ],
    )
    _write_day(
        tmp_path,
        "2026-06-03",
        [_line(2000.0, "inbound", "day3 q")],
    )
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 1500)
    texts = [o["text"] for o in result]
    # Only the two newest daily files are read; day1 is excluded.
    assert texts == ["day2 q", "day2 a", "day3 q"]


def test_multi_day_chronological_order(tmp_path: Path) -> None:
    _write_day(tmp_path, "2026-06-02", [_line(1000.0, "inbound", "earlier")])
    _write_day(tmp_path, "2026-06-03", [_line(2000.0, "inbound", "later")])
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 8, 1500)
    assert [o["text"] for o in result] == ["earlier", "later"]


def test_render_tail_block_empty() -> None:
    assert render_tail_block([]) == ""


def test_render_tail_block_formatting() -> None:
    tail = [
        _line(1000.0, "inbound", "hey anna"),
        _line(1001.0, "outbound", "hey there"),
    ]
    block = render_tail_block(tail)
    lines = block.splitlines()
    assert lines[0] == "# Unsaved conversation tail (since last checkpoint)"
    assert lines[1] == ""
    assert lines[2] == "**you:** hey anna"
    assert lines[3] == "**anna:** hey there"


def test_render_tail_block_truncates_long_message() -> None:
    long_text = "x" * 600
    tail = [_line(1000.0, "outbound", long_text)]
    block = render_tail_block(tail)
    body = block.splitlines()[-1]
    assert body.startswith("**anna:** ")
    assert body.endswith("...")
    # 400 chars of payload + prefix + ellipsis.
    assert len(body) < len("**anna:** ") + 400 + 5
