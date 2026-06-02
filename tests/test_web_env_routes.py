"""Tests for the ``/env`` route surface (subtask 8).

Nine cases pin the contract subtask 8 ships:

1. GET /env renders the masked list (documented names appear; secrets
   blanked; documented text vars surface their actual values).
2. GET /env/{key}/reveal returns the raw value as plain text.
3. GET /env/{key}/reveal 404s on an unknown/unset key.
4. POST /env with a documented key writes the value and re-tightens
   file mode to 0o600.
5. POST /env with an unknown key + no is_extra returns 422.
6. POST /env with an unknown key + is_extra=true succeeds.
7. DELETE /env/{key} removes the key and returns 204.
8. (Skip) audit-payload obligation test stub for subtask 12.
9. CRITICAL: the secret string never appears in the GET /env body —
   the load-bearing assertion that masking is real, not visual.

Fixtures use ``tmp_path`` + an explicit override of
``app.state.env_store`` so the operator's real ``~/anna/.env`` is
never touched. The test client is re-built per test because FastAPI's
``TestClient`` is cheap and the state-mutation pattern would otherwise
require careful undo logic across tests.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from anna_web.app import app
from anna_web.env_store import EnvStore


@pytest.fixture
def env_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` directory.

    Created empty; the EnvStore will materialize the ``.env`` file on
    first ``set`` call, mirroring how a fresh install arrives at the
    dashboard.
    """
    home = tmp_path / "anna_home"
    home.mkdir()
    return home


@pytest.fixture
def client(env_home: Path) -> Iterator[TestClient]:
    """Build a TestClient with the EnvStore pointed at a tmp .env.

    Mutates ``app.state.env_store`` for the duration of the test, then
    restores whatever was there before so the module-level app survives
    multiple test runs without leaking state across modules.
    """
    real_store = EnvStore(anna_home=env_home)
    previous = getattr(app.state, "env_store", None)
    app.state.env_store = real_store
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # Restore prior store if present; otherwise leave the attribute
        # absent. delattr is safe-guarded because Starlette's State
        # supports attribute deletion.
        if previous is not None:
            app.state.env_store = previous
        else:
            try:
                delattr(app.state, "env_store")
            except AttributeError:
                pass


def _mode(p: Path) -> int:
    """Return the low 12 bits of the file mode (permission bits)."""
    return stat.S_IMODE(p.stat().st_mode)


# ---------------------------------------------------------------------------
# 1. GET /env renders the masked list.
# ---------------------------------------------------------------------------


def test_get_env_renders_masked_list(client: TestClient, env_home: Path) -> None:
    """A known secret renders blanked; a known text var renders visible.

    Pre-seeds one of each via direct EnvStore writes so the assertions
    don't depend on dotenv's blank-default behavior.
    """
    store = app.state.env_store
    store.set("SLACK_BOT_TOKEN", "xoxb-test-secret-value")
    store.set("ANNA_LOG_LEVEL", "DEBUG")

    response = client.get("/env")

    assert response.status_code == 200
    body = response.text

    # Documented variable names show up in the rendered form.
    assert "SLACK_BOT_TOKEN" in body
    assert "ANTHROPIC_API_KEY" in body  # documented even though unset

    # Documented secret: input has value="" + data-has-value="true".
    # The id selector pins the exact element so a later cosmetic edit
    # that swaps the attribute order doesn't accidentally green this.
    assert 'data-env-input="SLACK_BOT_TOKEN"' in body
    assert 'data-has-value="true"' in body
    # Critical negative: the secret value never appears in HTML.
    assert "xoxb-test-secret-value" not in body

    # Documented text var: actual value renders visibly. ANNA_LOG_LEVEL
    # is detected as kind="text" by env_store._kind_from_name.
    assert "DEBUG" in body


# ---------------------------------------------------------------------------
# 2. GET /env/{key}/reveal returns the actual value.
# ---------------------------------------------------------------------------


def test_reveal_returns_raw_value(client: TestClient) -> None:
    """The reveal endpoint returns the value as text/plain."""
    store = app.state.env_store
    store.set("SLACK_BOT_TOKEN", "xoxb-test")

    response = client.get("/env/SLACK_BOT_TOKEN/reveal")

    assert response.status_code == 200
    assert response.text == "xoxb-test"
    # Plain-text response — the static/app.js handler does fetch().text()
    # so the content type matters for the browser to not try to parse
    # the body as HTML.
    assert response.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# 3. GET /env/{key}/reveal 404 on an unset/unknown key.
# ---------------------------------------------------------------------------


def test_reveal_404_on_unknown_key(client: TestClient) -> None:
    """Reveal on a key that isn't set returns 404."""
    response = client.get("/env/COMPLETELY_MADE_UP_KEY_XYZ/reveal")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 4. POST /env with a documented key writes the value.
# ---------------------------------------------------------------------------


def test_post_documented_key_writes(client: TestClient) -> None:
    """POST a documented key, value lands on disk at mode 0o600."""
    store = app.state.env_store

    response = client.post(
        "/env",
        data={"key": "SLACK_BOT_TOKEN", "value": "xoxb-new-value"},
    )

    assert response.status_code == 200
    # Success toast embedded in the response.
    assert "Saved SLACK_BOT_TOKEN" in response.text

    # Reload from disk to prove persistence.
    reloaded = EnvStore(anna_home=store.path.parent).load()
    assert reloaded["SLACK_BOT_TOKEN"] == "xoxb-new-value"

    # Regression pin on the 0o600 re-chmod EnvStore enforces.
    assert _mode(store.path) == 0o600


# ---------------------------------------------------------------------------
# 5. POST /env with unknown key, no is_extra → 422.
# ---------------------------------------------------------------------------


def test_post_unknown_key_without_is_extra_returns_422(client: TestClient) -> None:
    """Typos surface as 422 + a toast naming the Extras escape hatch."""
    response = client.post(
        "/env",
        data={"key": "RANDOM_UNDOCUMENTED_KEY_XYZ", "value": "foo"},
    )

    assert response.status_code == 422
    assert "Unknown key" in response.text
    # Toast hints at the workflow fix (check Extras).
    assert "Extras" in response.text


# ---------------------------------------------------------------------------
# 6. POST /env with unknown key + is_extra=true → 200, written.
# ---------------------------------------------------------------------------


def test_post_unknown_key_with_is_extra_succeeds(client: TestClient) -> None:
    """The Extras workflow accepts an undocumented key when opted in."""
    store = app.state.env_store

    response = client.post(
        "/env",
        data={
            "key": "RANDOM_UNDOCUMENTED_KEY_XYZ",
            "value": "foo",
            "is_extra": "true",
        },
    )

    assert response.status_code == 200
    reloaded = EnvStore(anna_home=store.path.parent).load()
    assert reloaded["RANDOM_UNDOCUMENTED_KEY_XYZ"] == "foo"


# ---------------------------------------------------------------------------
# 7. DELETE /env/{key} removes.
# ---------------------------------------------------------------------------


def test_delete_removes_key(client: TestClient) -> None:
    """DELETE on a documented key returns 204 + key gone from disk."""
    store = app.state.env_store
    store.set("SLACK_BOT_TOKEN", "to-be-removed")

    response = client.delete("/env/SLACK_BOT_TOKEN")

    assert response.status_code == 204
    # Empty body — HTMX hx-swap="delete" doesn't need a payload.
    assert response.text == ""

    reloaded = EnvStore(anna_home=store.path.parent).load()
    assert "SLACK_BOT_TOKEN" not in reloaded


# ---------------------------------------------------------------------------
# 8. Audit-payload obligation test stub for subtask 12.
# ---------------------------------------------------------------------------


def test_set_audit_payload_never_contains_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-cutting secret-redaction obligation for POST /env.

    Monkey-patches ``anna_web.audit.audit_event`` (the underlying
    writer the wrapper hands off to) so every audit call is captured
    into a list. The handler is invoked with a canary secret value
    that is highly unlikely to appear in any other field; we then
    walk every captured event and assert the canary never appears
    in any payload value.

    Belt-and-suspenders: also verify the event name is
    ``audit.web.dashboard.secret_write`` and the ``key`` field is the
    submitted key. If the audit emit was silently dropped the event
    list would be empty and the second assertion catches it.
    """
    from anna_web import audit as web_audit

    canary = "xoxb-canary-do-not-leak"
    captured: list[dict] = []

    def _capture(name: str, **kwargs: object) -> None:
        captured.append({"name": name, **kwargs})

    monkeypatch.setattr(web_audit, "audit_event", _capture)

    response = client.post(
        "/env",
        data={"key": "SLACK_BOT_TOKEN", "value": canary},
    )
    assert response.status_code == 200, response.text

    # Belt-and-suspenders: the secret_write event fired with the key.
    secret_writes = [e for e in captured if e["name"].endswith("secret_write")]
    assert secret_writes, "secret_write audit event was not emitted"
    assert secret_writes[0]["name"] == "audit.web.dashboard.secret_write"
    assert secret_writes[0].get("key") == "SLACK_BOT_TOKEN"

    # Load-bearing assertion: the canary value never appears anywhere
    # in any captured event payload. Walk every captured dict
    # depth-first stringifying as we go so a nested structure (e.g.
    # an exception repr that swept up the form body) still trips.
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
            f"secret value leaked into audit payload: {event!r}"
        )


# ---------------------------------------------------------------------------
# 9. CRITICAL — secret value never appears in the GET /env HTML body.
# ---------------------------------------------------------------------------


def test_secret_value_never_in_get_env_html_body(client: TestClient) -> None:
    """The load-bearing masking assertion.

    Sets a uniquely-recognizable secret string on a documented secret
    key. The GET /env response body must NOT contain that string
    anywhere — masking has to be real, not just visual via CSS or a
    JS-side blur. If a future template edit accidentally surfaces the
    secret in HTML this test will turn red.
    """
    secret = "xoxb-supersecret-canary-do-not-leak"
    store = app.state.env_store
    store.set("SLACK_BOT_TOKEN", secret)

    response = client.get("/env")

    assert response.status_code == 200
    assert secret not in response.text, (
        "secret value leaked into GET /env HTML body — masking broken"
    )
