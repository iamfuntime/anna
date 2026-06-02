"""Tests for the ANNA web dashboard scaffold (subtask 2).

The scaffold exposes a FastAPI app with a placeholder index and a
mounted static directory plus a CLI entry point that short-circuits
when ``web.enabled`` is False. Real routes (config / env / schedules
/ restart / healthz) land in later subtasks; these tests cover only
the wiring shape the scaffold ships.
"""

from __future__ import annotations

import io
import sys

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig


def test_app_importable() -> None:
    """The module-level ``app`` is importable as ``anna_web.app:app``."""
    from anna_web.app import app

    assert app is not None
    assert app.title == "ANNA Dashboard"


def test_app_root_serves_placeholder() -> None:
    """GET / returns the Jinja2-rendered index page.

    Subtask 6 swapped the inline placeholder string for an
    ``index.html`` template that extends ``base.html``. The body now
    advertises that the dashboard is live and points at the (still
    unrouted) nav targets — assert on the new copy so the test
    actually pins what ships.
    """
    from anna_web.app import app

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "ANNA Dashboard" in response.text
    assert "ANNA Dashboard is live" in response.text


def test_static_mount() -> None:
    """The vendored htmx.min.js is reachable at /static/htmx.min.js."""
    from anna_web.app import app

    client = TestClient(app)
    response = client.get("/static/htmx.min.js")

    assert response.status_code == 200
    # Starlette's StaticFiles maps .js to application/javascript by
    # default (or text/javascript on some platforms via mimetypes).
    content_type = response.headers.get("content-type", "")
    assert "javascript" in content_type.lower()
    # Spot-check that the vendored payload is the real htmx bundle and
    # not, say, a 404 HTML page that 200'd through a misconfigured mount.
    assert "htmx" in response.text.lower()


def test_disabled_mode_short_circuits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When cfg.web.enabled is False, main() logs the disabled line and exits 0
    without importing uvicorn or binding a port."""
    from anna_web import __main__ as anna_web_main

    cfg = AnnaConfig()
    cfg.web.enabled = False

    monkeypatch.setattr(anna_web_main, "load_config", lambda: cfg)
    # configure_logging mutates global structlog state in a way that
    # later in-process tests trip over (PrintLogger has no .name once
    # the stdlib LoggerFactory replaces it). Same pattern as
    # tests/test_admin_merge_checkpoints.py — no-op it for the
    # disabled-mode arm so the suite's ordering stays clean.
    monkeypatch.setattr(anna_web_main, "configure_logging", lambda **_: None)

    # Guard against an accidental uvicorn import path in the disabled
    # branch: stub the module so any import would surface as a clean
    # AssertionError rather than actually starting a server.
    fake_uvicorn = type(sys)("uvicorn")

    def _fail_run(*args: object, **kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("uvicorn.run should not be called in disabled mode")

    fake_uvicorn.run = _fail_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    # Capture the structlog JSON output to verify the disabled event
    # makes it into the operational stream.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    result = anna_web_main.main()

    assert result == 0
    out = buf.getvalue()
    assert "anna.web.dashboard.disabled" in out
