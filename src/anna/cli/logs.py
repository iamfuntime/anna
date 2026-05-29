"""``anna-logs`` CLI wrapper.

Per v3 section 7. Three modes:

* Default: pulls operational events from the user journal via journalctl.
* ``--audit``: reads ``$ANNA_HOME/audit/*.jsonl`` directly.
* ``--transcript <conv_key>``: reads ``$ANNA_HOME/transcripts/<key>/*.jsonl``.

Falls back to ``python -m json.tool`` when ``jq`` is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from anna.vault.audit import iter_audit_events, list_audit_files
from anna.vault.transcripts import iter_transcript_lines, list_conversations


def _anna_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("ANNA_HOME", "~/anna")))


def _parse_since(since: str) -> datetime | None:
    """Parse a since string into an aware datetime.

    Accepts: ``1h``, ``30m``, ``7d``, ``today``, or ISO-formatted timestamps.
    """
    if not since:
        return None
    s = since.strip().lower()
    if s == "today":
        now = datetime.now(timezone.utc).astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s.endswith(("h", "m", "d")):
        try:
            value = int(s[:-1])
        except ValueError:
            return None
        if s.endswith("h"):
            return datetime.now(timezone.utc) - timedelta(hours=value)
        if s.endswith("m"):
            return datetime.now(timezone.utc) - timedelta(minutes=value)
        if s.endswith("d"):
            return datetime.now(timezone.utc) - timedelta(days=value)
    try:
        return datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pretty_print(record: dict) -> None:
    if shutil.which("jq"):
        proc = subprocess.run(
            ["jq", "."], input=json.dumps(record), text=True, capture_output=True
        )
        click.echo(proc.stdout, nl=False)
    else:
        click.echo(json.dumps(record, indent=2))


@click.command()
@click.option("--follow", "-f", is_flag=True, help="Tail the operational stream (journalctl -f).")
@click.option("--since", default="", help="Filter to a time window: '1h', '7d', 'today', or ISO timestamp.")
@click.option(
    "--level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    default=None,
    help="Filter the operational stream to this level and above.",
)
@click.option("--grep", "grep_pattern", default="", help="Substring filter applied to operational stream output.")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON suitable for jq pipelines.")
@click.option("--audit", is_flag=True, help="Read the audit stream instead of the operational stream.")
@click.option("--event", "event_filter", default="", help="Filter audit events by exact event name.")
@click.option(
    "--transcript",
    default=None,
    help="Conversation directory name (e.g., slack-dm-U0ABCD123) to read transcripts for.",
)
@click.option("--list", "list_mode", is_flag=True, help="With --transcript, list every conv with transcripts on disk.")
def main(
    follow: bool,
    since: str,
    level: str | None,
    grep_pattern: str,
    json_out: bool,
    audit: bool,
    event_filter: str,
    transcript: str | None,
    list_mode: bool,
) -> int:
    """Read ANNA's logs.

    The default command shows the last 100 operational lines. Use ``--follow``
    for live tailing, ``--audit`` for the audit log, or ``--transcript`` to
    look at the raw conversation transcripts.
    """
    home = _anna_home()
    audit_dir = home / "audit"
    transcripts_dir = home / "transcripts"

    # -----------------------
    # Transcript mode
    # -----------------------
    if transcript is not None or list_mode:
        if list_mode:
            for name in list_conversations(transcripts_dir):
                click.echo(name)
            return 0
        days = None
        since_dt = _parse_since(since) if since else None
        if since_dt is not None:
            delta = datetime.now(timezone.utc) - since_dt
            days = max(1, int(delta.total_seconds() // 86400) + 1)
        for record in iter_transcript_lines(
            transcripts_dir=transcripts_dir,
            conv_dir_name=transcript or "",
            days=days,
        ):
            if json_out:
                click.echo(json.dumps(record))
            else:
                _pretty_print(record)
        return 0

    # -----------------------
    # Audit mode
    # -----------------------
    if audit:
        if not list_audit_files(audit_dir):
            click.echo(f"No audit files found under {audit_dir}", err=True)
            return 0
        since_dt = _parse_since(since) if since else None
        for record in iter_audit_events(
            audit_dir=audit_dir,
            since=since_dt,
            event_filter=event_filter or None,
        ):
            if json_out:
                click.echo(json.dumps(record))
            else:
                _pretty_print(record)
        return 0

    # -----------------------
    # Operational stream (journalctl)
    # -----------------------
    cmd = ["journalctl", "--user", "-u", "anna"]
    if follow:
        cmd.append("-f")
    else:
        cmd.extend(["-n", "100"])
    if since:
        cmd.extend(["--since", since])
    if level:
        # journalctl --priority takes a syslog priority. Map our levels.
        prio = {
            "DEBUG": "7",
            "INFO": "6",
            "WARNING": "4",
            "ERROR": "3",
            "CRITICAL": "2",
        }[level.upper()]
        cmd.extend(["-p", prio])
    if json_out:
        cmd.extend(["-o", "json"])
    else:
        cmd.extend(["-o", "cat"])

    if grep_pattern:
        # Pipe to grep without spawning a shell, to avoid escaping headaches.
        jc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        gr = subprocess.Popen(["grep", "--line-buffered", grep_pattern], stdin=jc.stdout)
        jc.stdout.close()  # type: ignore[union-attr]
        return gr.wait()

    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
