"""Validate scripts/seed_vault.sh creates the expected layout idempotently."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "seed_vault.sh"


EXPECTED_DIRS = (
    "Conversations",
    "Daily",
    "Topics",
    "Episodic",
    "Identity",
    "SubAgents",
    "agents",
    "skills",
)


def _run(vault_path: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ANNA_VAULT_PATH"] = str(vault_path)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing seed script at {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), "seed script should be executable"


def test_seed_creates_layout(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    result = _run(vault)
    assert result.returncode == 0
    for d in EXPECTED_DIRS:
        assert (vault / d).is_dir(), f"missing dir {d}"
    assert (vault / "INDEX.md").is_file()
    body = (vault / "INDEX.md").read_text(encoding="utf-8")
    assert "ANNA Vault" in body
    assert "Conversations/" in body


def test_seed_is_idempotent_and_preserves_existing_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _run(vault)

    # Plant operator content inside each seeded dir, plus a customized INDEX.md.
    fingerprints: dict[Path, str] = {}
    for d in EXPECTED_DIRS:
        marker = vault / d / "operator-note.md"
        marker.write_text(f"operator content in {d}", encoding="utf-8")
        fingerprints[marker] = marker.read_text(encoding="utf-8")
    index = vault / "INDEX.md"
    custom_index = "# my customized index\n"
    index.write_text(custom_index, encoding="utf-8")
    fingerprints[index] = custom_index

    # Re-run the seeder. Nothing should be touched.
    _run(vault)

    for path, expected in fingerprints.items():
        assert path.is_file(), f"seed run wiped {path}"
        assert path.read_text(encoding="utf-8") == expected, f"seed run modified {path}"


def test_seed_resolves_from_anna_yaml(tmp_path: Path) -> None:
    anna_home = tmp_path / "anna_home"
    anna_home.mkdir()
    vault = tmp_path / "vault-from-yaml"
    (anna_home / "anna.yaml").write_text(
        f"paths:\n  vault_path: {vault}\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("ANNA_VAULT_PATH", None)
    env["ANNA_HOME"] = str(anna_home)
    subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    for d in EXPECTED_DIRS:
        assert (vault / d).is_dir()
    assert (vault / "INDEX.md").is_file()
