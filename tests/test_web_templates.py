"""Tests for the ANNA web dashboard base template + static assets (subtask 6).

The plan calls for five cases that pin the shape the rest of pass 2/3
will build on:

1. Index renders via Jinja2 (200, HTML, brand + nav targets present).
2. CSS reachable (anna.css — the hand-rolled design system, MC-01).
3. Vendored JS reachable (htmx already covered in test_web_scaffold,
   add an explicit /static/app.js check).
4. The ``_toast.html`` partial renders the level class + message.
5. ``base.html`` block overrides (title + content) actually land.

Direct Jinja2 rendering is preferred for the partial / block-override
cases because constructing a ``Request`` for ``TemplateResponse`` is
ceremony that buys nothing — the load-bearing claim is "templates on
disk render correctly", not "FastAPI wires them up" (which test 1
covers).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "anna_web" / "templates"


def _env() -> Environment:
    """Bare Jinja2 environment over the templates dir. No autoescape
    fuss for these assertions — we're checking string contents, not
    rendering user input."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
    )


def test_index_renders_via_jinja() -> None:
    """GET / returns 200 HTML containing the brand and the three nav
    targets the subtask plan calls out."""
    from anna_web.app import app

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    content_type = response.headers.get("content-type", "")
    assert "html" in content_type.lower()

    body = response.text
    # Brand appears in both the <title> and the nav.
    assert "ANNA Dashboard" in body
    # Nav links to the three sections subtasks 7/8/9 will land.
    assert 'href="/config"' in body
    assert 'href="/env"' in body
    assert 'href="/schedules"' in body


def test_static_css_reachable() -> None:
    """anna.css (the linked design system, MC-01) resolves via the
    /static mount. pico.min.css / app.css remain on disk but unlinked
    until the deploy subtask removes them, so they aren't asserted."""
    from anna_web.app import app

    client = TestClient(app)

    anna_css = client.get("/static/anna.css")
    assert anna_css.status_code == 200
    assert "css" in anna_css.headers.get("content-type", "").lower()
    # Spot-check load-bearing contents — not just a 200 from a
    # misconfigured mount: dark-default tokens, the light override
    # block, the status palette, and the toast region app.js/HTMX rely on.
    assert '[data-theme="light"]' in anna_css.text
    assert "--status-ok" in anna_css.text
    assert ".toast" in anna_css.text


def test_static_js_reachable() -> None:
    """app.js (the reveal-toggle handler) is served alongside htmx."""
    from anna_web.app import app

    client = TestClient(app)

    response = client.get("/static/app.js")
    assert response.status_code == 200
    content_type = response.headers.get("content-type", "").lower()
    assert "javascript" in content_type
    # Spot-check: the reveal-toggle handler is the file's whole reason
    # for existing.
    assert "reveal-toggle" in response.text


def test_toast_partial_renders_level_and_message() -> None:
    """``_toast.html`` rendered with level=success + message=hello yields
    the level-scoped class plus the message text."""
    env = _env()
    template = env.get_template("_toast.html")

    rendered = template.render(level="success", message="hello")

    assert "toast-success" in rendered
    assert "hello" in rendered
    assert 'role="status"' in rendered


def test_base_block_overrides_land(tmp_path: Path) -> None:
    """A child template extending base.html can override the title and
    content blocks, and both land in the output."""
    # Drop a one-off child template into a tmp dir, then load via an
    # environment that searches both the real templates dir (for
    # ``base.html``) and the tmp dir (for the child).
    child = tmp_path / "child.html"
    child.write_text(
        '{% extends "base.html" %}\n'
        "{% block title %}Custom Title{% endblock %}\n"
        '{% block content %}<p id="custom">custom body copy</p>{% endblock %}\n'
    )

    env = Environment(
        loader=FileSystemLoader([str(tmp_path), str(_TEMPLATES_DIR)]),
        autoescape=True,
    )
    rendered = env.get_template("child.html").render()

    assert "<title>Custom Title</title>" in rendered
    assert '<p id="custom">custom body copy</p>' in rendered
    # Base scaffolding still present — confirms the child extends
    # rather than shadows.
    assert "/static/anna.css" in rendered
    assert "/static/htmx.min.js" in rendered


# ---------------------------------------------------------------------------
# Home page rebuild (subtask 8): operator overview surfaces the restart
# control + live status derived from /healthz, plus the three editor links.
# ---------------------------------------------------------------------------


@pytest.fixture
def home_client() -> Iterator[TestClient]:
    """TestClient over the module-level app with a swappable restart_manager.

    Mirrors the fixture in test_web_restart.py: individual tests overwrite
    ``app.state.restart_manager`` with a per-test mock so the server-rendered
    status is deterministic, then the original is restored.
    """
    from anna_web.app import app

    original = getattr(app.state, "restart_manager", None)
    try:
        yield TestClient(app)
    finally:
        if original is not None:
            app.state.restart_manager = original


def _mock_manager(active_state: str) -> MagicMock:
    mgr = MagicMock()
    mgr.target_unit = "anna.service"
    mgr.also_health_probe = AsyncMock(
        return_value={"active_state": active_state, "method": "dbus"}
    )
    return mgr


def test_home_surfaces_restart_control_and_editor_links(
    home_client: TestClient,
) -> None:
    """GET / includes the (previously orphaned) restart control + quick links."""
    from anna_web.app import app

    app.state.restart_manager = _mock_manager("active")

    response = home_client.get("/")
    assert response.status_code == 200
    body = response.text

    # The restart control is now reachable from Home.
    assert 'hx-post="/restart"' in body
    assert "Restart anna.service" in body

    # Quick links to the three editors.
    assert 'href="/config"' in body
    assert 'href="/env"' in body
    assert 'href="/schedules"' in body

    # The dead "later subtasks" placeholder is gone.
    assert "land in later subtasks" not in body


def test_home_status_reflects_running_daemon(home_client: TestClient) -> None:
    """An ``active`` probe server-renders the anna badge as ``running`` / up."""
    from anna_web.app import app

    app.state.restart_manager = _mock_manager("active")

    response = home_client.get("/")
    assert response.status_code == 200
    body = response.text

    assert 'id="anna-status"' in body
    # Daemon up + config loaded both render as "up" badges.
    assert "running" in body
    assert 'data-state="up"' in body
    # Config flag is derived from app.state.cfg being a real AnnaConfig.
    assert 'id="config-status"' in body
    assert "loaded" in body


def test_home_status_reflects_dead_daemon(home_client: TestClient) -> None:
    """An ``inactive`` probe server-renders the anna badge as ``stopped`` / down."""
    from anna_web.app import app

    app.state.restart_manager = _mock_manager("inactive")

    response = home_client.get("/")
    assert response.status_code == 200
    body = response.text

    # anna badge flips to stopped/down; config stays loaded/up (independent).
    assert "stopped" in body
    assert 'data-state="down"' in body
