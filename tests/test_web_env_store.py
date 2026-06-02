"""Tests for ``anna_web.env_store.EnvStore`` (subtask 4).

Eight tests per the buildout plan. The two load-bearing claims are:

* the documented-key allow-list rejects typos unless explicitly
  overridden (test 5), and
* file mode is 0o600 after every mutation, even if dotenv loosens
  it (tests 3, 4, 6).

Tests use ``tmp_path`` for the ``.env`` file location so the
operator's real ``~/anna/.env`` is never touched. The
:data:`DOCUMENTED_VARS` parsing test (#7) reads the real
``.env.example`` from the repo because that's the canonical input
and the whole module-level constant is built off it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from anna_web.env_store import DOCUMENTED_VARS, DocumentedVar, EnvStore


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` with no pre-existing ``.env``.

    The store creates the file on first ``set`` call, mirroring how
    a fresh install would arrive at the dashboard. Tests that need
    pre-existing content seed it themselves.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    return home


def _mode(p: Path) -> int:
    """Return the low 12 bits of the file mode (permission bits)."""
    return stat.S_IMODE(p.stat().st_mode)


# ---------------------------------------------------------------------------
# 1. load() round-trip.
# ---------------------------------------------------------------------------


def test_load_returns_all_keys(anna_home: Path) -> None:
    """Set two keys, then load() returns both as a plain dict.

    Uses ``allow_unknown=True`` so the test isn't coupled to which
    keys happen to be in ``.env.example``. The dict equality check
    pins the surface: keys preserved, values preserved, nothing
    extra leaking in.
    """
    store = EnvStore(anna_home=anna_home)
    store.set("FOO", "bar", allow_unknown=True)
    store.set("BAZ", "qux", allow_unknown=True)

    loaded = store.load()

    assert loaded == {"FOO": "bar", "BAZ": "qux"}


def test_load_missing_file_returns_empty_dict(anna_home: Path) -> None:
    """A fresh install has no ``.env`` yet; load() must not raise.

    The dashboard's GET ``/env`` endpoint is the first thing the
    operator hits on a brand-new box. If load() raised on a missing
    file the form would be unrenderable until something else
    bootstrapped the file.
    """
    store = EnvStore(anna_home=anna_home)
    assert store.load() == {}


# ---------------------------------------------------------------------------
# 2. get() returns one value or None.
# ---------------------------------------------------------------------------


def test_get_returns_value_or_none(anna_home: Path) -> None:
    """Set a key, get it back; ask for a missing key, get None.

    Backs the reveal endpoint's "key exists" vs "404" branching.
    """
    store = EnvStore(anna_home=anna_home)
    store.set("FOO", "bar", allow_unknown=True)

    assert store.get("FOO") == "bar"
    assert store.get("NONEXISTENT") is None


def test_get_missing_file_returns_none(anna_home: Path) -> None:
    """No .env file yet → get() returns None instead of raising."""
    store = EnvStore(anna_home=anna_home)
    assert store.get("ANYTHING") is None


# ---------------------------------------------------------------------------
# 3. set() writes + persists + file mode 0o600.
# ---------------------------------------------------------------------------


def test_set_writes_and_mode_is_0600(anna_home: Path) -> None:
    """SLACK_BOT_TOKEN is in DOCUMENTED_VARS; set then re-read.

    The assertion that SLACK_BOT_TOKEN is in the documented list
    pins the test against the canonical .env.example so a future
    refactor that drops the variable from the example surfaces
    here, not as a silent green test.
    """
    assert any(v.name == "SLACK_BOT_TOKEN" for v in DOCUMENTED_VARS), (
        "SLACK_BOT_TOKEN must appear in .env.example for this test to be meaningful"
    )

    store = EnvStore(anna_home=anna_home)
    store.set("SLACK_BOT_TOKEN", "xoxb-test-value")

    # Reload from a fresh store instance to prove persistence to
    # disk rather than to some in-memory cache.
    reloaded = EnvStore(anna_home=anna_home).load()
    assert reloaded["SLACK_BOT_TOKEN"] == "xoxb-test-value"
    assert _mode(store.path) == 0o600


# ---------------------------------------------------------------------------
# 4. set() re-tightens permissions even when file starts world-readable.
# ---------------------------------------------------------------------------


def test_set_retightens_loose_permissions(anna_home: Path) -> None:
    """Pre-create the .env at 0o644, set a key, mode must drop to 0o600.

    This is the load-bearing test for the "python-dotenv sometimes
    loosens permissions" guarantee in the plan. Even if dotenv's
    write opens the file with default umask permissions, our
    re-chmod must restore the tight mode unconditionally.
    """
    env_path = anna_home / ".env"
    env_path.write_text("SLACK_BOT_TOKEN=existing\n", encoding="utf-8")
    os.chmod(env_path, 0o644)
    assert _mode(env_path) == 0o644  # sanity check the setup

    store = EnvStore(anna_home=anna_home)
    store.set("SLACK_BOT_TOKEN", "new-value")

    assert _mode(env_path) == 0o600


# ---------------------------------------------------------------------------
# 5. Unknown-key rejection with allow_unknown escape hatch.
# ---------------------------------------------------------------------------


def test_set_rejects_unknown_key(anna_home: Path) -> None:
    """A key not in DOCUMENTED_VARS raises ValueError by default.

    Uses a deliberately implausible key so this test survives any
    future expansion of .env.example.
    """
    undocumented = "RANDOM_UNDOCUMENTED_KEY_XYZ"
    assert undocumented not in {v.name for v in DOCUMENTED_VARS}

    store = EnvStore(anna_home=anna_home)
    with pytest.raises(ValueError, match="unknown env key"):
        store.set(undocumented, "value")


def test_set_allows_unknown_key_when_opted_in(anna_home: Path) -> None:
    """allow_unknown=True is the escape hatch for the free-form rows UI."""
    undocumented = "RANDOM_UNDOCUMENTED_KEY_XYZ"

    store = EnvStore(anna_home=anna_home)
    store.set(undocumented, "value", allow_unknown=True)

    assert store.get(undocumented) == "value"
    assert _mode(store.path) == 0o600


def test_delete_rejects_unknown_key(anna_home: Path) -> None:
    """delete() applies the same allow-list discipline as set()."""
    undocumented = "RANDOM_UNDOCUMENTED_KEY_XYZ"

    store = EnvStore(anna_home=anna_home)
    with pytest.raises(ValueError, match="unknown env key"):
        store.delete(undocumented)


# ---------------------------------------------------------------------------
# 6. delete() removes the key + mode preserved.
# ---------------------------------------------------------------------------


def test_delete_removes_key_and_preserves_mode(anna_home: Path) -> None:
    """Set then delete a documented key; reload sees nothing, mode 0o600.

    Exercises the full lifecycle the operator goes through when
    rotating a leaked credential: write the new one (or in this
    case set + delete), confirm it's gone, confirm the file is
    still tight.
    """
    store = EnvStore(anna_home=anna_home)
    store.set("SLACK_BOT_TOKEN", "value")

    store.delete("SLACK_BOT_TOKEN")

    reloaded = store.load()
    assert "SLACK_BOT_TOKEN" not in reloaded
    assert _mode(store.path) == 0o600


# ---------------------------------------------------------------------------
# 7. DOCUMENTED_VARS parses .env.example correctly.
# ---------------------------------------------------------------------------


def test_documented_vars_parsed_from_example() -> None:
    """The constant is non-empty, includes SLACK_BOT_TOKEN as a secret,
    and labels are title-cased with spaces.

    Spot-checks the parsing rules rather than enumerating every
    variable: enumeration would couple the test to .env.example
    line-for-line and break on every benign edit. The structural
    invariants (it parsed something, secret detection works, label
    formatting works) are what we care about.
    """
    assert DOCUMENTED_VARS, "DOCUMENTED_VARS must be non-empty when .env.example exists"

    by_name = {v.name: v for v in DOCUMENTED_VARS}
    assert "SLACK_BOT_TOKEN" in by_name

    slack = by_name["SLACK_BOT_TOKEN"]
    assert isinstance(slack, DocumentedVar)
    assert slack.kind == "secret"
    assert slack.label == "Slack Bot Token"

    # ANNA_LOG_LEVEL is a plain-text setting (no secret tokens in
    # the name) — pins the kind-detection branch the other way.
    assert "ANNA_LOG_LEVEL" in by_name
    assert by_name["ANNA_LOG_LEVEL"].kind == "text"
    assert by_name["ANNA_LOG_LEVEL"].label == "Anna Log Level"


# ---------------------------------------------------------------------------
# 8. Audit-payload placeholder for subtask 12.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="audit wiring lands in subtask 12")
def test_set_audit_payload_never_contains_value() -> None:
    """Placeholder for the cross-cutting secret-redaction obligation.

    Subtask 12 wires audit_event into EnvStore.set/delete. When
    that lands, this test must assert that the emitted audit record
    contains the key name and actor but never the value. Leaving
    the test stub here keeps the obligation visible in the suite's
    skip count instead of in a TODO that nobody greps for.
    """
