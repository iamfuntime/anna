"""Web access tool implementations.

Two tools live here, both backed by httpx:

* ``web_search`` — Brave Search REST wrapper. Reads the API key from the
  env var named in ``config.tools.web_search.api_key_env``. Returns a
  structured result list. Surfaces auth, quota, and timeout errors as
  clear ``RuntimeError`` messages so the SDK doesn't silently swallow a
  misconfigured deployment.

* ``web_fetch`` — httpx GET + ``markdownify`` HTML-to-Markdown. The
  ``used_playwright`` field in the response shape stays in place for
  forward-compat with the deferred JS-render fallback; flipping
  ``config.tools.web_fetch.playwright_fallback`` to ``true`` requires a
  later code slice plus ``playwright install chromium``.

Every call emits ``audit.tool.{web_search,web_fetch}.{call,complete,fail}``
events. Query text and URLs go to the audit log; raw bodies do not
(transcripts capture that).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from markdownify import markdownify as html_to_markdown

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger


_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
# Bounded retry for transient Brave failures (per-second 429s, 5xx, flaky
# transport). 1 initial attempt + up to 2 retries. Backoff for retry N
# (1-indexed) = base * N, i.e. ~1.2s then ~2.4s — Brave's free tier allows
# 1 query/second, so each delay must clear a full second.
_WEB_SEARCH_MAX_ATTEMPTS = 3
_WEB_SEARCH_BACKOFF_SECONDS = 1.2
_TEXT_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
)


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class WebTools:
    """Per-worker bundle holding the shared config + a long-lived httpx client.

    The httpx client is created lazily on first call and reused for the
    lifetime of the worker. Each call still enforces a per-request
    timeout via ``timeout=`` on the request itself; the client-level
    timeout defaults are intentionally loose.
    """

    def __init__(self, *, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.tools.web")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Follow redirects by default. Without this, NIST and similar
            # 301-happy hosts return a redirect body the model then has
            # to interpret.
            self._client = httpx.AsyncClient(follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _audit(
        self,
        event: str,
        *,
        conv_key: str,
        level: str = "INFO",
        **fields: Any,
    ) -> None:
        audit_event(
            event,
            audit_dir=self._config.audit_dir,
            actor="anna",
            conv_key=conv_key,
            fsync_on_write=self._config.logging.audit.fsync_on_write,
            level=level,
            **fields,
        )

    # ------------------------------------------------------------------
    # web_search
    # ------------------------------------------------------------------

    async def web_search(
        self,
        *,
        query: str,
        max_results: int | None,
        conv_key: str,
    ) -> dict[str, Any]:
        """Run a Brave Search query and return up to ``max_results`` hits.

        Each hit is `{url, title, snippet, published_at}`. ``published_at``
        is the Brave-reported ``page_age`` string (e.g. "2 days ago") or
        empty if Brave didn't surface one for the result.
        """
        cfg = self._config.tools.web_search
        if not query or not query.strip():
            raise ValueError("query cannot be empty")

        effective_max = max_results if max_results is not None else cfg.max_results
        if effective_max <= 0 or effective_max > 50:
            raise ValueError("max_results must be between 1 and 50")

        api_key = os.environ.get(cfg.api_key_env, "").strip()
        if not api_key:
            self._audit(
                "audit.tool.web_search.fail",
                conv_key=conv_key,
                level="WARNING",
                error="missing_api_key",
                env_var=cfg.api_key_env,
            )
            raise RuntimeError(
                f"web_search requires {cfg.api_key_env} in the environment; "
                f"add it to ~/anna/.env"
            )

        self._audit(
            "audit.tool.web_search.call",
            conv_key=conv_key,
            query=query,
            max_results=effective_max,
        )

        client = await self._get_client()
        # Bounded retry loop. The `.call` audit above fires once, before the
        # loop. Transient failures (429, 5xx, transport errors) emit a
        # `.retry` event and back off; only the final exhausted attempt falls
        # through to the existing `.fail` + raise branches below. Success,
        # 401, and non-retryable 4xx break out immediately with no retry.
        resp: httpx.Response | None = None
        for attempt in range(1, _WEB_SEARCH_MAX_ATTEMPTS + 1):
            backoff = _WEB_SEARCH_BACKOFF_SECONDS * attempt
            try:
                resp = await client.get(
                    _BRAVE_ENDPOINT,
                    params={"q": query, "count": effective_max},
                    headers={
                        "X-Subscription-Token": api_key,
                        "Accept": "application/json",
                    },
                    timeout=cfg.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                if attempt < _WEB_SEARCH_MAX_ATTEMPTS:
                    self._audit(
                        "audit.tool.web_search.retry",
                        conv_key=conv_key,
                        level="INFO",
                        query=query,
                        attempt=attempt,
                        status=None,
                        backoff_seconds=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                self._audit(
                    "audit.tool.web_search.fail",
                    conv_key=conv_key,
                    level="WARNING",
                    error="timeout",
                    query=query,
                )
                raise RuntimeError(
                    f"web_search timed out after {cfg.timeout_seconds}s"
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < _WEB_SEARCH_MAX_ATTEMPTS:
                    self._audit(
                        "audit.tool.web_search.retry",
                        conv_key=conv_key,
                        level="INFO",
                        query=query,
                        attempt=attempt,
                        status=None,
                        backoff_seconds=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                self._audit(
                    "audit.tool.web_search.fail",
                    conv_key=conv_key,
                    level="WARNING",
                    error=str(exc),
                    query=query,
                )
                raise RuntimeError(f"web_search transport error: {exc}") from exc

            # Transient HTTP failures: per-second rate limit (429) or upstream
            # outage (5xx). Retry until attempts are exhausted, then fall
            # through to the terminal status handling below.
            if (resp.status_code == 429 or resp.status_code >= 500) and (
                attempt < _WEB_SEARCH_MAX_ATTEMPTS
            ):
                self._audit(
                    "audit.tool.web_search.retry",
                    conv_key=conv_key,
                    level="INFO",
                    query=query,
                    attempt=attempt,
                    status=resp.status_code,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                continue

            # Success, 401, non-retryable 4xx, or an exhausted 429/5xx — stop
            # retrying and let the status handling below decide the outcome.
            break

        assert resp is not None  # loop always sets resp or raises

        if resp.status_code == 401:
            self._audit(
                "audit.tool.web_search.fail",
                conv_key=conv_key,
                level="WARNING",
                error="unauthorized",
                status=401,
            )
            raise RuntimeError(
                "web_search got HTTP 401 from Brave — API key is invalid or "
                "missing the required subscription"
            )
        if resp.status_code == 429:
            self._audit(
                "audit.tool.web_search.fail",
                conv_key=conv_key,
                level="WARNING",
                error="quota_exhausted",
                status=429,
            )
            raise RuntimeError(
                "web_search got HTTP 429 from Brave — monthly quota exhausted "
                "or per-second rate limit exceeded"
            )
        if resp.status_code >= 500:
            self._audit(
                "audit.tool.web_search.fail",
                conv_key=conv_key,
                level="WARNING",
                error="brave_5xx",
                status=resp.status_code,
            )
            raise RuntimeError(
                f"web_search got HTTP {resp.status_code} from Brave — upstream "
                f"outage, retry later"
            )
        if resp.status_code >= 400:
            self._audit(
                "audit.tool.web_search.fail",
                conv_key=conv_key,
                level="WARNING",
                error="brave_4xx",
                status=resp.status_code,
                body=resp.text[:500],
            )
            raise RuntimeError(
                f"web_search got HTTP {resp.status_code} from Brave: {resp.text[:200]}"
            )

        payload = resp.json()
        web_block = payload.get("web") or {}
        results_raw = web_block.get("results") or []

        results: list[dict[str, str]] = []
        for item in results_raw[:effective_max]:
            results.append(
                {
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("description", ""),
                    "published_at": item.get("page_age", ""),
                }
            )

        self._audit(
            "audit.tool.web_search.complete",
            conv_key=conv_key,
            query=query,
            count=len(results),
        )

        if not results:
            return _text_response(f"(no Brave results for {query!r})")

        lines = [f"{len(results)} results for {query!r}:"]
        for r in results:
            age = f" ({r['published_at']})" if r["published_at"] else ""
            lines.append(f"- {r['url']}{age}")
            lines.append(f"  {r['title']}")
            if r["snippet"]:
                lines.append(f"  {r['snippet']}")
        return _text_response("\n".join(lines))

    # ------------------------------------------------------------------
    # web_fetch
    # ------------------------------------------------------------------

    async def web_fetch(
        self,
        *,
        url: str,
        conv_key: str,
    ) -> dict[str, Any]:
        """Fetch a URL and return the body as Markdown.

        Non-HTML content (PDF, images) returns a content-type note rather
        than a markdown body — the writer sub-agent should fall back to
        ``vault_download`` if it needs the binary.

        ``used_playwright`` is hardcoded ``false`` here for the slim
        slice. The JS-render fallback hooks into the post-fetch branch
        below when ``cfg.playwright_fallback`` is enabled — see the
        comment marker ``# playwright-fallback-insertion-point``.
        """
        cfg = self._config.tools.web_fetch
        if not url or not url.strip():
            raise ValueError("url cannot be empty")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"url must be http(s); got {url!r}")

        self._audit(
            "audit.tool.web_fetch.call",
            conv_key=conv_key,
            url=url,
        )

        client = await self._get_client()
        try:
            resp = await client.get(
                url,
                headers={"User-Agent": cfg.user_agent},
                timeout=cfg.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            self._audit(
                "audit.tool.web_fetch.fail",
                conv_key=conv_key,
                level="WARNING",
                error="timeout",
                url=url,
            )
            raise RuntimeError(
                f"web_fetch timed out after {cfg.timeout_seconds}s for {url}"
            ) from exc
        except httpx.HTTPError as exc:
            self._audit(
                "audit.tool.web_fetch.fail",
                conv_key=conv_key,
                level="WARNING",
                error=str(exc),
                url=url,
            )
            raise RuntimeError(f"web_fetch transport error for {url}: {exc}") from exc

        content_type = (resp.headers.get("content-type") or "").lower()
        # Strip any charset suffix.
        primary_type = content_type.split(";", 1)[0].strip()

        is_text = any(primary_type.startswith(t) for t in _TEXT_CONTENT_TYPES)

        if not is_text:
            self._audit(
                "audit.tool.web_fetch.complete",
                conv_key=conv_key,
                url=url,
                status=resp.status_code,
                content_type=primary_type,
                content_length=len(resp.content),
                used_playwright=False,
                non_text=True,
            )
            return _text_response(
                f"url: {url}\n"
                f"status: {resp.status_code}\n"
                f"content_type: {primary_type}\n"
                f"content_length: {len(resp.content)}\n"
                f"used_playwright: false\n"
                f"\n(non-text content; use vault_download to save the file "
                f"or web_search to find a text representation)"
            )

        body_text = resp.text
        if primary_type in ("text/html", "application/xhtml+xml"):
            content_markdown = html_to_markdown(body_text, heading_style="ATX")
        else:
            content_markdown = body_text

        # playwright-fallback-insertion-point: when cfg.playwright_fallback
        # is true AND the extracted markdown is below a threshold (e.g.
        # 200 chars, configurable), retry the GET through a headless
        # Chromium session and rebuild content_markdown from the rendered
        # DOM. Set used_playwright=true on success. This branch is
        # currently inert; the dep + chromium download arrive in a later
        # slice.

        self._audit(
            "audit.tool.web_fetch.complete",
            conv_key=conv_key,
            url=url,
            status=resp.status_code,
            content_type=primary_type,
            content_length=len(content_markdown),
            used_playwright=False,
        )

        return _text_response(
            f"url: {url}\n"
            f"status: {resp.status_code}\n"
            f"content_type: {primary_type}\n"
            f"content_length: {len(content_markdown)}\n"
            f"used_playwright: false\n"
            f"\n{content_markdown}"
        )


__all__ = ["WebTools"]
