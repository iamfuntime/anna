"""Tests for ``anna_web.audit`` (subtask 12).

Six cases pin the wrapper's contract:

1. ``emit("foo")`` prefixes the event name to
   ``audit.web.dashboard.foo`` before calling
   :func:`anna.log.audit_event`.
2. ``emit("foo", actor="bob")`` forwards a non-default actor.
3. ``emit("foo", request=...)`` reads ``request.state.request_id``
   and stamps it onto the payload.
4. ``emit("foo", value="hi")`` strips the value field AND logs a
   warning so the regression surfaces in the operational stream.
5. ``emit("foo", secret_value="...", token="...")`` strips every
   reserved-name field independently.
6. :data:`anna_web.audit.RESERVED_VALUE_FIELDS` contains the
   documented set ``{value, secret_value, password, token}`` so a
   future refactor that drops one of them turns this test red.

Each test stubs :func:`anna.log.audit_event` via ``monkeypatch`` so
no real audit JSONL row lands on disk during the suite. The
``anna_web.audit`` module imports the underlying writer as
``audit_event``, so patching the symbol on the module object is what
the wrapper actually calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from anna.config import AnnaConfig
from anna_web import audit as web_audit


@pytest.fixture
def fake_audit_event(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture every audit_event call into a list of recorded dicts."""
    captured: list[dict] = []

    def _capture(name: str, **kwargs: Any) -> None:
        captured.append({"name": name, **kwargs})

    monkeypatch.setattr(web_audit, "audit_event", _capture)
    return captured


@pytest.fixture
def cfg(tmp_path: Path) -> AnnaConfig:
    """A real AnnaConfig with anna_home overridden to a tmp directory."""
    config = AnnaConfig()
    home = tmp_path / "anna_home"
    home.mkdir()
    (home / "audit").mkdir()
    object.__setattr__(config, "anna_home", home)
    return config


def _make_request_stub(*, request_id: str | None = None, cfg: AnnaConfig | None = None) -> MagicMock:
    """Build a stand-in Request with ``state``/``app.state`` shaped right.

    The audit wrapper only reads ``request.state.request_id`` and
    ``request.app.state.cfg`` — a MagicMock with those two attributes
    populated is enough surface for the unit tests.
    """
    request = MagicMock()
    request.state = MagicMock()
    if request_id is None:
        # Mirror the no-id branch: request.state has no request_id at all.
        # MagicMock auto-creates attribute access so we have to be explicit.
        request.state.request_id = None
    else:
        request.state.request_id = request_id
    request.app.state.cfg = cfg
    return request


# ---------------------------------------------------------------------------
# 1. Event prefix.
# ---------------------------------------------------------------------------


def test_emit_prefixes_event_name(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """The plain ``"foo"`` tag is rewritten as the dotted dashboard name."""
    web_audit.emit("foo", cfg=cfg)
    assert len(fake_audit_event) == 1
    assert fake_audit_event[0]["name"] == "audit.web.dashboard.foo"


# ---------------------------------------------------------------------------
# 2. Actor override.
# ---------------------------------------------------------------------------


def test_emit_forwards_actor(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """Passing a non-default ``actor`` lands in the audit kwargs."""
    web_audit.emit("foo", actor="bob", cfg=cfg)
    assert fake_audit_event[0]["actor"] == "bob"


def test_emit_default_actor_is_operator(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """Default ``actor`` is ``"operator"`` per the v1 dashboard model."""
    web_audit.emit("foo", cfg=cfg)
    assert fake_audit_event[0]["actor"] == "operator"


# ---------------------------------------------------------------------------
# 3. request_id pulled from request.state.
# ---------------------------------------------------------------------------


def test_emit_includes_request_id_from_state(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """The middleware-tagged request_id surfaces in the audit payload."""
    request = _make_request_stub(request_id="abc123", cfg=cfg)
    web_audit.emit("foo", request=request)
    assert fake_audit_event[0]["request_id"] == "abc123"


def test_emit_omits_request_id_when_absent(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """No request_id on state means no request_id key in the payload.

    Important for tests that snapshot the payload — an absent middleware
    tag should not stamp a synthetic placeholder.
    """
    request = _make_request_stub(request_id=None, cfg=cfg)
    web_audit.emit("foo", request=request)
    assert "request_id" not in fake_audit_event[0]


# ---------------------------------------------------------------------------
# 4. Reserved value field stripped, warning logged.
# ---------------------------------------------------------------------------


def test_emit_strips_value_field_with_warning(
    fake_audit_event: list[dict],
    cfg: AnnaConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``value=`` arg is dropped and a warning lands in the log stream.

    structlog's configured handler renders to stdout via the
    operational stream (see :func:`anna.log.configure_logging`), so
    the assertion uses ``capsys`` rather than ``caplog``. ``caplog``
    only catches stdlib logging.LogRecord instances that go through
    pytest's hook; the structlog → stdlib bridge that anna installs
    routes through a custom handler that bypasses caplog.
    """
    web_audit.emit("foo", cfg=cfg, value="should-not-leak")
    assert "value" not in fake_audit_event[0]
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "reserved_field_stripped" in combined or "value" in combined


# ---------------------------------------------------------------------------
# 5. Multiple reserved fields, each stripped.
# ---------------------------------------------------------------------------


def test_emit_strips_every_reserved_field(
    fake_audit_event: list[dict],
    cfg: AnnaConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """secret_value, password, token are all dropped independently."""
    web_audit.emit(
        "foo",
        cfg=cfg,
        secret_value="redact-me-1",
        password="redact-me-2",
        token="redact-me-3",
        # Non-reserved field passes through.
        key="SOME_KEY",
    )
    record = fake_audit_event[0]
    assert "secret_value" not in record
    assert "password" not in record
    assert "token" not in record
    assert record["key"] == "SOME_KEY"
    # One warning per stripped field. structlog renders to stdout via
    # the daemon's configured handler; capsys catches the output.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # Three reserved fields → three warning lines mentioning the
    # field names. Count occurrences of the warning event name.
    assert combined.count("reserved_field_stripped") >= 3


# ---------------------------------------------------------------------------
# 6. RESERVED_VALUE_FIELDS contains the documented set.
# ---------------------------------------------------------------------------


def test_reserved_value_fields_exact_set() -> None:
    """The constant defines the documented set, no more and no less."""
    assert web_audit.RESERVED_VALUE_FIELDS == frozenset(
        {"value", "secret_value", "password", "token"}
    )


# ---------------------------------------------------------------------------
# Bonus: via="web" stamp.
# ---------------------------------------------------------------------------


def test_emit_stamps_via_web(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """Every dashboard audit row carries ``via="web"`` so audit slicing works."""
    web_audit.emit("foo", cfg=cfg)
    assert fake_audit_event[0]["via"] == "web"


def test_emit_uses_cfg_audit_dir(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """Explicit cfg= override flows audit_dir through correctly."""
    web_audit.emit("foo", cfg=cfg)
    assert fake_audit_event[0]["audit_dir"] == cfg.audit_dir


def test_emit_falls_back_to_request_app_cfg(
    fake_audit_event: list[dict], cfg: AnnaConfig
) -> None:
    """When no explicit cfg/audit_dir, the wrapper pulls cfg off request.app.state."""
    request = _make_request_stub(request_id="req-1", cfg=cfg)
    web_audit.emit("foo", request=request)
    assert fake_audit_event[0]["audit_dir"] == cfg.audit_dir


def test_emit_no_anchor_logs_and_returns(
    fake_audit_event: list[dict],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No cfg, no request, no audit_dir → soft fail, no exception."""
    web_audit.emit("foo")
    # No audit_event invocation lands because we cannot resolve a path.
    assert fake_audit_event == []
    # Warning is emitted so the missing dir surfaces in journald.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "no_audit_dir" in combined
