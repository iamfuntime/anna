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

import ipaddress
import uuid
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp


_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_wildcard_host(value: str | None) -> bool:
    """True when ``value`` is an all-interfaces wildcard bind (``0.0.0.0`` / ``::``).

    A wildcard bind has no single canonical origin: the browser reaches
    the dashboard at whatever concrete address routes to the box
    (``192.168.x.y``, a tailscale IP, a LAN hostname), so the literal
    ``http://0.0.0.0:8765`` origin string the app factory would otherwise
    build never matches a real ``Origin`` header. The app factory passes
    the result of this check to :class:`SameOriginMiddleware` so the
    same-origin comparison anchors to the request ``Host`` header for
    wildcard binds instead of the un-matchable static origin.

    Returns ``False`` for ``None``/empty and for any concrete literal or
    hostname (those keep the strict static-origin path). Tolerates a
    bracketed IPv6 literal like ``[::]``.
    """
    if value is None:
        return False
    host = str(value).strip()
    if not host:
        return False
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate).is_unspecified
    except ValueError:
        # Not an IP literal — a bare hostname is never a wildcard bind.
        return False


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

    ``wildcard_host`` handles the all-interfaces bind case. When the
    operator points ``cfg.web.host`` at ``0.0.0.0`` / ``::`` (the
    intended LAN-bind path), the static ``allowed_origin`` becomes
    ``http://0.0.0.0:8765`` — an origin no browser ever sends, so a
    strict static match would 403 every POST and silently turn the
    dashboard read-only. With ``wildcard_host=True`` the same-origin
    check instead anchors to the request's own ``Host`` header (the
    concrete address the browser actually used to reach the server),
    which a cross-site attacker still cannot forge. The static-origin
    path is left byte-for-byte unchanged for the loopback / concrete
    bind case.

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
      the expected origin — the configured static origin normally, or
      the request ``Host`` header when ``wildcard_host`` is set.
    * Mismatch: 403.
    * Match: pass through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origin: str,
        wildcard_host: bool = False,
    ) -> None:
        super().__init__(app)
        self._allowed_origin_raw = allowed_origin
        self._wildcard_host = wildcard_host
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
        # For a wildcard bind the static origin is un-matchable, so the
        # expected origin is derived from the request's own Host header
        # (the concrete address the browser used). For every other bind
        # it's the static origin parsed at construction time.
        expected = (
            self._host_origin_tuple(request)
            if self._wildcard_host
            else self._allowed_tuple
        )
        if requested is None or expected is None:
            return PlainTextResponse(
                "invalid Origin header",
                status_code=403,
            )

        if requested != expected:
            return PlainTextResponse(
                "Origin mismatch",
                status_code=403,
            )

        return await call_next(request)

    def _host_origin_tuple(
        self, request: Request
    ) -> tuple[str, str, int | None] | None:
        """Build the expected origin tuple from the request ``Host`` header.

        Used only for wildcard binds. The browser sends ``Host`` (the
        address it dialed) and ``Origin`` (scheme + that same address)
        consistently for a same-origin request, so comparing the
        ``Origin`` against ``scheme://<Host>`` enforces same-origin
        without a single canonical bind URL. A cross-site request still
        carries the attacker's ``Origin`` against the dashboard's
        ``Host`` and fails the comparison. Returns ``None`` (→ 403,
        fail-closed) when no ``Host`` header is present.
        """
        host_header = request.headers.get("host")
        if not host_header:
            return None
        return _origin_tuple(f"{request.url.scheme}://{host_header}")
