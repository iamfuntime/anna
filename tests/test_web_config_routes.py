"""Tests for ``anna_web.routes.config_routes`` (subtask 7).

Six required cases from the buildout plan:

1. ``GET /config`` returns 200 and lists every top-level section.
2. ``GET /config/web`` returns 200 and emits the dotted-path input
   names the form parser later expects.
3. ``GET /config/nonsense`` returns 404.
4. ``POST /config/web`` with a valid payload writes the YAML file
   (round-tripped through ``ConfigStore``) and renders the success
   toast.
5. ``POST /config/web`` with an out-of-range port returns 422,
   renders the field name in an error context, and leaves the file
   byte-identical.
6. ``POST /config/nonsense`` returns 404.

Fixture strategy mirrors :mod:`tests.test_web_config_store`: copy the
canonical ``anna.yaml.example`` into ``tmp_path``, construct a fresh
:class:`anna_web.app.create_app`-built app pointed at that tmp home,
and exercise it through :class:`fastapi.testclient.TestClient`. Each
test gets its own isolated home so writes from one don't bleed into
the next.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.app import create_app
from anna_web.config_store import ConfigStore

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` with a fresh copy of anna.yaml.example."""
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


@pytest.fixture
def client(anna_home: Path) -> TestClient:
    """TestClient over an app whose ConfigStore points at ``anna_home``.

    We build a fresh ``AnnaConfig`` (defaults are fine — only
    anna_home matters for ConfigStore wiring), force-override the
    derived ``anna_home`` to the tmp path, and rebuild the app. The
    module-level ``anna_web.app:app`` is untouched.
    """
    cfg = AnnaConfig()
    # AnnaConfig is frozen against attribute assignment in some
    # configurations; use object.__setattr__ to match the existing
    # test pattern in tests/test_web_server.py.
    object.__setattr__(cfg, "anna_home", anna_home)
    app = create_app(cfg)
    # Defensive: rebind the config_store to one pointed at our tmp
    # path in case any future refactor of create_app changes how
    # anna_home flows through. Today both paths are equivalent.
    app.state.config_store = ConfigStore(anna_home=anna_home)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. GET /config — section index.
# ---------------------------------------------------------------------------


def test_get_config_lists_all_sections(client: TestClient) -> None:
    """All 15 top-level AnnaConfig sections appear as links in the index."""
    response = client.get("/config")
    assert response.status_code == 200
    body = response.text

    # Every non-anna_home top-level field on AnnaConfig should appear.
    expected = [
        "auth",
        "runtime",
        "transports",
        "vault",
        "watchdog",
        "logging",
        "housekeeping",
        "sessions",
        "admin",
        "scheduler",
        "google",
        "tools",
        "subagents",
        "web",
        "identities",
    ]
    for section in expected:
        assert f'href="/config/{section}"' in body, (
            f"section {section!r} missing from /config index"
        )


# ---------------------------------------------------------------------------
# 1b. GET /config — cards preview section contents (subtask 6).
# ---------------------------------------------------------------------------


def test_config_index_previews_field_labels(client: TestClient) -> None:
    """Section cards preview child field labels instead of an "N fields" count.

    The transports section's children are slack / telegram / cli; their
    humanized labels should surface in the card body so the operator learns
    what's inside without clicking through.
    """
    response = client.get("/config")
    assert response.status_code == 200
    body = response.text

    # Field-label preview is present...
    assert "Slack" in body
    assert "Telegram" in body
    # ...and the old bare-count abstraction for transports is gone.
    assert "3 fields" not in body
    assert "fields</small>" not in body


def test_config_index_identities_shows_count_and_edit(client: TestClient) -> None:
    """The REPEATED_FIELDSET section reports an item count plus an edit link.

    The identities card carries two links to /config/identities — the title
    and a dedicated edit affordance — and an "item(s)" count in its body.
    """
    response = client.get("/config")
    assert response.status_code == 200
    body = response.text

    assert "item" in body
    # Title link + edit-affordance link both point at the section editor.
    assert body.count('href="/config/identities"') >= 2


# ---------------------------------------------------------------------------
# 2. GET /config/web — form for one section.
# ---------------------------------------------------------------------------


def test_get_config_web_renders_form(client: TestClient) -> None:
    """The web section editor emits the four dotted-path input names."""
    response = client.get("/config/web")
    assert response.status_code == 200
    body = response.text

    # The dotted-path naming convention is load-bearing for the POST
    # handler: it strips the ``web.`` prefix when rebuilding the
    # payload. If a future refactor regresses this the POST parser
    # silently produces an empty payload.
    assert 'name="web.enabled"' in body
    assert 'name="web.host"' in body
    assert 'name="web.port"' in body
    assert 'name="web.target_unit"' in body
    # And the form posts back to itself with HTMX wiring.
    assert 'hx-post="/config/web"' in body


# ---------------------------------------------------------------------------
# 3. GET /config/nonsense — 404.
# ---------------------------------------------------------------------------


def test_get_config_unknown_section_404(client: TestClient) -> None:
    response = client.get("/config/nonsense")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 4. POST /config/web — valid payload writes file + success toast.
# ---------------------------------------------------------------------------


def test_post_config_web_writes_file(client: TestClient, anna_home: Path) -> None:
    """Valid POST: 200, success toast rendered, on-disk port reflects edit."""
    yaml_path = anna_home / "anna.yaml"
    before = yaml_path.read_text(encoding="utf-8")
    assert "port: 8765" in before, "fixture sanity check"

    response = client.post(
        "/config/web",
        data={
            "web.enabled": "on",
            "web.host": "127.0.0.1",
            "web.port": "9000",
            "web.target_unit": "anna.service",
        },
    )

    assert response.status_code == 200, response.text
    body = response.text
    # Success-toast copy from the plan / route.
    assert "Saved web" in body
    assert "toast-success" in body

    # File reflects the edit.
    after = yaml_path.read_text(encoding="utf-8")
    assert "port: 9000" in after
    assert "port: 8765" not in after


# ---------------------------------------------------------------------------
# 5. POST /config/web — invalid payload: 422 + error context + file unchanged.
# ---------------------------------------------------------------------------


def test_post_config_web_invalid_port_returns_422(
    client: TestClient, anna_home: Path
) -> None:
    """web.port = 70000 trips the 1..65535 validator: 422, file untouched."""
    yaml_path = anna_home / "anna.yaml"
    before_bytes = yaml_path.read_bytes()

    response = client.post(
        "/config/web",
        data={
            "web.enabled": "on",
            "web.host": "127.0.0.1",
            "web.port": "70000",
            "web.target_unit": "anna.service",
        },
    )

    assert response.status_code == 422, response.text
    body = response.text
    # The error toast surfaces and the per-field error slot is keyed
    # by the dotted path. The slot's data-field carries the path, and
    # for the failing field the inner text is non-empty.
    assert "toast-error" in body
    # The per-field error slot for web.port should contain something.
    # We look for the data-field attribute and then assert that the
    # template's template-condition fired.
    assert 'data-field="web.port"' in body
    # Pydantic's message for the validator includes "65535" — pin on it.
    assert "65535" in body

    # File untouched.
    assert yaml_path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# 6. POST /config/nonsense — 404.
# ---------------------------------------------------------------------------


def test_post_config_unknown_section_404(client: TestClient) -> None:
    response = client.post("/config/nonsense", data={})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Bonus: checkbox-on-the-wire round-trip.
# ---------------------------------------------------------------------------


def test_post_config_web_unchecked_checkbox_writes_false(
    client: TestClient, anna_home: Path
) -> None:
    """Unchecked checkboxes don't post; payload should land as False.

    This pins the checkbox-omission contract: the POST handler must
    fill in False for any CHECKBOX FormField whose path is absent
    from the form body. Otherwise the operator can never flip a
    default-true field to false via the dashboard.
    """
    yaml_path = anna_home / "anna.yaml"

    response = client.post(
        "/config/web",
        data={
            # web.enabled deliberately omitted (unchecked).
            "web.host": "127.0.0.1",
            "web.port": "9000",
            "web.target_unit": "anna.service",
        },
    )

    assert response.status_code == 200, response.text
    after = yaml_path.read_text(encoding="utf-8")
    # ruamel writes booleans as ``true``/``false`` (lowercase). The
    # web block should now show enabled: false.
    assert "enabled: false" in after


# ---------------------------------------------------------------------------
# Canary: config_write audit payload never contains the submitted value.
# ---------------------------------------------------------------------------


def test_config_write_audit_payload_never_contains_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section name is allowed in the audit row; submitted values are not.

    Captures every audit_event call into a list, posts a canary value
    against a config field, and asserts the canary string never appears
    in any captured payload. The route is allowed to log ``section=web``
    but must not pass through ``web.host`` or any other field value.
    """
    from anna_web import audit as web_audit

    canary = "10.0.0.1-canary-do-not-leak"
    captured: list[dict] = []

    def _capture(name: str, **kwargs: object) -> None:
        captured.append({"name": name, **kwargs})

    monkeypatch.setattr(web_audit, "audit_event", _capture)

    response = client.post(
        "/config/web",
        data={
            "web.enabled": "on",
            "web.host": canary,
            "web.port": "9000",
            "web.target_unit": "anna.service",
        },
    )
    # Pydantic's IPvAnyAddress validation isn't enforced on web.host
    # (it's a plain str), so this should land as a successful write.
    # If a future schema tightens the type, the audit emit still fires
    # from the validate-failure branch — either way, no canary in the
    # captured payload.
    assert response.status_code in (200, 422), response.text

    config_events = [e for e in captured if "config" in e["name"]]
    assert config_events, "no config-related audit events were emitted"
    # Section name should appear (intentional — operator-readable).
    assert any(e.get("section") == "web" for e in config_events)

    # Load-bearing assertion: walk every captured event and verify the
    # canary string is absent everywhere.
    def _has_canary(obj: object) -> bool:
        if isinstance(obj, str):
            return canary in obj
        if isinstance(obj, dict):
            return any(_has_canary(v) for v in obj.values()) or any(
                _has_canary(k) for k in obj.keys()
            )
        if isinstance(obj, (list, tuple, set)):
            return any(_has_canary(v) for v in obj)
        return canary in repr(obj)

    for event in captured:
        assert not _has_canary(event), (
            f"config write value leaked into audit payload: {event!r}"
        )
