"""Tests for the ``anna.__main__`` subcommand dispatcher.

Phase 2 §5 subtask 11. The dispatcher routes ``python -m anna ...`` /
``anna ...`` invocations to either the daemon entrypoint or one of the
CLI client modules (``chat`` / ``ask`` / ``admin``). These tests cover
the routing arms; the daemon and CLI client modules themselves are
exercised elsewhere.

Each test stubs out the dispatched target so the test does not actually
launch the daemon or open a Unix socket — what we are verifying is the
dispatch *decision*, the lazy-import behavior, and the ``sys.argv``
rewriting for downstream argv-parsing.
"""

from __future__ import annotations

import sys
import types

import pytest

import anna.__main__ as anna_main

# ---------------------------------------------------------------------------
# Helper: a stub CLI subcommand module that records its invocation.
# ---------------------------------------------------------------------------


class _RecordingStub:
    """Minimal stand-in for a CLI subcommand module.

    ``main()`` records the ``sys.argv`` shape the dispatcher passed in
    and returns the configured exit code. Acts as a module surrogate
    for ``anna.cli.chat`` / ``anna.cli.ask`` / ``anna.cli.admin`` so
    the tests don't need the heavyweight prompt_toolkit / asyncio
    machinery loaded.
    """

    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def main(self) -> int:
        self.calls.append(list(sys.argv))
        return self.exit_code


# ---------------------------------------------------------------------------
# 1. No subcommand → daemon
# ---------------------------------------------------------------------------


def test_main_no_subcommand_runs_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``anna`` with no args dispatches to ``run_daemon``.

    The systemd unit invokes the ``anna`` console-script with no
    arguments, so this path is load-bearing — argparse's default
    "print help and exit" must not fire here.
    """
    monkeypatch.setattr(sys, "argv", ["anna"])
    called: list[bool] = []

    def fake_run_daemon() -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(anna_main, "run_daemon", fake_run_daemon)
    rc = anna_main.main()
    assert rc == 0
    assert called == [True]


# ---------------------------------------------------------------------------
# 2. Explicit ``daemon`` subcommand → daemon
# ---------------------------------------------------------------------------


def test_main_daemon_subcommand_runs_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``anna daemon`` dispatches to ``run_daemon`` just like no-subcommand."""
    monkeypatch.setattr(sys, "argv", ["anna", "daemon"])
    called: list[bool] = []

    def fake_run_daemon() -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(anna_main, "run_daemon", fake_run_daemon)
    rc = anna_main.main()
    assert rc == 0
    assert called == [True]


# ---------------------------------------------------------------------------
# 3. ``chat`` subcommand → anna.cli.chat.main
# ---------------------------------------------------------------------------


def test_main_chat_subcommand_dispatches_to_cli_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``anna chat`` imports ``anna.cli.chat`` and calls its ``main()``.

    The stub module records ``sys.argv`` at call time to verify the
    dispatcher rewrote it to ``["anna chat", *rest]`` so click/argparse
    inside the real module would see the right shape.
    """
    monkeypatch.setattr(sys, "argv", ["anna", "chat"])
    stub = _RecordingStub(exit_code=0)
    fake_module = types.SimpleNamespace(main=stub.main)
    monkeypatch.setitem(sys.modules, "anna.cli.chat", fake_module)

    # Also guard against ``run_daemon`` accidentally being invoked.
    monkeypatch.setattr(
        anna_main,
        "run_daemon",
        lambda: pytest.fail("run_daemon must not be called for `anna chat`"),
    )

    rc = anna_main.main()
    assert rc == 0
    assert len(stub.calls) == 1
    # sys.argv was rewritten to ``["anna chat", *rest]``; ``rest`` is empty here.
    assert stub.calls[0] == ["anna chat"]


# ---------------------------------------------------------------------------
# 4. ``ask`` subcommand passes extra args through via sys.argv
# ---------------------------------------------------------------------------


def test_main_ask_subcommand_forwards_extra_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``anna ask what time is it`` forwards the remainder to ``ask.main``.

    The dispatcher rewrites ``sys.argv`` so the inner ``ask.main()``
    sees ``["anna ask", "what", "time", "is", "it"]`` and can parse the
    prompt from ``sys.argv[1:]`` exactly as it does when invoked as a
    plain console script.
    """
    monkeypatch.setattr(sys, "argv", ["anna", "ask", "what", "time", "is", "it"])
    stub = _RecordingStub(exit_code=0)
    fake_module = types.SimpleNamespace(main=stub.main)
    monkeypatch.setitem(sys.modules, "anna.cli.ask", fake_module)

    rc = anna_main.main()
    assert rc == 0
    assert len(stub.calls) == 1
    assert stub.calls[0] == ["anna ask", "what", "time", "is", "it"]


# ---------------------------------------------------------------------------
# 5. ``admin`` subcommand: not-importable → stub message + exit 2
# ---------------------------------------------------------------------------


def test_main_admin_subcommand_missing_module_stub_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If ``anna.cli.admin`` cannot be imported, dispatch exits 2 with a stub line.

    Simulates the pre-subtask-12 state where the module either does not
    exist yet or has been intentionally broken. The dispatcher must not
    crash and must not leak the import traceback to the operator.
    """
    monkeypatch.setattr(sys, "argv", ["anna", "admin"])

    # ``importlib.import_module`` with ``sys.modules[name] = None``
    # raises ImportError immediately. This simulates the not-yet-shipped
    # state without needing to touch the real ``anna.cli.admin`` file.
    monkeypatch.setitem(sys.modules, "anna.cli.admin", None)

    rc = anna_main.main()
    assert rc == 2

    captured = capsys.readouterr()
    assert "anna admin subcommands not yet available" in captured.err
    assert "subtask 12" in captured.err


# ---------------------------------------------------------------------------
# 6. Unknown subcommand → usage to stderr + exit 2
# ---------------------------------------------------------------------------


def test_main_unknown_subcommand_exits_two_with_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unrecognized first positional argument prints usage and exits 2.

    argparse itself rejects unknown subcommands inside ``parse_args()``
    and surfaces a usage banner on stderr; we just verify the rc and
    that *something* usage-shaped landed on stderr.
    """
    monkeypatch.setattr(sys, "argv", ["anna", "bogus"])

    # If the dispatcher accidentally fell through to daemon, the test
    # would hang trying to load config. Stub it just in case.
    monkeypatch.setattr(
        anna_main,
        "run_daemon",
        lambda: pytest.fail("run_daemon must not be called for `anna bogus`"),
    )

    rc = anna_main.main()
    assert rc == 2

    captured = capsys.readouterr()
    # argparse prints a usage line and an error line, both to stderr.
    assert "usage" in captured.err.lower() or "anna" in captured.err
