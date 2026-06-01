"""``anna-admin`` CLI.

Operator recovery commands:

* ``anna-admin unpoison <file>``: clear the poison flag on a core identity
  file the supervisor refused to keep writing to. Emits
  ``audit.supervisor.unpoisoned`` with ``actor=operator``.
* ``anna-admin merge-checkpoints --canonical <name> --from <conv_key>``:
  migrate per-transport conversation checkpoints into the unified
  ``user:<canonical>`` directory after configuring an identity alias.
  Emits ``audit.admin.merge_checkpoints`` with ``actor=operator``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import click

from anna.config import load_config
from anna.log import audit_event, configure_logging


def _anna_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")))


def _safe_conv_key(conv_key: str) -> str:
    """Convert a conv_key into the filesystem-safe directory-name form.

    Mirrors the inline conversion in ``anna.vault.checkpoint`` (both
    ``write_checkpoint`` and ``list_recent_checkpoints``) and
    ``anna.log._transcript_dir_for``. Kept in lockstep with those call
    sites so the merge command writes to the same path the worker writes
    to. (See punted note in subtask 12 about folding the three inline
    copies into a single named helper.)
    """
    return conv_key.replace(":", "-").replace("/", "_")


@click.group()
def main() -> None:
    """ANNA operator administration."""
    configure_logging(level="INFO", format="json")


@main.command()
@click.argument("file")
def unpoison(file: str) -> None:
    """Clear the supervisor poison flag for FILE (e.g., SOUL.md).

    Reads the supervisor state file directly, removes the entry, and emits an
    audit event. The running process picks up the change on its next core
    write attempt because it reloads the state file each time.
    """
    home = _anna_home()
    state_path = home / "supervisor-state.json"
    audit_dir = home / "audit"

    if not state_path.exists():
        click.secho(f"No supervisor state at {state_path}; nothing to unpoison.", fg="yellow")
        sys.exit(0)

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        click.secho(f"Failed to read {state_path}: {exc}", fg="red")
        sys.exit(2)

    poisoned = set(data.get("poisoned", []))
    if file not in poisoned:
        click.secho(f"{file} is not currently poisoned.", fg="yellow")
        sys.exit(0)

    poisoned.discard(file)
    data["poisoned"] = sorted(poisoned)
    state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    audit_event(
        "audit.supervisor.unpoisoned",
        audit_dir=audit_dir,
        actor="operator",
        fsync_on_write=True,
        file=file,
    )
    click.secho(f"Cleared poison flag on {file}.", fg="green")


@main.command()
def status() -> None:
    """Show the supervisor's poison state."""
    home = _anna_home()
    state_path = home / "supervisor-state.json"
    if not state_path.exists():
        click.echo("supervisor-state.json not present; nothing poisoned.")
        return
    data = json.loads(state_path.read_text(encoding="utf-8"))
    poisoned = data.get("poisoned", [])
    if not poisoned:
        click.echo("No files currently poisoned.")
        return
    click.echo("Poisoned core files:")
    for name in poisoned:
        click.echo(f"  - {name}")


@main.command(name="merge-checkpoints")
@click.option(
    "--canonical",
    required=True,
    help="Identity canonical name (becomes ``user:<canonical>`` on disk).",
)
@click.option(
    "--from",
    "from_key",
    required=True,
    help="The pre-alias conv_key, e.g. slack:dm:USP2QLB41",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would move without touching the filesystem.",
)
@click.option(
    "--keep-original",
    is_flag=True,
    default=False,
    help="Copy instead of move; original files stay in place.",
)
def merge_checkpoints(
    canonical: str,
    from_key: str,
    dry_run: bool,
    keep_original: bool,
) -> None:
    """Migrate per-transport checkpoints into ``user:<canonical>``.

    After adding an ``identities`` entry to ``anna.yaml`` (Phase 2 §5
    decision #3), the router rewrites future conv_keys to
    ``user:<canonical>`` but existing per-transport checkpoint files at
    ``vault/Conversations/<safe(from_key)>/`` are orphaned from the
    resume-context reader's perspective. This command moves (or copies)
    those files into ``vault/Conversations/user-<canonical>/`` so the
    next worker that resumes ``user:<canonical>`` can read them.

    Refuses to run if any filename in the source directory already
    exists at the destination; the operator must resolve manually.
    """
    config = load_config()
    vault_root = config.vault.resolved_path
    audit_dir = config.audit_dir

    source_dir = vault_root / "Conversations" / _safe_conv_key(from_key)
    dest_key = f"user:{canonical}"
    dest_dir = vault_root / "Conversations" / _safe_conv_key(dest_key)

    if not source_dir.is_dir():
        click.secho(
            f"no files to merge: source dir does not exist at {source_dir}",
            fg="yellow",
        )
        sys.exit(0)

    source_files = sorted(p for p in source_dir.iterdir() if p.is_file())
    if not source_files:
        click.secho(f"no files to merge: {source_dir} is empty", fg="yellow")
        sys.exit(0)

    # Refuse-on-collision: walk the source and check every filename against
    # the (possibly already-existing) destination. We do this BEFORE any
    # filesystem mutation so a partial migration cannot leave the operator
    # with files split across two directories.
    if dest_dir.exists():
        collisions = sorted(
            p.name for p in source_files if (dest_dir / p.name).exists()
        )
        if collisions:
            click.secho(
                "Refusing to merge: the destination directory already "
                "contains files with the same names as the source.",
                fg="red",
                err=True,
            )
            click.secho("Colliding filenames:", fg="red", err=True)
            for name in collisions:
                click.secho(f"  - {name}", fg="red", err=True)
            click.secho(
                "",
                err=True,
            )
            click.secho(
                f"Source:      {source_dir}",
                err=True,
            )
            click.secho(
                f"Destination: {dest_dir}",
                err=True,
            )
            click.secho(
                "",
                err=True,
            )
            click.secho(
                "Resolve manually: inspect the colliding files (e.g., "
                "``diff`` the duplicates, ``mv`` the unique ones), then "
                "re-run. ``--dry-run`` shows what would move without "
                "acting.",
                err=True,
            )
            sys.exit(1)

    file_count = len(source_files)
    total_bytes = sum(p.stat().st_size for p in source_files)

    if dry_run:
        click.echo(f"[dry-run] {file_count} file(s) would move from {source_dir}:")
        for p in source_files:
            click.echo(f"  {p.name}  ->  {dest_dir / p.name}")
        click.echo(f"[dry-run] total bytes: {total_bytes}")
        audit_event(
            "audit.admin.merge_checkpoints",
            audit_dir=audit_dir,
            actor="operator",
            fsync_on_write=True,
            source_conv_key=from_key,
            dest_canonical=canonical,
            file_count=file_count,
            total_bytes=total_bytes,
            mode="dry-run",
        )
        sys.exit(0)

    # Real run: create destination dir if needed and transfer each file.
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in source_files:
        target = dest_dir / p.name
        if keep_original:
            shutil.copy2(str(p), str(target))
        else:
            shutil.move(str(p), str(target))

    mode = "copy" if keep_original else "move"

    # If we moved (didn't copy), clean up the now-empty source dir. If a
    # stray non-file (subdir) or unmoved entry remains, leave it alone and
    # report it so the operator can investigate.
    leftover: list[str] = []
    if not keep_original and source_dir.is_dir():
        remaining = list(source_dir.iterdir())
        if not remaining:
            try:
                os.rmdir(source_dir)
            except OSError as exc:
                # Race or permission issue — surface it but don't fail
                # the operation since the files did move.
                click.secho(
                    f"Note: could not remove empty source dir {source_dir}: {exc}",
                    fg="yellow",
                )
        else:
            leftover = sorted(p.name for p in remaining)
            click.secho(
                f"Note: source dir {source_dir} still contains "
                f"{len(leftover)} non-checkpoint entr"
                f"{'y' if len(leftover) == 1 else 'ies'}; leaving in place.",
                fg="yellow",
            )
            for name in leftover:
                click.secho(f"  - {name}", fg="yellow")

    audit_event(
        "audit.admin.merge_checkpoints",
        audit_dir=audit_dir,
        actor="operator",
        fsync_on_write=True,
        source_conv_key=from_key,
        dest_canonical=canonical,
        file_count=file_count,
        total_bytes=total_bytes,
        mode=mode,
    )

    verb = "Copied" if keep_original else "Moved"
    click.secho(
        f"{verb} {file_count} file(s) ({total_bytes} bytes) "
        f"from {source_dir} to {dest_dir}.",
        fg="green",
    )


if __name__ == "__main__":
    main()
