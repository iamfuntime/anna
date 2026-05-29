"""``anna-admin`` CLI.

Operator recovery commands. Phase 1 ships one:

* ``anna-admin unpoison <file>``: clear the poison flag on a core identity
  file the supervisor refused to keep writing to. Emits
  ``audit.supervisor.unpoisoned`` with ``actor=operator``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from anna.log import audit_event, configure_logging


def _anna_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")))


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


if __name__ == "__main__":
    main()
