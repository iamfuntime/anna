"""Tests for anna.vault.transcript_resume."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from anna.vault.transcript_resume import (
    delivered_posts_since,
    latest_ts,
    render_delivered_posts,
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


# ---------------------------------------------------------------------------
# Out-of-band posts (rows carrying a ``source``): scheduled posts and
# slack_post deliveries recorded by anna.runtime.delivered_posts.
# ---------------------------------------------------------------------------


def _sourced(epoch: float, text: str, source: str) -> dict:
    obj = _line(epoch, "outbound", text)
    obj["source"] = source
    return obj


def test_render_labels_scheduled_post_with_its_origin() -> None:
    tail = [
        _line(1000.0, "outbound", "a conversational reply"),
        _sourced(2000.0, "DocuSign: the offer was signed.", "schedule:heartbeat"),
        _line(3000.0, "inbound", "Fuck yea it did"),
    ]
    block = render_tail_block(tail)
    lines = block.splitlines()
    # A plain reply keeps the bare label; the scheduled post names its origin.
    assert "**anna:** a conversational reply" in lines
    assert "**anna (scheduled: heartbeat):** DocuSign: the offer was signed." in lines
    assert "**you:** Fuck yea it did" in lines
    assert lines.index(
        "**anna (scheduled: heartbeat):** DocuSign: the offer was signed."
    ) < lines.index("**you:** Fuck yea it did")


def test_render_labels_slack_post_and_unknown_sources() -> None:
    tail = [
        _sourced(1000.0, "build is red", "slack_post:schedule:ci-watch:2026-10-07"),
        _sourced(2000.0, "something else", "future_origin"),
        _sourced(3000.0, "blank source", "   "),
    ]
    block = render_tail_block(tail)
    assert (
        "**anna (slack_post from schedule:ci-watch:2026-10-07):** build is red"
        in block
    )
    assert "**anna (future_origin):** something else" in block
    # A blank / non-string source degrades to the plain label.
    assert "**anna:** blank source" in block


def test_render_truncates_long_scheduled_post() -> None:
    tail = [_sourced(1000.0, "x" * 1000, "schedule:heartbeat")]
    block = render_tail_block(tail)
    body = block.splitlines()[-1]
    assert body.startswith("**anna (scheduled: heartbeat):** ")
    assert body.endswith("...")
    assert len(body) < 500


def test_scheduled_posts_bounded_by_the_same_caps(tmp_path: Path) -> None:
    """Scheduled rows are ordinary outbound lines to the trimmer: they ride
    inside the turn cap and are cut by the token cap, oldest first."""
    # Turn cap: two exchanges, each preceded by a scheduled post.
    objs = [
        _sourced(1000.0, "sched 0", "schedule:heartbeat"),
        _line(1001.0, "inbound", "user 0"),
        _sourced(2000.0, "sched 1", "schedule:heartbeat"),
        _line(2001.0, "inbound", "user 1"),
        _sourced(3000.0, "sched 2", "schedule:heartbeat"),
    ]
    _write_day(tmp_path, "2026-06-03", objs)
    result = transcript_tail_since(tmp_path, CONV_KEY, None, 1, 100_000)
    # One inbound-anchored exchange is kept, plus the unanswered post after
    # it; the older scheduled posts fall outside the window.
    assert [o["text"] for o in result] == ["user 1", "sched 2"]

    # Token cap: a run of unanswered scheduled posts cannot grow the tail
    # without bound.
    flood = [
        _sourced(5000.0 + i, f"alert {i} " + "word " * 50, "schedule:heartbeat")
        for i in range(40)
    ]
    _write_day(tmp_path, "2026-06-04", flood)
    result = transcript_tail_since(tmp_path, CONV_KEY, 4000.0, 8, 200)
    assert 0 < len(result) < 40
    assert result[-1]["text"].startswith("alert 39 ")
    assert all(o["source"] == "schedule:heartbeat" for o in result)


# ---------------------------------------------------------------------------
# Posts delivered into a live conversation: lookup past a high-water mark
# and the bounded per-turn listing.
# ---------------------------------------------------------------------------


def test_delivered_posts_since_returns_only_sourced_outbound_past_mark(
    tmp_path: Path,
) -> None:
    inbound_with_source = _line(3500.0, "inbound", "operator text")
    inbound_with_source["source"] = "schedule:heartbeat"
    _write_day(
        tmp_path,
        "2026-06-03",
        [
            _sourced(1000.0, "before the mark", "schedule:heartbeat"),
            _sourced(2000.0, "exactly at the mark", "schedule:heartbeat"),
            _line(2500.0, "outbound", "a conversational reply"),
            _line(2600.0, "inbound", "an operator message"),
            _sourced(3000.0, "after the mark", "schedule:heartbeat"),
            _sourced(3100.0, "blank source", "  "),
            inbound_with_source,
            _sourced(4000.0, "own post", f"slack_post:{CONV_KEY}"),
            _sourced(5000.0, "foreign post", "slack_post:schedule:ci:2026-06-03"),
        ],
    )

    posts = delivered_posts_since(
        tmp_path, CONV_KEY, 2000.0, exclude_sources=(f"slack_post:{CONV_KEY}",)
    )

    # Strictly newer than the mark, outbound, source-tagged, not excluded.
    assert [p["text"] for p in posts] == ["after the mark", "foreign post"]
    assert latest_ts(posts) == 5000.0


def test_delivered_posts_since_missing_dir_and_two_day_window(tmp_path: Path) -> None:
    assert delivered_posts_since(tmp_path, CONV_KEY, 0.0) == []
    assert latest_ts([]) is None

    _write_day(tmp_path, "2026-06-01", [_sourced(1000.0, "three days old", "schedule:a")])
    _write_day(tmp_path, "2026-06-02", [_sourced(2000.0, "yesterday", "schedule:a")])
    _write_day(tmp_path, "2026-06-03", [_sourced(3000.0, "today", "schedule:a")])
    # Same newest-two-daily-files window as the resume tail.
    posts = delivered_posts_since(tmp_path, CONV_KEY, 0.0)
    assert [p["text"] for p in posts] == ["yesterday", "today"]


def test_render_delivered_posts_line_shape() -> None:
    epoch = datetime(2026, 10, 7, 15, 0, 28, tzinfo=timezone.utc).timestamp()
    lines, omitted = render_delivered_posts(
        [_sourced(epoch, "  DocuSign: signed.  ", "schedule:heartbeat")]
    )
    assert omitted == 0
    assert lines == [
        "[2026-10-07 15:00:28Z] anna (scheduled: heartbeat): DocuSign: signed."
    ]
    assert render_delivered_posts([]) == ([], 0)


def test_render_delivered_posts_caps_count_newest_kept() -> None:
    posts = [_sourced(1000.0 + i, f"alert {i}", "schedule:heartbeat") for i in range(7)]
    lines, omitted = render_delivered_posts(posts, max_posts=3, max_chars=10_000)
    assert omitted == 4
    assert lines[0] == "(4 earlier posts omitted)"
    assert [line.rsplit(": ", 1)[-1] for line in lines[1:]] == [
        "alert 4",
        "alert 5",
        "alert 6",
    ]


def test_render_delivered_posts_caps_characters() -> None:
    posts = [
        _sourced(1000.0, "a" * 60, "schedule:heartbeat"),
        _sourced(1001.0, "b" * 60, "schedule:heartbeat"),
        _sourced(1002.0, "c" * 60, "schedule:heartbeat"),
    ]
    # Budget fits two whole posts; the oldest is omitted, never split.
    lines, omitted = render_delivered_posts(posts, max_posts=10, max_chars=130)
    assert omitted == 1
    assert lines[0] == "(1 earlier post omitted)"
    assert lines[1].endswith("b" * 60) and lines[2].endswith("c" * 60)

    # The newest post is always kept, cut to the budget with an ellipsis.
    lines, omitted = render_delivered_posts(posts, max_posts=10, max_chars=20)
    assert omitted == 2
    assert lines[0] == "(2 earlier posts omitted)"
    assert lines[1].endswith(": " + "c" * 20 + "...")
