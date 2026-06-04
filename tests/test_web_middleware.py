"""Tests for ``anna_web.middleware.SameOriginMiddleware`` (subtask 12).

Seven cases pin the contract:

1. ``GET /healthz`` passes without an Origin header (external uptime
   monitoring must be able to probe without forging an Origin).
2. ``POST`` without an Origin header returns 403 + "missing Origin".
3. ``POST`` with a mismatched Origin returns 403.
4. ``POST`` with a matching Origin passes through to the handler.
5. ``PUT`` and ``DELETE`` behave the same as POST.
6. ``HEAD`` and ``OPTIONS`` pass through without an Origin.
7. Every request carries ``request.state.request_id`` after the
   middleware runs — verified by inspecting it inside a probe route.

Fixture strategy: build a tiny FastAPI app with the middleware bolted
on and a probe route that echoes ``request.state.request_id``. No
real ConfigStore/EnvStore/etc. — the middleware doesn't need them and
keeping the app minimal makes the test loop fast.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from anna_web.middleware import SameOriginMiddleware, is_wildcard_host


ALLOWED_ORIGIN = "http://127.0.0.1:8765"

# Concrete LAN address a wildcard (0.0.0.0) bind is actually reached at.
# The wildcard probe app's TestClient dials this, so the browser-style
# Host/Origin headers carry it and the Host-anchored same-origin check
# has something real to match against.
WILDCARD_BASE_URL = "http://192.168.1.50:8765"
WILDCARD_ORIGIN = "http://192.168.1.50:8765"

# Sentinel that signals the conftest's auto-Origin patch to NOT
# inject the default Origin header on this request. Tests that
# exercise the no-Origin rejection branch use this so the middleware
# sees a bare request the way curl would deliver it.
NO_ORIGIN: dict[str, str] = {"x-test-omit-origin": "1"}


def _build_probe_app(
    *,
    allowed_origin: str = ALLOWED_ORIGIN,
    wildcard_host: bool = False,
) -> FastAPI:
    """Construct a minimal FastAPI app wired with the middleware.

    The probe route returns ``request.state.request_id`` so the
    request_id-tagging assertion can read it back through HTTP.

    ``allowed_origin`` / ``wildcard_host`` mirror the middleware's
    constructor so the wildcard-bind cases can exercise the
    Host-header-anchored path.
    """
    app = FastAPI()

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "request_id": request.state.request_id})

    @app.post("/config/web")
    async def post_config(request: Request) -> JSONResponse:
        return JSONResponse(
            {"posted": True, "request_id": request.state.request_id}
        )

    @app.put("/config/web")
    async def put_config(request: Request) -> JSONResponse:
        return JSONResponse({"put": True, "request_id": request.state.request_id})

    @app.delete("/config/web")
    async def delete_config(request: Request) -> JSONResponse:
        return JSONResponse(
            {"deleted": True, "request_id": request.state.request_id}
        )

    @app.options("/config/web")
    async def options_config(request: Request) -> JSONResponse:
        return JSONResponse(
            {"options": True, "request_id": request.state.request_id}
        )

    app.add_middleware(
        SameOriginMiddleware,
        allowed_origin=allowed_origin,
        wildcard_host=wildcard_host,
    )
    return app


# ---------------------------------------------------------------------------
# 1. GET /healthz unrestricted.
# ---------------------------------------------------------------------------


def test_get_healthz_passes_without_origin() -> None:
    client = TestClient(_build_probe_app())
    response = client.get("/healthz", headers=NO_ORIGIN)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    # request_id should be tagged on every request, including GETs.
    assert isinstance(payload["request_id"], str)
    assert len(payload["request_id"]) > 0


# ---------------------------------------------------------------------------
# 2. POST without Origin → 403 missing Origin.
# ---------------------------------------------------------------------------


def test_post_without_origin_returns_403() -> None:
    client = TestClient(_build_probe_app())
    response = client.post("/config/web", json={}, headers=NO_ORIGIN)
    assert response.status_code == 403
    assert "missing Origin" in response.text


# ---------------------------------------------------------------------------
# 3. POST with mismatched Origin → 403.
# ---------------------------------------------------------------------------


def test_post_with_mismatched_origin_returns_403() -> None:
    client = TestClient(_build_probe_app())
    response = client.post(
        "/config/web",
        json={},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert "Origin" in response.text


def test_post_with_mismatched_port_returns_403() -> None:
    """A host match isn't enough — the port has to match exactly too."""
    client = TestClient(_build_probe_app())
    response = client.post(
        "/config/web",
        json={},
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 4. POST with matching Origin passes through.
# ---------------------------------------------------------------------------


def test_post_with_matching_origin_passes_through() -> None:
    client = TestClient(_build_probe_app())
    response = client.post(
        "/config/web",
        json={},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["posted"] is True
    assert isinstance(payload["request_id"], str)


# ---------------------------------------------------------------------------
# 5. PUT/DELETE behave the same as POST.
# ---------------------------------------------------------------------------


def test_put_without_origin_returns_403() -> None:
    client = TestClient(_build_probe_app())
    response = client.put("/config/web", json={}, headers=NO_ORIGIN)
    assert response.status_code == 403


def test_put_with_matching_origin_passes_through() -> None:
    client = TestClient(_build_probe_app())
    response = client.put(
        "/config/web", json={}, headers={"Origin": ALLOWED_ORIGIN}
    )
    assert response.status_code == 200
    assert response.json()["put"] is True


def test_delete_without_origin_returns_403() -> None:
    client = TestClient(_build_probe_app())
    response = client.delete("/config/web", headers=NO_ORIGIN)
    assert response.status_code == 403


def test_delete_with_matching_origin_passes_through() -> None:
    client = TestClient(_build_probe_app())
    response = client.delete(
        "/config/web", headers={"Origin": ALLOWED_ORIGIN}
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True


# ---------------------------------------------------------------------------
# 6. HEAD/OPTIONS pass through without Origin.
# ---------------------------------------------------------------------------


def test_head_passes_without_origin() -> None:
    client = TestClient(_build_probe_app())
    # HEAD requests should always pass through the middleware regardless
    # of Origin. FastAPI's default routing returns 405 for HEAD against
    # a GET-only route (it doesn't auto-route HEAD → GET), so the
    # observable signal that the middleware let it through is "not 403".
    response = client.head("/healthz", headers=NO_ORIGIN)
    assert response.status_code != 403


def test_options_passes_without_origin() -> None:
    client = TestClient(_build_probe_app())
    response = client.options("/config/web", headers=NO_ORIGIN)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 7. request.state.request_id is set on every request.
# ---------------------------------------------------------------------------


def test_request_id_tagged_on_every_request() -> None:
    """Both a passing GET and a passing POST carry a non-empty request_id."""
    client = TestClient(_build_probe_app())

    get_response = client.get("/healthz")
    assert get_response.status_code == 200
    get_id = get_response.json()["request_id"]
    assert isinstance(get_id, str) and len(get_id) > 0

    post_response = client.post(
        "/config/web", json={}, headers={"Origin": ALLOWED_ORIGIN}
    )
    assert post_response.status_code == 200
    post_id = post_response.json()["request_id"]
    assert isinstance(post_id, str) and len(post_id) > 0

    # Different requests get different ids (uuid4 is collision-free in
    # practice; a clash here would be a regression on the tagging).
    assert get_id != post_id


def test_request_id_is_short() -> None:
    """The id is short (12 hex chars) — long enough to debug, short for logs."""
    client = TestClient(_build_probe_app())
    response = client.get("/healthz")
    request_id = response.json()["request_id"]
    # uuid4().hex[:12] is exactly 12 lowercase hex chars.
    assert len(request_id) == 12
    int(request_id, 16)  # parseable as hex; raises ValueError if not


# ---------------------------------------------------------------------------
# 8. Wildcard bind (0.0.0.0 / ::) anchors the same-origin check to the
#    request Host header instead of the un-matchable static origin.
#
#    Regression guard: building allowed_origin as "http://0.0.0.0:8765"
#    for a LAN bind used to 403 every POST (no browser sends that Origin),
#    silently making the dashboard read-only. With wildcard_host=True the
#    middleware compares the Origin against the address the browser
#    actually dialed (the Host header), so a real same-origin POST passes
#    while cross-site and missing-Origin requests still 403.
# ---------------------------------------------------------------------------


def _wildcard_client() -> TestClient:
    """TestClient for a 0.0.0.0-bound probe app, dialed at a LAN address.

    ``base_url`` drives the ``Host`` header TestClient sends, so the
    Host-anchored same-origin check has a concrete address to match.
    """
    app = _build_probe_app(allowed_origin="http://0.0.0.0:8765", wildcard_host=True)
    return TestClient(app, base_url=WILDCARD_BASE_URL)


def test_wildcard_bind_accepts_same_origin_post() -> None:
    """A same-origin POST (Origin == the dialed Host) is NOT 403."""
    client = _wildcard_client()
    response = client.post(
        "/config/web",
        json={},
        headers={"Origin": WILDCARD_ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["posted"] is True


def test_wildcard_bind_rejects_cross_origin_post() -> None:
    """Cross-site Origin against a wildcard bind still 403s."""
    client = _wildcard_client()
    response = client.post(
        "/config/web",
        json={},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert "Origin" in response.text


def test_wildcard_bind_rejects_missing_origin_post() -> None:
    """Missing Origin on a mutating request still 403s under a wildcard bind."""
    client = _wildcard_client()
    response = client.post("/config/web", json={}, headers=NO_ORIGIN)
    assert response.status_code == 403
    assert "missing Origin" in response.text


def test_wildcard_bind_rejects_mismatched_port_post() -> None:
    """The Host-anchored check still enforces an exact port match."""
    client = _wildcard_client()
    response = client.post(
        "/config/web",
        json={},
        headers={"Origin": "http://192.168.1.50:9999"},
    )
    assert response.status_code == 403


def test_loopback_bind_unchanged_with_static_origin() -> None:
    """Non-wildcard (loopback) behavior is byte-for-byte the strict match.

    A matching static Origin passes; a host that merely *looks* like the
    box but isn't the configured bind still 403s. This pins that the
    wildcard branch did not leak into the default path.
    """
    client = TestClient(_build_probe_app())  # wildcard_host defaults False
    ok = client.post("/config/web", json={}, headers={"Origin": ALLOWED_ORIGIN})
    assert ok.status_code == 200
    # Same bare request the wildcard path would accept off its Host header
    # must NOT pass the strict loopback static match.
    bad = client.post(
        "/config/web", json={}, headers={"Origin": "http://testserver"}
    )
    assert bad.status_code == 403


# ---------------------------------------------------------------------------
# 9. is_wildcard_host classifier.
# ---------------------------------------------------------------------------


def test_is_wildcard_host_true_for_unspecified_binds() -> None:
    assert is_wildcard_host("0.0.0.0") is True
    assert is_wildcard_host("::") is True
    assert is_wildcard_host("[::]") is True  # bracketed IPv6 literal


def test_is_wildcard_host_false_for_concrete_and_empty() -> None:
    assert is_wildcard_host("127.0.0.1") is False
    assert is_wildcard_host("::1") is False
    assert is_wildcard_host("192.168.1.50") is False
    assert is_wildcard_host("localhost") is False
    assert is_wildcard_host("") is False
    assert is_wildcard_host(None) is False
