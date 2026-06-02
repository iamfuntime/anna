"""Test-suite-wide fixtures and TestClient patches.

The web dashboard ships :class:`anna_web.middleware.SameOriginMiddleware`
as the v1 CSRF posture. Real browsers always send an ``Origin``
header on POST/PUT/DELETE per the Fetch spec, so the middleware's
"no Origin on mutating request → 403" rule mirrors how a deployed
dashboard behaves. Starlette's :class:`fastapi.testclient.TestClient`
is curl-like and does not auto-send Origin, so without this
conftest every pre-existing test that POSTs to a route would 403
the moment the middleware lands.

To keep the existing test surface stable while preserving the
middleware's runtime behavior, this conftest monkey-patches
:class:`httpx.Client.request` (the underlying transport TestClient
uses) once at session scope so every test request carries an
``Origin`` header matching the dashboard's default bind URL
``http://127.0.0.1:8765``. Tests that need to assert on the
middleware's rejection branch can still override the header per
call (see :mod:`tests.test_web_middleware` for examples — that
file constructs its TestClient against a minimal app and passes
explicit headers).

This file is test infrastructure, not a test case; it carries no
``test_*`` functions and adds no fixtures other than the autouse
patch.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


_ORIGIN_VALUE = "http://127.0.0.1:8765"

# Sentinel header name. A test that needs to exercise the
# no-Origin branch of the middleware passes this header (any value);
# the patched request method strips it AND skips the default-Origin
# injection so the request reaches the middleware with no Origin
# at all. The header name is namespaced under ``x-test-`` so it cannot
# collide with any real HTTP semantics the dashboard cares about.
_OMIT_ORIGIN_SENTINEL = "x-test-omit-origin"


@pytest.fixture(autouse=True, scope="session")
def _inject_default_origin_header() -> Any:
    """Monkey-patch httpx.Client.request to default Origin to the bind URL.

    Without this, every POST/PUT/DELETE in the existing test suite
    fails the SameOriginMiddleware check because TestClient never
    sends an Origin header by default. With it, tests behave like a
    browser session pointed at ``http://127.0.0.1:8765`` would.

    Tests that pass their own ``Origin`` header (e.g.
    :mod:`tests.test_web_middleware`) win because the per-call header
    overrides the default.
    """
    original_request = httpx.Client.request

    def _patched_request(
        self: httpx.Client,
        method: str,
        url: Any,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = kwargs.get("headers")
        # Normalize headers to a mutable dict-like so we can inspect
        # for a caller-supplied Origin or the omit sentinel.
        if headers is None:
            hdr_dict: dict[str, str] = {}
        else:
            try:
                hdr_dict = dict(headers)
            except (TypeError, ValueError):
                hdr_dict = {}

        # Sentinel: caller explicitly wants no Origin header. Strip
        # the sentinel and skip the default-Origin injection so the
        # request reaches the middleware bare.
        sentinel_keys = [k for k in hdr_dict if k.lower() == _OMIT_ORIGIN_SENTINEL]
        if sentinel_keys:
            for k in sentinel_keys:
                del hdr_dict[k]
            # Strip any inadvertent Origin too — the sentinel means
            # "this test wants the request to look like a curl POST".
            origin_keys = [k for k in hdr_dict if k.lower() == "origin"]
            for k in origin_keys:
                del hdr_dict[k]
            kwargs["headers"] = hdr_dict
            return original_request(self, method, url, *args, **kwargs)

        # Case-insensitive membership check — HTTP headers are
        # case-insensitive but Python dict keys are not.
        has_origin = any(k.lower() == "origin" for k in hdr_dict)
        if not has_origin:
            hdr_dict["Origin"] = _ORIGIN_VALUE
            kwargs["headers"] = hdr_dict
        return original_request(self, method, url, *args, **kwargs)

    httpx.Client.request = _patched_request  # type: ignore[method-assign]
    try:
        yield
    finally:
        httpx.Client.request = original_request  # type: ignore[method-assign]
