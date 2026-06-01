"""Validate that the worker's system prompt includes recent checkpoints.

Per v3 §6, the worker reads the two most recent checkpoint files for the
conversation key on resume and splices them into the system prompt as
``# Recent checkpoints (resume context)``, oldest first. With zero
checkpoints, the section is omitted entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker


CONV_KEY = "slack:dm:UTEST"


def _make_worker(tmp_path: Path) -> ConversationWorker:
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
