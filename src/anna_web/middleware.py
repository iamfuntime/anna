"""Same-origin middleware for the ANNA web dashboard.

Subtask 12 of the Phase 2.5 buildout. This is the v1 CSRF posture
the plan calls out: any mutating request (POST/PUT/PATCH/DELETE)
whose ``Origin`` header doesn't match the dashboard's bind address
gets a 403 before the route handler ever runs. Browsers set
``Origin`` on all cross-origin requests AND on same-origin POSTs
from forms per the Fetch spec, so a real operator session in a
browser pointed at ``http://127.0.0.1:8765`` always passes the check.
CLI tools like ``curl`` don't send ``Origin`` by default, which is
intentional — we want curl POSTs from localhost to fail the
same-origin check unless the operator explicitly adds the header,
matching the plan's v1 CSRF posture.

GET, HEAD, and OPTIONS are unrestricted:

* ``GET /healthz`` must be reachable from external monitoring without
  an Origin header.
* HEAD and OPTIONS are non-mutating per HTTP semantics; restricting
  them would break common browser preflight flows.

Every request also gets a per-request ``request_id`` tagged onto
``request.state`` (a short uuid4) so :mod:`anna_web.audit` can pull
it into the emitted audit payload. The middleware tags the id
before any same-origin check so failed requests still carry the id
for tracing.

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md`` —
"FastAPI app structure" and "Architecture → Audit events" — for the
full design.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp


_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_tuple(origin: str) -> tuple[str, str, int | None] | None:
    """Parse an Origin header into ``(scheme, host, port)``.

    Returns ``None`` if the input is unparseable. We compare schemes,
    hostnames, and ports as a tuple so a mismatched port (the
    dashboard binds 8765, operator hits 8080 with the same host)
    surfaces as the 403 it should be.
    """
    if not origin:
        return None
    try:
        parsed = urlparse(origin)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    # urlparse returns None for port when omitted; the caller has
    # decided what port the dashboard binds, so an absent port on the
    # request side counts as "no explicit port, must match".
    return (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port)


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject mutating requests whose Origin doesn't match the bind address.

    Construction takes ``allowed_origin`` — the canonical URL string
    the dashboard is supposed to be reachable at. The app factory
    builds this from ``cfg.web.host`` + ``cfg.web.port``; tests can
    pass anything.

    Scope:

    * Methods: POST, PUT, PATCH, DELETE.
    * Exception: GET, HEAD, OPTIONS are unrestricted (healthz must be
      reachable from external monitoring without an Origin header).

    Logic:

    * Always tag ``request.state.request_id`` with a fresh short uuid4.
    * Bypass if the method is non-mutating.
    * Read the request's ``Origin`` header.
    * If absent and method is mutating: 403 with ``"missing Origin"``.
    * If present, parse and compare scheme + hostname + port against
      the configured allowed origin.
    * Mismatch: 403.
    * Match: pass through.
    """

    def __init__(self, app: ASGIApp, *, allowed_origin: str) -> None:
        super().__init__(app)
        self._allowed_origin_raw = allowed_origin
        parsed = _origin_tuple(allowed_origin)
        if parsed is None:
            # Caller blew up before we could enforce anything. Fail
            # closed: every mutating request will 403 until the
            # operator fixes the bind URL.
            self._allowed_tuple: tuple[str, str, int | None] | None = None
        else:
            self._allowed_tuple = parsed

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Tag the request id first so even rejected requests carry one
        # in any audit/log path that fires before we return.
        if not getattr(request.state, "request_id", None):
            request.state.request_id = uuid.uuid4().hex[:12]

        method = request.method.upper()
        if method not in _MUTATING_METHODS:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin is None:
            return PlainTextResponse(
                "missing Origin header on mutating request",
                status_code=403,
            )

        requested = _origin_tuple(origin)
        if requested is None or self._allowed_tuple is None:
            return PlainTextResponse(
                "invalid Origin header",
                status_code=403,
            )

        if requested != self._allowed_tuple:
            return PlainTextResponse(
                "Origin mismatch",
                status_code=403,
            )

        return await call_next(request)
