"""Per-account Google API service builders.

A thin layer over the credential loaders in :mod:`anna.tools.google_auth`
that returns ready-to-call Gmail and Calendar service objects. Services
are cached by ``(slug, api)`` so we do not re-refresh OAuth tokens or
re-build httplib2 transports for every tool call.

The cache is process-scoped and not thread-safe; ANNA runs single-threaded
asyncio per worker, so a per-worker GoogleClients is fine. If we ever
spawn parallel workers that share an instance, swap the cache for an
``asyncio.Lock``-guarded dict.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, TypeVar

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.log import get_logger
from anna.tools.google_auth import (
    GoogleAuthError,
    find_account,
    load_credentials,
)

ApiName = Literal["gmail", "calendar"]
T = TypeVar("T")

_API_VERSIONS: dict[ApiName, str] = {
    "gmail": "v1",
    "calendar": "v3",
}


class GoogleClients:
    """Service-object factory + cache.

    Construct once per process (or per worker). Call :meth:`gmail` or
    :meth:`calendar` with the account slug to get a built API client.

    The class is safe to construct even when no accounts are configured;
    every call will raise :class:`GoogleAuthError` until anna.yaml is
    updated.
    """

    def __init__(self, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.google.clients")
        # Each discovery Resource (and its httplib2 transport) belongs to one
        # dedicated thread.  Discovery, credential refresh, and execute must
        # never migrate that transport between arbitrary asyncio threads.
        self._cache: dict[tuple[str, ApiName], Any] = {}
        self._executors: dict[tuple[str, ApiName], ThreadPoolExecutor] = {}
        self._closed = False

    @property
    def config(self) -> AnnaConfig:
        return self._config

    def configured_slugs(self) -> list[str]:
        return [a.slug for a in self._config.google.accounts]

    def account(self, slug: str) -> GoogleAccountConfig:
        return find_account(self._config, slug)

    def _service(self, slug: str, api: ApiName) -> Any:
        cache_key = (slug, api)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Imports are deferred so an import of this module on a host
        # without google-api-python-client installed does not fail.
        from googleapiclient.discovery import build

        account = find_account(self._config, slug)
        creds = load_credentials(self._config, account)
        # cache_discovery=False suppresses the noisy
        # ``ImportError: file_cache is unavailable when using oauth2client``
        # log line that pops up on certain library combos.
        service = build(api, _API_VERSIONS[api], credentials=creds, cache_discovery=False)
        self._cache[cache_key] = service
        return service

    def _executor(self, slug: str, api: ApiName) -> ThreadPoolExecutor:
        key = (slug, api)
        executor = self._executors.get(key)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"anna-google-{slug}-{api}")
            self._executors[key] = executor
        return executor

    async def run(
        self, slug: str, api: ApiName, operation: Callable[[Any], T], *, timeout_seconds: float = 30.0
    ) -> T:
        """Run a complete discovery request on its account/API owner thread.

        Cancellation/timeout stops awaiting but cannot safely kill a Python
        thread in an in-flight HTTP call. The bounded one-worker executor
        prevents duplicate concurrent use; callers receive an explicit timeout
        rather than assuming uncertain remote effects did not happen.
        """
        if self._closed:
            raise RuntimeError("GoogleClients is closed")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor(slug, api), lambda: operation(self._service(slug, api)))
        return await asyncio.wait_for(future, timeout=timeout_seconds)

    def close(self, *, wait: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=True)
        self._executors.clear()
        self._cache.clear()

    def gmail(self, slug: str) -> Any:
        """Return a Gmail v1 service for the given account."""
        return self._service(slug, "gmail")

    def calendar(self, slug: str) -> Any:
        """Return a Calendar v3 service for the given account."""
        return self._service(slug, "calendar")

    def invalidate(self, slug: str | None = None) -> None:
        """Drop the cache for one slug, or all when slug is None.

        Call after a credential file is replaced or a service-account is
        re-keyed so the next call rebuilds with fresh creds.
        """
        if slug is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k[0] == slug]:
            self._cache.pop(key, None)


__all__ = ["GoogleAuthError", "GoogleClients"]
