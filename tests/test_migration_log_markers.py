"""Pin the journal marker strings scripts/migrate-to-uv-tool.sh greps for.

The migration script's transport-connectivity gate (step 10) tails
``journalctl --user -u anna`` and greps for literal structlog event strings
emitted by the transport adapters:

* ``channel.connected``               (slack, telegram, cli — success marker)
* ``channel.token_missing``           (slack, telegram — fail-fast marker)
* ``audit.transport.token_missing``   (slack, telegram — journald audit mirror)

If a marker is renamed in the Python source without updating the script (or
vice versa) the gate silently stops gating — exactly the silent-degradation
failure mode it exists to catch (2026-06-02). These tests read both sides
and fail CI on drift, and also source the script's bash helpers to prove the
grep patterns match real structlog-rendered JSON lines.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate-to-uv-tool.sh"
TRANSPORTS_DIR = REPO_ROOT / "src" / "anna" / "transports"

CONNECTED_MARKER = "channel.connected"
TOKEN_MISSING_LOG_MARKER = "channel.token_missing"
TOKEN_MISSING_AUDIT_MARKER = "audit.transport.token_missing"

CONNECTED_SITES = [
    ("slack.py", "slack"),
    ("telegram.py", "telegram"),
    ("cli.py", "cli"),
]
TOKEN_MISSING_SITES = [
    ("slack.py", "slack"),
    ("telegram.py", "telegram"),
]


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _source_text(filename: str) -> str:
    return (TRANSPORTS_DIR / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Python side: the transports still emit the exact markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename,channel", CONNECTED_SITES)
def test_connected_marker_emitted_by_transport(filename: str, channel: str) -> None:
    """Each transport logs `channel.connected` with its channel kwarg."""
    pattern = rf'"{re.escape(CONNECTED_MARKER)}",\s*channel="{channel}"'
    assert re.search(pattern, _source_text(filename)), (
        f"{filename} no longer logs the literal '{CONNECTED_MARKER}' event with "
        f'channel="{channel}". scripts/migrate-to-uv-tool.sh greps journalctl for '
        "this exact string as its transport gate — update both together."
    )


@pytest.mark.parametrize("filename,channel", TOKEN_MISSING_SITES)
def test_token_missing_log_marker_emitted_by_transport(filename: str, channel: str) -> None:
    """Tokenless boots log the `channel.token_missing` WARNING."""
    pattern = rf'"{re.escape(TOKEN_MISSING_LOG_MARKER)}",\s*channel="{channel}"'
    assert re.search(pattern, _source_text(filename)), (
        f"{filename} no longer logs the literal '{TOKEN_MISSING_LOG_MARKER}' event "
        f'with channel="{channel}". scripts/migrate-to-uv-tool.sh fails fast on this '
        "marker — update both together."
    )


@pytest.mark.parametrize("filename,channel", TOKEN_MISSING_SITES)
def test_token_missing_audit_marker_emitted_by_transport(filename: str, channel: str) -> None:
    """Tokenless boots also write the audit event (mirrored to journald)."""
    src = _source_text(filename)
    assert f'"{TOKEN_MISSING_AUDIT_MARKER}"' in src, (
        f"{filename} no longer emits the literal '{TOKEN_MISSING_AUDIT_MARKER}' audit "
        "event. scripts/migrate-to-uv-tool.sh greps its journald mirror — update both "
        "together."
    )


# ---------------------------------------------------------------------------
# Bash side: the migration script still greps for the same markers
# ---------------------------------------------------------------------------


def test_script_greps_connected_marker() -> None:
    assert f'"{CONNECTED_MARKER}"' in _script_text(), (
        f"scripts/migrate-to-uv-tool.sh no longer greps for '{CONNECTED_MARKER}'. "
        "The transport gate must match the marker the transports emit."
    )


def test_script_greps_token_missing_markers() -> None:
    # The script uses grep -E with escaped dots; strip backslashes so the
    # assertion tolerates regex escaping in the pattern.
    deslashed = _script_text().replace("\\", "")
    for marker in (TOKEN_MISSING_LOG_MARKER, TOKEN_MISSING_AUDIT_MARKER):
        assert marker in deslashed, (
            f"scripts/migrate-to-uv-tool.sh no longer greps for '{marker}'. "
            "The fail-fast branch of the transport gate depends on it."
        )


# ---------------------------------------------------------------------------
# End-to-end: the script's bash helpers match structlog-rendered JSON lines
# ---------------------------------------------------------------------------


def _render(event: str, channel: str, **fields: object) -> str:
    """Render a journal MESSAGE line the way the daemon does.

    Uses structlog's JSONRenderer (the production renderer wired in
    src/anna/log.py) so the test breaks if a renderer change alters the
    on-the-wire shape the script greps.
    """
    structlog = pytest.importorskip("structlog")
    event_dict = {
        "channel": channel,
        **fields,
        "event": event,
        "logger": f"anna.transports.{channel}",
        "level": "info",
        "timestamp": "2026-06-11T00:00:00.000000Z",
    }
    return str(structlog.processors.JSONRenderer()(None, "", event_dict))


def _bash_helper(helper: str, journal_text: str, transport: str) -> int:
    """Source the migration script and invoke one of its grep helpers."""
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; "$2" "$3" "$4"', "_", str(SCRIPT), helper, journal_text, transport],
        capture_output=True,
        text=True,
        env={**os.environ, "ANNA_HOME": "/nonexistent-anna-home"},
    )
    return result.returncode


@pytest.mark.parametrize("channel", ["slack", "telegram", "cli"])
def test_helper_matches_rendered_connected_line(channel: str) -> None:
    line = _render(CONNECTED_MARKER, channel, attempt=1)
    assert _bash_helper("journal_has_connected", line, channel) == 0
    # A connected marker for one channel must not satisfy another.
    other = "telegram" if channel != "telegram" else "slack"
    assert _bash_helper("journal_has_connected", line, other) != 0
    # And it must not register as token_missing.
    assert _bash_helper("journal_has_token_missing", line, channel) != 0


@pytest.mark.parametrize(
    "event", [TOKEN_MISSING_LOG_MARKER, TOKEN_MISSING_AUDIT_MARKER]
)
def test_helper_matches_rendered_token_missing_line(event: str) -> None:
    line = _render(event, "slack", missing=["SLACK_APP_TOKEN"])
    assert _bash_helper("journal_has_token_missing", line, "slack") == 0
    assert _bash_helper("journal_has_token_missing", line, "telegram") != 0
    assert _bash_helper("journal_has_connected", line, "slack") != 0


def test_helper_tolerates_compact_json() -> None:
    """A future renderer that drops the space after ':' must still match."""
    line = json.dumps(
        {"channel": "cli", "event": CONNECTED_MARKER}, separators=(",", ":")
    )
    assert _bash_helper("journal_has_connected", line, "cli") == 0


# ---------------------------------------------------------------------------
# Bash smoke: enabled-transport parsing from anna.yaml
# ---------------------------------------------------------------------------


def _enabled_transports(tmp_path: Path, yaml_text: str | None) -> str:
    if yaml_text is not None:
        (tmp_path / "anna.yaml").write_text(yaml_text, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; enabled_transports', "_", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={**os.environ, "ANNA_HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_enabled_transports_reads_yaml(tmp_path: Path) -> None:
    yaml_text = (
        "runtime:\n"
        "  model: whatever\n"
        "transports:\n"
        "  slack:\n"
        "    enabled: true\n"
        "  telegram:\n"
        "    enabled: false\n"
        "  # cli: commented example must be ignored\n"
        "  #   enabled: false\n"
        "vault:\n"
        "  path: ~/x\n"
    )
    # cli defaults to enabled when absent, mirroring CLITransportConfig.
    assert _enabled_transports(tmp_path, yaml_text) == "slack cli"


def test_enabled_transports_all_disabled(tmp_path: Path) -> None:
    yaml_text = (
        "transports:\n"
        "  slack:\n"
        "    enabled: false\n"
        "  telegram:\n"
        "    enabled: false\n"
        "  cli:\n"
        "    enabled: false\n"
    )
    assert _enabled_transports(tmp_path, yaml_text) == ""


def test_enabled_transports_missing_yaml_defaults_to_cli(tmp_path: Path) -> None:
    assert _enabled_transports(tmp_path, None) == "cli"
