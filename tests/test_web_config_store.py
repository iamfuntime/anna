"""Tests for ``anna_web.config_store.ConfigStore`` (subtask 3).

Five required tests per the buildout plan, plus a couple of edge
cases worth pinning. The load-bearing claim is that the ruamel
round-trip preserves operator comments verbatim — if test 1 ever
regresses the whole dashboard subtask is moot, so it's structured
to fail loud (byte-for-byte string compare against the example).

Tests use a per-test copy of the canonical ``anna.yaml.example`` in
``tmp_path`` so the repo file is never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from anna_web.config_store import ConfigStore

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake $ANNA_HOME with a fresh copy of anna.yaml.example.

    The store reads ``<anna_home>/anna.yaml``, so we copy the
    canonical example to that name. Each test gets an isolated
    parent directory under pytest's tmp_path machinery so the
    atomic-rename window has somewhere to put its tempfile.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


def _read_bytes(p: Path) -> bytes:
    return p.read_bytes()


# ---------------------------------------------------------------------------
# 1. Round-trip preserves comments verbatim.
# ---------------------------------------------------------------------------


def test_load_then_write_is_byte_identical(anna_home: Path) -> None:
    """Load anna.yaml.example through ruamel and write it straight back.

    The strongest possible test of the round-trip contract: every
    byte in, every byte out. If this fails, every other test in this
    module is meaningless — the dashboard cannot honor the
    "comments preserved" promise.

    We exercise the public surface (``load_raw`` + the private
    atomic-write helper) rather than the async ``write_section``
    path because ``write_section`` requires picking a mutation, and
    the strongest no-change test is "no change".
    """
    store = ConfigStore(anna_home=anna_home)
    before = _read_bytes(store.path)

    doc = store.load_raw()
    # Round-trip with zero mutation.
    store._atomic_write(doc)  # noqa: SLF001 — intentional white-box check.

    after = _read_bytes(store.path)
    assert after == before, (
        "ruamel round-trip mutated the file. "
        f"len(before)={len(before)} len(after)={len(after)}"
    )


# ---------------------------------------------------------------------------
# 2. write_section mutates one block, leaves siblings byte-identical.
# ---------------------------------------------------------------------------


async def test_write_section_preserves_sibling_sections(anna_home: Path) -> None:
    """Mutating runtime leaves auth/transports/vault/etc. byte-identical.

    Strategy: split the file on top-level section headers (each
    top-level key starts at column 0, e.g. ``auth:``, ``runtime:``,
    ``transports:``). Compare every chunk OTHER than the one we
    intended to mutate; they must match the pre-write file exactly.
    Comments inside the mutated section are preserved by ruamel
    (that's the point); comments outside are preserved trivially
    because the bytes never move.
    """
    store = ConfigStore(anna_home=anna_home)
    before_text = store.path.read_text(encoding="utf-8")

    # Mutate runtime.permission_mode. The visibility sub-block is
    # commented out in the example, so we touch the one runtime
    # field that's actually live on disk. Note: write_section
    # replaces the section wholesale, so we have to send the full
    # runtime payload.
    raw_before = store.load_raw()
    runtime_payload = dict(raw_before["runtime"])
    assert runtime_payload["permission_mode"] == "bypassPermissions"
    runtime_payload["permission_mode"] = "acceptEdits"

    cfg = await store.write_section("runtime", runtime_payload)
    assert cfg.runtime.permission_mode == "acceptEdits"

    after_text = store.path.read_text(encoding="utf-8")

    # Split both texts on top-level section headers. Top-level keys
    # start at column 0; anything indented is part of the previous
    # section's body. The ConfigStore-written file uses identical
    # indentation (mapping=2, sequence=4, offset=2) to ruamel's
    # parse of the example, so the section-header set is identical.
    before_sections = _split_top_level(before_text)
    after_sections = _split_top_level(after_text)

    assert set(before_sections.keys()) == set(after_sections.keys()), (
        f"top-level keys diverged: "
        f"before={sorted(before_sections)} after={sorted(after_sections)}"
    )

    for key in before_sections:
        if key == "runtime":
            # The mutated section is expected to differ. Sanity-
            # check that the new value made it in and the old one
            # is gone, so the test fails for "didn't write" too.
            assert "permission_mode: acceptEdits" in after_sections[key]
            assert "permission_mode: bypassPermissions" not in after_sections[key]
        else:
            assert after_sections[key] == before_sections[key], (
                f"sibling section {key!r} changed across write_section"
            )


def _split_top_level(text: str) -> dict[str, str]:
    """Split a YAML doc into chunks keyed by top-level section name.

    Top-level sections in anna.yaml.example start with a ``# ====``
    banner followed by a ``key:`` line at column 0. We split on
    bare ``^[a-z_]+:`` lines (column 0, no leading whitespace).
    Leading comment / banner lines that precede the first such
    header are stashed under the empty-string key so they're
    compared too.
    """
    chunks: dict[str, str] = {}
    current_key = ""
    current_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        # Top-level key: starts at column 0, looks like ``name:`` or
        # ``name:<space>...``. Ignore lines starting with ``#`` or
        # whitespace.
        if (
            stripped
            and stripped[0].isalpha()
            and ":" in stripped
            and not stripped.startswith(" ")
            and not stripped.startswith("#")
        ):
            key_part = stripped.split(":", 1)[0].strip()
            # Flush the previous section before starting the new one.
            chunks[current_key] = "".join(current_lines)
            current_key = key_part
            current_lines = [line]
        else:
            current_lines.append(line)
    chunks[current_key] = "".join(current_lines)
    return chunks


# ---------------------------------------------------------------------------
# 3. Validation failure leaves the file unchanged.
# ---------------------------------------------------------------------------


async def test_validation_failure_leaves_file_unchanged(anna_home: Path) -> None:
    """A pydantic ValidationError aborts the write — file is untouched.

    web.port has a 1..65535 validator; 70000 is out of range. We
    capture both mtime and exact bytes before the call and assert
    neither moves after the ValidationError surfaces.
    """
    store = ConfigStore(anna_home=anna_home)
    before_bytes = store.path.read_bytes()
    before_mtime = store.path.stat().st_mtime_ns

    with pytest.raises(ValidationError):
        await store.write_section(
            "web",
            {"enabled": True, "host": "127.0.0.1", "port": 70000, "target_unit": "anna.service"},
        )

    after_bytes = store.path.read_bytes()
    after_mtime = store.path.stat().st_mtime_ns

    assert after_bytes == before_bytes, "file mutated despite ValidationError"
    assert after_mtime == before_mtime, "file mtime moved despite ValidationError"


# ---------------------------------------------------------------------------
# 4. Atomic rename — interrupted dump leaves original intact and no tmp litter.
# ---------------------------------------------------------------------------


async def test_atomic_write_failure_leaves_original_intact(
    anna_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupt the dump step; original file stays whole, tmp gets cleaned up.

    We monkeypatch the YAML instance's ``dump`` method to raise
    after we know the tempfile has been opened. The store's
    cleanup branch should remove the tempfile so we don't leak
    ``.anna.yaml.*.tmp`` orphans.
    """
    store = ConfigStore(anna_home=anna_home)
    before_bytes = store.path.read_bytes()

    class _BoomError(RuntimeError):
        pass

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise _BoomError("simulated dump failure mid-write")

    monkeypatch.setattr(store._yaml, "dump", _explode)

    # Send a payload that passes validation so we reach the dump
    # step (otherwise we'd trip the validate-fail branch first).
    raw = store.load_raw()
    runtime_payload = dict(raw["runtime"])

    with pytest.raises(_BoomError):
        await store.write_section("runtime", runtime_payload)

    # Original untouched.
    assert store.path.read_bytes() == before_bytes

    # No tempfile litter. The store uses prefix=".anna.yaml." +
    # suffix=".tmp" in the same parent directory.
    leftover = list(anna_home.glob(".anna.yaml.*.tmp"))  # noqa: ASYNC240 — sync glob in a test
    assert leftover == [], f"tempfile cleanup failed; orphans: {leftover}"


# ---------------------------------------------------------------------------
# 5. Unknown section raises ValueError; file unchanged.
# ---------------------------------------------------------------------------


async def test_unknown_section_raises_value_error(anna_home: Path) -> None:
    """A bogus section name fails fast with a message naming the bad key."""
    store = ConfigStore(anna_home=anna_home)
    before_bytes = store.path.read_bytes()

    with pytest.raises(ValueError, match="nonsense"):
        await store.write_section("nonsense", {})

    assert store.path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# Bonus: load_validated returns a working AnnaConfig and load_raw vs
# load_validated agree on the structure.
# ---------------------------------------------------------------------------


def test_load_validated_returns_anna_config(anna_home: Path) -> None:
    """load_validated produces an AnnaConfig and the web block matches defaults."""
    store = ConfigStore(anna_home=anna_home)
    cfg = store.load_validated()

    # Example file ships web.enabled=true, host=127.0.0.1, port=8765,
    # target_unit=anna.service.
    assert cfg.web.enabled is True
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8765
    assert cfg.web.target_unit == "anna.service"


def test_anna_home_is_not_writable_as_a_section(anna_home: Path) -> None:
    """anna_home is a derived field on AnnaConfig, not a YAML key.

    The derived field would otherwise leak into ``model_fields`` and
    accidentally become writable from the dashboard, which would
    persist a host-specific Path into a file the operator might
    sync across machines. Pinning the exclusion with a test means a
    future refactor of AnnaConfig can't silently regress this.
    """
    store = ConfigStore(anna_home=anna_home)

    import asyncio

    with pytest.raises(ValueError, match="anna_home"):
        asyncio.run(store.write_section("anna_home", {}))
