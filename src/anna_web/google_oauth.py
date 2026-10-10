"""One-at-a-time loopback OAuth sessions for the dashboard.

Wraps Google's supported installed-app flow (``InstalledAppFlow.run_local_server``): the library
binds a one-shot server on 127.0.0.1, generates ``state`` and a PKCE verifier, validates ``state`` on
the callback and exchanges the code.  This module only adds lifecycle around it:

* the consent URL is captured (never printed or logged) so the dashboard can show a link;
* a hard TTL, explicit cancel (a poke at the loopback port ends the blocking wait) and single-use sessions;
* the token is written ONLY by :func:`anna.tools.google_auth.persist_connected_token` after the granted scopes and
  the real mailbox identity were checked;
* in-memory session objects keep only a status and an error CLASS name. Flow objects (which hold the PKCE verifier
  and the exchanged credentials) are dropped as soon as the thread ends.

Nothing here is a hosted callback or an out-of-band flow.
"""

from __future__ import annotations

import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.log import get_logger
from anna.tools.google_auth import (
    GoogleAuthError,
    GoogleIdentityMismatch,
    persist_connected_token,
    required_scopes,
)

ACTIVE = {"starting", "waiting", "verifying"}
SESSION_TTL_SECONDS = 300
FORGET_AFTER_SECONDS = 600
_SUCCESS_PAGE = "Authorization received. You can close this tab and return to the ANNA dashboard."


class SessionError(RuntimeError):
    pass


@dataclass
class OAuthSession:
    id: str
    slug: str
    created: float
    status: str = "starting"  # starting | waiting | verifying | done | failed | expired | cancelled
    error: str = ""  # fixed code / exception CLASS name, never a message
    auth_url: str = ""
    port: int = 0
    cancelled: bool = False
    finished: float = 0.0
    thread: threading.Thread | None = field(default=None, repr=False)


def _make_flow_class() -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    class CapturingFlow(InstalledAppFlow):  # type: ignore[misc]
        """Records the consent URL instead of printing it."""

        captured: Callable[[str, str], None] | None = None

        def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
            url, state = super().authorization_url(**kwargs)
            if self.captured is not None:
                self.captured(url, str(getattr(self, "redirect_uri", "")))
            return url, state

    return CapturingFlow


class OAuthSessionManager:
    def __init__(self, config: AnnaConfig, *, ttl: int = SESSION_TTL_SECONDS,
                 on_connected: Callable[[GoogleAccountConfig], None] | None = None,
                 flow_class: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._ttl = ttl
        self._on_connected = on_connected
        self._flow_class = flow_class
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, OAuthSession] = {}
        self._log = get_logger("anna.web.google_oauth")

    # ------------------------------------------------------------------ public

    def start(self, account: GoogleAccountConfig) -> OAuthSession:
        if account.auth_type != "oauth":
            raise SessionError("only OAuth accounts use the browser flow")
        client = self._config.resolve_google_credentials_path(account)
        if not client.is_file():
            raise SessionError("import the shared OAuth client first")
        with self._lock:
            self._reap()
            if any(s.status in ACTIVE for s in self._sessions.values()):
                raise SessionError("another Google connection is already in progress; finish or cancel it first")
            sess = OAuthSession(id=secrets.token_urlsafe(24), slug=account.slug, created=self._clock())
            self._sessions[sess.id] = sess
        ready = threading.Event()

        def captured(url: str, redirect_uri: str) -> None:
            sess.auth_url = url
            sess.port = urlparse(redirect_uri).port or 0
            sess.status = "waiting"
            ready.set()

        flow_class = self._flow_class or _make_flow_class()
        thread = threading.Thread(target=self._run, args=(sess, account, client, flow_class, captured, ready),
                                  name=f"google-oauth-{account.slug}", daemon=True)
        sess.thread = thread
        thread.start()
        if not ready.wait(10) and sess.status == "starting":
            self.cancel(sess.id)
            raise SessionError("could not start the local callback listener (is the callback port busy?)")
        if sess.status in ("failed", "expired", "cancelled"):
            raise SessionError(f"could not start the OAuth flow ({sess.error or sess.status})")
        return sess

    def get(self, sid: str) -> OAuthSession | None:
        with self._lock:
            self._reap()
            return self._sessions.get(sid)

    def cancel(self, sid: str) -> bool:
        sess = self._sessions.get(sid)
        if sess is None or sess.status not in ACTIVE:
            return False
        sess.cancelled = True
        self._poke(sess)
        return True

    def active(self) -> OAuthSession | None:
        with self._lock:
            self._reap()
            return next((s for s in self._sessions.values() if s.status in ACTIVE), None)

    # ---------------------------------------------------------------- internals

    def _poke(self, sess: OAuthSession) -> None:
        """Unblock the library's one-shot loopback server with a request that carries no valid state."""
        if not sess.port:
            return
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{sess.port}/cancelled", timeout=2).close()  # noqa: S310
        except (urllib.error.URLError, OSError):
            pass

    def _reap(self) -> None:
        now = self._clock()
        for sess in list(self._sessions.values()):
            if sess.status in ACTIVE and now - sess.created > self._ttl + 5:
                sess.cancelled = True
                self._poke(sess)
            if sess.finished and now - sess.finished > FORGET_AFTER_SECONDS:
                self._sessions.pop(sess.id, None)

    def _run(self, sess: OAuthSession, account: GoogleAccountConfig, client: Any, flow_class: Any,
             captured: Callable[[str, str], None], ready: threading.Event) -> None:
        flow: Any = None
        creds: Any = None
        try:
            flow = flow_class.from_client_secrets_file(str(client), required_scopes(account))
            flow.captured = captured
            creds = flow.run_local_server(
                host="127.0.0.1", port=self._config.google.oauth_callback_port, open_browser=False,
                authorization_prompt_message="", success_message=_SUCCESS_PAGE,
                timeout_seconds=self._ttl, access_type="offline", prompt="consent")
            sess.status = "verifying"
            persist_connected_token(self._config, account, creds)
            sess.status = "done"
            if self._on_connected is not None:
                self._on_connected(account)
        except GoogleIdentityMismatch:
            sess.status, sess.error = "failed", "identity_mismatch"
        except GoogleAuthError:
            sess.status, sess.error = "failed", "not_authorized"
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if sess.cancelled:
                sess.status, sess.error = "cancelled", ""
            elif name == "WSGITimeoutError":
                sess.status, sess.error = "expired", ""
            else:
                sess.status, sess.error = "failed", name  # class only: messages may embed codes/URLs
        finally:
            flow = None  # drops the PKCE verifier and client secret copy
            creds = None
            sess.auth_url = ""
            sess.finished = self._clock()
            ready.set()
            self._log.info("google.oauth.session_end", slug=sess.slug, status=sess.status, error=sess.error)
            del flow, creds


def parse_state(auth_url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(auth_url).query)
