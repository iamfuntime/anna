"""Unit tests for ``anna.vault.checkpoint``.

Covers the second-granular filename stamp, the ``checkpoint_kind``
frontmatter field, and the newest-first ordering of
``list_recent_checkpoints``.
"""

from __future__ import annotations

import re
from pathlib import Path

from anna.vault.checkpoint import list_recent_checkpoints, write_checkpoint

CONV_KEY = "telegram:dm:12345"


def _safe(key: str) -> str:
    return key.replace(":", "-").replace("/", "_")


# ---------------------------------------------------------------------------
# Filename stamp granularity
# ---------------------------------------------------------------------------


def test_filename_carries_seconds(tmp_path: Path) -> None:
    path = write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="hello",
    )
    # Stem is YYYY-MM-DD-HHMMSS (second granularity): date plus a 6-digit
    # zero-padded time component.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}", path.stem), path.stem
    # And it is glob-discoverable as a markdown file.
    assert path.suffix == ".md"
    assert path in list((tmp_path / "Conversations" / _safe(CONV_KEY)).glob("*.md"))


def test_two_writes_over_a_second_apart_are_distinct(
    tmp_path: Path, monkeypatch
) -> None:
    """Writes more than one second apart land in different files.

    We drive the clock deterministically rather than sleeping so the test
    stays fast and is not flaky on a fast machine.
    """
    import anna.vault.checkpoint as cp

    from datetime import datetime, timezone

    times = iter(
        [
            datetime(2026, 6, 3, 12, 30, 15, tzinfo=timezone.utc),
            datetime(2026, 6, 3, 12, 30, 17, tzinfo=timezone.utc),
        ]
    )

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return next(times)

    monkeypatch.setattr(cp, "datetime", _FakeDateTime)

    first = write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="one",
    )
    second = write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="two",
    )

    assert first != second
    assert first.name == "2026-06-03-123015.md"
    assert second.name == "2026-06-03-123017.md"


def test_same_minute_writes_do_not_collide(tmp_path: Path, monkeypatch) -> None:
    """Two writes within the same minute (different seconds) coexist.

    This is the collision the second-granular stamp fixes: under the old
    minute-only stamp both would map to one filename and overwrite.
    """
    import anna.vault.checkpoint as cp

    from datetime import datetime, timezone

    times = iter(
        [
            datetime(2026, 6, 3, 9, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 3, 9, 5, 2, tzinfo=timezone.utc),
        ]
    )

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return next(times)

    monkeypatch.setattr(cp, "datetime", _FakeDateTime)

    write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="periodic",
        kind="periodic",
    )
    write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="closeout",
    )

    conv_dir = tmp_path / "Conversations" / _safe(CONV_KEY)
    files = list(conv_dir.glob("*.md"))
    assert len(files) == 2


# ---------------------------------------------------------------------------
# Ordering with the new stamp
# ---------------------------------------------------------------------------


def test_list_recent_orders_newest_first(tmp_path: Path, monkeypatch) -> None:
    """``list_recent_checkpoints`` returns newest first with second stamps.

    Lexicographic ordering of the fixed-width zero-padded stamp must equal
    chronological ordering, including across a minute boundary where only the
    seconds field disambiguates.
    """
    import anna.vault.checkpoint as cp

    from datetime import datetime, timezone

    stamps = [
        datetime(2026, 6, 3, 12, 30, 5, tzinfo=timezone.utc),  # oldest
        datetime(2026, 6, 3, 12, 30, 59, tzinfo=timezone.utc),
        datetime(2026, 6, 3, 12, 31, 0, tzinfo=timezone.utc),  # newest
    ]
    times = iter(stamps)

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return next(times)

    monkeypatch.setattr(cp, "datetime", _FakeDateTime)

    for i, _ in enumerate(stamps):
        write_checkpoint(
            vault_root=tmp_path,
            transport="telegram",
            conversation_key=CONV_KEY,
            summary=f"summary-{i}",
        )

    recent = list_recent_checkpoints(
        vault_root=tmp_path,
        conversation_key=CONV_KEY,
        limit=3,
    )
    names = [p.name for p in recent]
    assert names == [
        "2026-06-03-123100.md",  # newest first
        "2026-06-03-123059.md",
        "2026-06-03-123005.md",
    ]


# ---------------------------------------------------------------------------
# checkpoint_kind frontmatter
# ---------------------------------------------------------------------------


def test_kind_defaults_to_closeout(tmp_path: Path) -> None:
    path = write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="hello",
    )
    body = path.read_text(encoding="utf-8")
    assert "checkpoint_kind: closeout\n" in body


def test_kind_roundtrips_periodic(tmp_path: Path) -> None:
    path = write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="hello",
        kind="periodic",
    )
    body = path.read_text(encoding="utf-8")
    assert "checkpoint_kind: periodic\n" in body


def test_existing_frontmatter_fields_preserved(tmp_path: Path) -> None:
    """The new field is additive; all prior frontmatter is unchanged."""
    path = write_checkpoint(
        vault_root=tmp_path,
        transport="telegram",
        conversation_key=CONV_KEY,
        summary="body text here",
        operator_short_name="Seth",
    )
    body = path.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "tags:\n  - domain/anna\n  - type/checkpoint\n" in body
    assert "transport: telegram\n" in body
    assert f"conversation_key: {CONV_KEY}\n" in body
    assert "checkpoint_kind: closeout\n" in body
    assert "# Checkpoint\n\nbody text here\n" in body
    assert "Addressed as: Seth\n" in body
