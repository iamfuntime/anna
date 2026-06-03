"""Tests for auth-path resolution and isolated CLAUDE_CONFIG_DIR seeding.

``ensure_isolated_config_dir`` prepares the per-subprocess CLAUDE_CONFIG_DIR
ANNA points her spawned CLIs at, seeded with only a symlink to the real
``~/.claude/.credentials.json`` so max-mode OAuth still resolves while host
CLAUDE.md / skills / plugins / local MCP discovery is relocated off the
operator's dir.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from anna.auth import ensure_isolated_config_dir


def _seed_host_credentials(home: Path) -> Path:
    """Create a fake ~/.claude/.credentials.json under the monkeypatched home."""
    source = home / ".claude" / ".credentials.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"token": "abc"}', encoding="utf-8")
    return source


def test_ensure_isolated_config_dir_max_creates_0700_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    result = ensure_isolated_config_dir(runtime_dir, "max")

    assert result == runtime_dir
    assert runtime_dir.is_dir()
    mode = stat.S_IMODE(os.stat(runtime_dir).st_mode)
    assert mode == 0o700

    link = runtime_dir / ".credentials.json"
    assert link.is_symlink()
    assert Path(os.readlink(link)) == source


def test_ensure_isolated_config_dir_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    ensure_isolated_config_dir(runtime_dir, "max")
    # Second call must not raise and must leave the symlink intact / self-healed.
    ensure_isolated_config_dir(runtime_dir, "max")

    link = runtime_dir / ".credentials.json"
    assert link.is_symlink()
    assert Path(os.readlink(link)) == source
    assert stat.S_IMODE(os.stat(runtime_dir).st_mode) == 0o700


def test_ensure_isolated_config_dir_api_key_skips_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Even with host credentials present, api_key mode does not seed the symlink.
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


def test_ensure_isolated_config_dir_self_heals_broken_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Pre-create a broken symlink: points at a path that does not exist.
    link = runtime_dir / ".credentials.json"
    link.symlink_to(tmp_path / "nonexistent" / ".credentials.json")
    assert link.is_symlink()
    assert not link.exists()  # broken

    ensure_isolated_config_dir(runtime_dir, "max")

    assert link.is_symlink()
    assert Path(os.readlink(link)) == source
    assert link.exists()  # now resolves to the real source


def test_ensure_isolated_config_dir_directory_at_target_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _seed_host_credentials(home)

    runtime_dir = tmp_path / "anna" / ".claude-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # A real directory squatting at the credential target must not fail boot.
    target = runtime_dir / ".credentials.json"
    target.mkdir()
    (target / "junk").write_text("x", encoding="utf-8")

    # Must not raise (no IsADirectoryError); the dir is removed and the symlink
    # takes its place.
    result = ensure_isolated_config_dir(runtime_dir, "max")

    assert result == runtime_dir
    assert target.is_symlink()
    assert Path(os.readlink(target)) == source
