"""Bounded, account-owned execution for synchronous Google discovery APIs."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.log import get_logger
from anna.tools.google_auth import GoogleAuthError, find_account, load_credentials

ApiName = Literal["gmail", "calendar"]
T = TypeVar("T")
GOOGLE_HTTP_TIMEOUT_SECONDS = 30.0
_MAX_ACCEPTED_PER_ACCOUNT = 2
_API_VERSIONS: dict[ApiName, str] = {"gmail": "v1", "calendar": "v3"}


class GoogleBackpressureError(GoogleAuthError):
    """Raised before submission when an account's bounded lane is full."""


@dataclass
class _AccountLane:
    executor: ThreadPoolExecutor
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0
    cache: dict[ApiName, Any] = field(default_factory=dict)
    running_task: asyncio.Task[Any] | None = None


class GoogleClients:
    """One owner thread per account, shared across that account's APIs.

    Gmail and Calendar share a lane because OAuth refresh and token replacement
    are account state. At most two requests are accepted per lane; only one is
    submitted to its executor at a time, so the executor's unbounded queue is
    never exposed to callers.
    """

    def __init__(self, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.google.clients")
        self._lanes: dict[str, _AccountLane] = {}
        self._state_lock = threading.Lock()
        self._invalidated: set[str] = set()
        self._closed = False

    @property
    def config(self) -> AnnaConfig:
        return self._config

    def configured_slugs(self) -> list[str]:
        return [a.slug for a in self._config.google.accounts]

    def account(self, slug: str) -> GoogleAccountConfig:
        return find_account(self._config, slug)

    def _lane(self, slug: str) -> _AccountLane:
        self.account(slug)  # validate before executor allocation
        with self._state_lock:
            lane = self._lanes.get(slug)
            if lane is None:
                lane = _AccountLane(ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"anna-google-{slug}"))
                self._lanes[slug] = lane
            return lane

    def _service(self, slug: str, api: ApiName, lane: _AccountLane) -> Any:
        # ``invalidate`` may be called from another thread during shutdown.
        # Only the account owner mutates its cache, but access to the request
        # flag itself is protected by the process-wide state lock.
        with self._state_lock:
            invalidated = slug in self._invalidated
            if invalidated:
                self._invalidated.discard(slug)
        if invalidated:
            self._dispose_cached_services(lane)
        cached = lane.cache.get(api)
        if cached is not None:
            return cached
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build

        account = self.account(slug)
        creds = load_credentials(self._config, account, timeout_seconds=GOOGLE_HTTP_TIMEOUT_SECONDS)
        # ``build`` rejects a simultaneous ``credentials`` and ``http``.
        # Authorize an account-owned transport explicitly so discovery, API
        # execution, and automatic refresh all use the bounded socket timeout.
        transport = httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=transport)
        service = build(api, _API_VERSIONS[api], http=http, cache_discovery=False)
        lane.cache[api] = service
        return service

    async def _run_lane(
        self,
        lane: _AccountLane,
        slug: str,
        api: ApiName,
        operation: Callable[[Any], T],
    ) -> T:
        """Run one request and retain ownership until its thread really exits."""
        try:
            async with lane.lock:
                lane.running_task = asyncio.current_task()
                if self._closed:
                    raise RuntimeError("GoogleClients is closed")
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(
                    lane.executor,
                    lambda: operation(self._service(slug, api, lane)),
                )
                # Do not apply the caller's deadline here. A synchronous
                # request cannot be killed safely; retaining the lane lock
                # until it returns prevents a second request from sharing its
                # credentials or transport after the caller has timed out.
                return await future
        finally:
            if lane.running_task is asyncio.current_task():
                lane.running_task = None
            lane.pending -= 1

    @staticmethod
    def _observe_late_task(task: asyncio.Task[Any]) -> None:
        """Consume results after a caller gave up, avoiding unhandled errors."""
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            # The original caller already received its cancellation/timeout.
            pass

    def _dispose_cached_services(self, lane: _AccountLane) -> None:
        """Close cached resources on the account owner thread only."""
        for service in lane.cache.values():
            closer = getattr(service, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:
                    self._log.debug("google.service.close_failed", error=str(exc))
        lane.cache.clear()

    async def run(
        self,
        slug: str,
        api: ApiName,
        operation: Callable[[Any], T],
        *,
        timeout_seconds: float = GOOGLE_HTTP_TIMEOUT_SECONDS,
    ) -> T:
        if self._closed:
            raise RuntimeError("GoogleClients is closed")
        if api not in self.account(slug).services:
            raise GoogleAuthError(f"google account {slug!r} is not authorized for {api}; "
                                  "reconnect it with that service selected")
        lane = self._lane(slug)
        if lane.pending >= _MAX_ACCEPTED_PER_ACCOUNT:
            raise GoogleBackpressureError(f"google account {slug!r} is busy; retry later")
        lane.pending += 1
        task = asyncio.create_task(self._run_lane(lane, slug, api, operation))
        try:
            # Shielding is crucial: a caller deadline is only a response
            # deadline. It cannot safely interrupt httplib2 or token refresh.
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            if lane.running_task is not task:
                # A cancelled caller whose request has not entered the owner
                # thread can be removed from the bounded queue immediately.
                task.cancel()
            task.add_done_callback(self._observe_late_task)
            raise

    def invalidate(self, slug: str | None = None) -> None:
        """Request owner-thread cache eviction after any in-flight work."""
        slugs = self.configured_slugs() if slug is None else [slug]
        if slug is not None:
            self.account(slug)
        with self._state_lock:
            self._invalidated.update(slugs)

    def close(self, *, wait: bool = False) -> None:
        """Reject new work; cleanup follows already-running provider calls.

        Python cannot forcibly stop a thread blocked in provider I/O. A single
        owner-thread cleanup is therefore queued after that call, then the
        executor is shut down. ``wait=False`` is non-blocking but never lets a
        later request reuse the account's cached transport.
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            lanes = list(self._lanes.values())
        for lane in lanes:
            # There can be at most one submitted provider operation (the lane
            # lock keeps later requests in asyncio), so this adds one bounded
            # cleanup item after it. Do not cancel it during shutdown.
            lane.executor.submit(self._dispose_cached_services, lane)
            lane.executor.shutdown(wait=wait, cancel_futures=False)

    def gmail(self, slug: str) -> Any:
        raise RuntimeError("synchronous Google access is unsafe; use await GoogleClients.run(..., 'gmail', ...)")

    def calendar(self, slug: str) -> Any:
        raise RuntimeError("synchronous Google access is unsafe; use await GoogleClients.run(..., 'calendar', ...)")


__all__ = ["GOOGLE_HTTP_TIMEOUT_SECONDS", "GoogleAuthError", "GoogleBackpressureError", "GoogleClients"]
