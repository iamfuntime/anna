"""Tests for ``anna admin merge-checkpoints``.

Subtask 12 of the Phase 2 §5 CLI Transport plan. Adds a second
subcommand alongside the Phase 1 ``unpoison`` command on the existing
``anna.cli.admin`` click group; these tests exercise the migration path
operators run once per identity alias at setup time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import structlog
from click.testing import CliRunner

from anna.cli import admin as admin_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def anna_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME and ANNA_HOME at tmp_path so ``load_config`` returns
    defaults rooted under the test directory.

    The vault default is ``~/anna/vault`` and ``audit`` lives at
    ``~/anna/audit`` (via ``anna_home / "audit"``). Setting HOME to
    tmp_path keeps both inside the test sandbox.

    No ``configure_logging`` monkeypatch is needed: the admin subcommands
    guard their logging configuration with ``_is_test_run()`` (detects
    ``PYTEST_CURRENT_TEST``), so test invocations never mutate global
    structlog state. See the regression test at the bottom of this file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANNA_HOME", str(tmp_path / "anna"))
    # Make sure no operator anna.yaml at $PWD gets picked up.
    monkeypatch.delenv("ANNA_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "anna"
    home.mkdir(parents=True, exist_ok=True)
    (home / "vault" / "Conversations").mkdir(parents=True, exist_ok=True)
    (home / "audit").mkdir(parents=True, exist_ok=True)
    return home


def _read_audit_records(audit_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write_checkpoint(dirpath: Path, name: str, body: str = "x") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Happy path: 3 files move, source dir is removed, audit event lands.
# ---------------------------------------------------------------------------


def test_merge_happy_path_moves_files_and_emits_audit(anna_home: Path) -> None:
    vault = anna_home / "vault"
    source = vault / "Conversations" / "slack-dm-USP2QLB41"
    _write_checkpoint(source, "2026-05-30-0900.md", "morning")
    _write_checkpoint(source, "2026-05-31-1400.md", "afternoon")
    _write_checkpoint(source, "2026-06-01-0700.md", "today")

    runner = CliRunner()
    result = runner.invoke(
        admin_module.main,
        [
            "merge-checkpoints",
            "--canonical",
            "seth",
            "--from",
            "slack:dm:USP2QLB41",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    dest = vault / "Conversations" / "user-seth"
    assert dest.is_dir()
    names = sorted(p.name for p in dest.iterdir())
    assert names == [
        "2026-05-30-0900.md",
        "2026-05-31-1400.md",
        "2026-06-01-0700.md",
    ]
    # Move (not copy) means the source dir is gone after the run.
    assert not source.exists(), "source dir should be removed after move"

    records = _read_audit_records(anna_home / "audit")
    merge_events = [r for r in records if r["event"] == "audit.admin.merge_checkpoints"]
    assert len(merge_events) == 1
    ev = merge_events[0]
    assert ev["actor"] == "operator"
    assert ev["source_conv_key"] == "slack:dm:USP2QLB41"
    assert ev["dest_canonical"] == "seth"
    assert ev["file_count"] == 3
    assert ev["mode"] == "move"
    assert ev["total_bytes"] > 0


# ---------------------------------------------------------------------------
# 2. --dry-run: nothing moves, plan prints to stdout, exit 0.
# ---------------------------------------------------------------------------


def test_merge_dry_run_does_not_touch_filesystem(anna_home: Path) -> None:
    vault = anna_home / "vault"
    source = vault / "Conversations" / "slack-dm-USP2QLB41"
    _write_checkpoint(source, "2026-05-30-0900.md")
    _write_checkpoint(source, "2026-05-31-1400.md")

    runner = CliRunner()
    result = runner.invoke(
        admin_module.main,
        [
            "merge-checkpoints",
            "--canonical",
            "seth",
            "--from",
            "slack:dm:USP2QLB41",
            "--dry-run",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "2026-05-30-0900.md" in result.output
    assert "2026-05-31-1400.md" in result.output

    # Source untouched.
    source_files = sorted(p.name for p in source.iterdir())
    assert source_files == ["2026-05-30-0900.md", "2026-05-31-1400.md"]

    # Destination dir never created (dry-run must not write anywhere).
    dest = vault / "Conversations" / "user-seth"
    assert not dest.exists()

    # The dry-run audit event is still emitted (the operator did something
    # auditable; mode="dry-run" tells the reader it was a no-op).
    records = _read_audit_records(anna_home / "audit")
    merge_events = [r for r in records if r["event"] == "audit.admin.merge_checkpoints"]
    assert len(merge_events) == 1
    assert merge_events[0]["mode"] == "dry-run"
    assert merge_events[0]["file_count"] == 2


# ---------------------------------------------------------------------------
# 3. --keep-original: source files remain, destination has copies, mtimes
#    preserved.
# ---------------------------------------------------------------------------


def test_merge_keep_original_copies_with_mtime(anna_home: Path) -> None:
    vault = anna_home / "vault"
    source = vault / "Conversations" / "slack-dm-USP2QLB41"
    file_a = _write_checkpoint(source, "2026-05-30-0900.md", "morning")
    file_b = _write_checkpoint(source, "2026-05-31-1400.md", "afternoon")

    # Stamp the source files with a known mtime so we can assert
    # ``shutil.copy2`` preserved it.
    fixed_mtime = 1_700_000_000.0
    os.utime(file_a, (fixed_mtime, fixed_mtime))
    os.utime(file_b, (fixed_mtime, fixed_mtime))

    runner = CliRunner()
    result = runner.invoke(
        admin_module.main,
        [
            "merge-checkpoints",
            "--canonical",
            "seth",
            "--from",
            "slack:dm:USP2QLB41",
            "--keep-original",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    # Source still present.
    assert source.is_dir()
    source_files = sorted(p.name for p in source.iterdir())
    assert source_files == ["2026-05-30-0900.md", "2026-05-31-1400.md"]

    # Destination has the copies.
    dest = vault / "Conversations" / "user-seth"
    dest_files = sorted(p.name for p in dest.iterdir())
    assert dest_files == ["2026-05-30-0900.md", "2026-05-31-1400.md"]

    # mtimes preserved (shutil.copy2).
    for name in dest_files:
        assert (dest / name).stat().st_mtime == pytest.approx(fixed_mtime, abs=1.0)

    records = _read_audit_records(anna_home / "audit")
    merge_events = [r for r in records if r["event"] == "audit.admin.merge_checkpoints"]
    assert len(merge_events) == 1
    assert merge_events[0]["mode"] == "copy"
    assert merge_events[0]["file_count"] == 2


# ---------------------------------------------------------------------------
# 4. Refuse-on-collision: exit 1, name the colliding file in stderr, no
#    filesystem mutation.
# ---------------------------------------------------------------------------


def test_merge_refuses_on_collision(anna_home: Path) -> None:
    vault = anna_home / "vault"
    source = vault / "Conversations" / "slack-dm-USP2QLB41"
    _write_checkpoint(source, "2026-05-30-0900.md", "source-version")
    _write_checkpoint(source, "2026-05-31-1400.md", "source-version")

    dest = vault / "Conversations" / "user-seth"
    # The destination already has one file with the same name as a source
    # file. The command must refuse to merge without moving anything.
    _write_checkpoint(dest, "2026-05-30-0900.md", "dest-version")

    runner = CliRunner()
    result = runner.invoke(
        admin_module.main,
        [
            "merge-checkpoints",
            "--canonical",
            "seth",
            "--from",
            "slack:dm:USP2QLB41",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    # The colliding filename is mentioned in stderr. click 8.2+ keeps
    # ``result.stderr`` separate from ``result.stdout`` by default.
    assert "2026-05-30-0900.md" in result.stderr
    assert "Refusing to merge" in result.stderr

    # Source untouched.
    source_files = sorted(p.name for p in source.iterdir())
    assert source_files == ["2026-05-30-0900.md", "2026-05-31-1400.md"]
    assert source.joinpath("2026-05-30-0900.md").read_text(encoding="utf-8") == "source-version"

    # Destination unchanged: only the pre-existing collider file.
    dest_files = sorted(p.name for p in dest.iterdir())
    assert dest_files == ["2026-05-30-0900.md"]
    assert dest.joinpath("2026-05-30-0900.md").read_text(encoding="utf-8") == "dest-version"

    # No merge audit event landed; the operation was refused before any
    # filesystem work.
    records = _read_audit_records(anna_home / "audit")
    merge_events = [r for r in records if r["event"] == "audit.admin.merge_checkpoints"]
    assert merge_events == []


# ---------------------------------------------------------------------------
# 5. Empty / missing source dir: "no files to merge", exit 0, no audit event.
# ---------------------------------------------------------------------------


def test_merge_missing_source_dir_is_idempotent(anna_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        admin_module.main,
        [
            "merge-checkpoints",
            "--canonical",
            "seth",
            "--from",
            "slack:dm:NOTAREALUSER",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "no files to merge" in result.output

    records = _read_audit_records(anna_home / "audit")
    merge_events = [r for r in records if r["event"] == "audit.admin.merge_checkpoints"]
    assert merge_events == [], (
        "no audit event should land when there was no work to do"
    )


# ---------------------------------------------------------------------------
# 6. Regression: admin invocation must not poison worker logging.
#
# The admin click group's callback used to call ``configure_logging``
# unconditionally, mutating global structlog state on every CliRunner
# invocation and breaking later worker tests in the same pytest process
# (PrintLogger has no ``.name``, etc.). The fix moved the call into each
# subcommand behind an ``_is_test_run()`` guard. This test locks that in:
# invoke a subcommand, then build a worker and exercise structlog defaults.
# ---------------------------------------------------------------------------


def test_admin_invocation_does_not_poison_worker_logging(
    anna_home: Path, tmp_path: Path
) -> None:
    from anna.config import AnnaConfig
    from anna.runtime.supervisor import Supervisor
    from anna.runtime.worker import ConversationWorker

    vault = anna_home / "vault"
    source = vault / "Conversations" / "slack-dm-USP2QLB41"
    _write_checkpoint(source, "2026-05-30-0900.md", "morning")

    structlog.reset_defaults()
    try:
        runner = CliRunner()
        result = runner.invoke(
            admin_module.main,
            [
                "merge-checkpoints",
                "--canonical",
                "seth",
                "--from",
                "slack:dm:USP2QLB41",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # The subcommand ran under pytest, so the _is_test_run() guard must
        # have kept structlog on its package defaults.
        assert not structlog.is_configured(), (
            "admin subcommand reconfigured structlog during a test run"
        )

        # A worker constructed in the same process logs fine on the default
        # (PrintLogger-backed) configuration — this is the exact shape that
        # used to break (add_logger_name on a PrintLogger, etc.).
        cfg = AnnaConfig()
        object.__setattr__(cfg, "anna_home", tmp_path / "worker_home")
        cfg.vault.path = str(tmp_path / "worker_vault")
        cfg.core_dir.mkdir(parents=True, exist_ok=True)
        supervisor = Supervisor(config=cfg)

        async def _noop_send(_msg):
            return None

        worker = ConversationWorker(
            conversation_key="slack:dm:UTEST",
            transport="slack",
            config=cfg,
            supervisor=supervisor,
            send=_noop_send,
        )
        assert worker is not None

        # Emitting through structlog defaults must not raise.
        structlog.get_logger("anna.runtime.worker").info(
            "post_admin_worker_logging_ok", conv_key="slack:dm:UTEST"
        )
        assert not structlog.is_configured()
    finally:
        # Leave the process on structlog defaults for whatever test runs next.
        structlog.reset_defaults()
