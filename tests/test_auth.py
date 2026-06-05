"""Tests for auth-path resolution and isolated CLAUDE_CONFIG_DIR seeding.

``ensure_isolated_config_dir`` prepares the per-subprocess CLAUDE_CONFIG_DIR
ANNA points her spawned CLIs at, relocating host CLAUDE.md / skills / plugins /
local MCP discovery off the operator's dir. Credentials are NOT seeded here;
they are resolved separately via ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` (the
operator's real ``~/.claude``) so max-mode OAuth reads and the refresh-write
share the operator's ``.credentials.json`` directly. The dir must also clean up
any stale ``.credentials.json`` left over from the previous symlink-seeding
design.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from anna.auth import (
    ensure_isolated_config_dir,
    operator_credentials_path,
    operator_securestorage_dir,
)


def _seed_host_credentials(home: Path) -> Path:
    """Create a fake ~/.claude/.credentials.json under the monkeypatched home."""
    source = home / ".claude" / ".credentials.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"token": "abc"}', encoding="utf-8")
    return source


def test_operator_paths_derive_from_single_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The securestorage dir is exactly the parent of the credentials file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    creds = operator_credentials_path()
    securestorage = operator_securestorage_dir()

    assert creds == home / ".claude" / ".credentials.json"
    assert securestorage == home / ".claude"
    # The securestorage dir is derived as the credentials file's parent, never
    # duplicated as a separate literal.
    assert securestorage == creds.parent


def test_ensure_isolated_config_dir_max_creates_0700_no_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    result = ensure_isolated_config_dir(runtime_dir, "max")

    assert result == runtime_dir
    assert runtime_dir.is_dir()
    mode = stat.S_IMODE(os.stat(runtime_dir).st_mode)
    assert mode == 0o700

    # No credentials symlink (or file) is seeded under the new design.
    cred = runtime_dir / ".credentials.json"
    assert not cred.is_symlink()
    assert not cred.exists()


def test_ensure_isolated_config_dir_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    ensure_isolated_config_dir(runtime_dir, "max")
    # Second call must not raise and must leave the dir clean of credentials.
    ensure_isolated_config_dir(runtime_dir, "max")

    cred = runtime_dir / ".credentials.json"
    assert not cred.exists()
    assert not cred.is_symlink()
    assert stat.S_IMODE(os.stat(runtime_dir).st_mode) == 0o700


def test_ensure_isolated_config_dir_api_key_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Even with host credentials present, api_key mode seeds nothing.
    _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    ensure_isolated_config_dir(runtime_dir, "api_key")

    assert runtime_dir.is_dir()
    assert stat.S_IMODE(os.stat(runtime_dir).st_mode) == 0o700
    assert not (runtime_dir / ".credentials.json").exists()


def test_ensure_isolated_config_dir_max_missing_source_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # No host credentials seeded.

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    # Must not raise even though max mode wants credentials that don't exist.
    result = ensure_isolated_config_dir(runtime_dir, "max")

    assert result == runtime_dir
    assert runtime_dir.is_dir()
    assert not (runtime_dir / ".credentials.json").exists()


def test_ensure_isolated_config_dir_removes_stale_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover credentials symlink from the old design is removed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Simulate the pre-fix state: a symlink seeded by the old code.
    link = runtime_dir / ".credentials.json"
    link.symlink_to(source)
    assert link.is_symlink()

    ensure_isolated_config_dir(runtime_dir, "max")

    assert not link.is_symlink()
    assert not link.exists()
    # Critical safety property: unlinking the symlink must NOT follow it and
    # delete the operator's real shared credentials file.
    assert source.exists()


def test_ensure_isolated_config_dir_removes_stale_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A previously-clobbered real .credentials.json in the runtime dir is removed.

    This is the exact leftover the bug produced: the CLI's refresh replaced the
    symlink with a standalone file. It must not be allowed to shadow the
    securestorage-resolved credentials.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stale = runtime_dir / ".credentials.json"
    stale.write_text('{"token": "divorced"}', encoding="utf-8")
    assert stale.is_file() and not stale.is_symlink()

    ensure_isolated_config_dir(runtime_dir, "max")

    assert not stale.exists()


def test_ensure_isolated_config_dir_removes_stale_directory_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # A real directory squatting at the credential path must not fail boot.
    target = runtime_dir / ".credentials.json"
    target.mkdir()
    (target / "junk").write_text("x", encoding="utf-8")

    # Must not raise (no IsADirectoryError); the dir is removed.
    result = ensure_isolated_config_dir(runtime_dir, "max")

    assert result == runtime_dir
    assert not target.exists()
