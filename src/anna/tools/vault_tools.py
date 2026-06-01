"""Vault-side write tools.

``vault_download`` is the only tool here for now. It fetches a URL with
httpx and streams the body to ``config.tools.vault_download.destination``
(default ``~/Obsidian/ANNA/Inbox/``). Mid-stream abort + partial cleanup
if the response exceeds ``max_size_bytes``.

Filename resolution order:

1. ``Content-Disposition: filename=`` if the server set one.
2. URL path basename if it has an extension.
3. A short hash of the URL.

The extension is derived from the response ``Content-Type`` via
``mimetypes.guess_extension`` so a PDF served with a query-suffixed URL
still lands as ``.pdf``.

Collisions append a numeric suffix (``-1``, ``-2``, ...). Existing files
are never overwritten — the operator can clean up the Inbox manually.

Audit events: ``audit.tool.vault_download.{call,complete,fail}``. URL +
bytes-written go to the audit log; the file contents do not.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _sanitize_stem(raw: str) -> str:
    """Reduce an arbitrary filename string to alnum + dash + underscore + dot.

    Multiple consecutive bad chars collapse to a single dash. Leading and
    trailing dashes are stripped. Empty result returns an empty string;
    the caller picks a fallback.
    """
    cleaned = _SAFE_CHARS.sub("-", raw).strip("-_.")
    return cleaned


def _filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    # Handle both `filename="x"` and `filename=x; ...` forms. Doesn't
    # implement RFC 5987 `filename*=UTF-8''...` because the consumers
    # don't currently need it; falls through to URL basename in that
    # case.
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', header, re.IGNORECASE)
    if not match:
        return None
    return unquote(match.group(1)).strip()


def _filename_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return None
    last = unquote(last)
    return last if "." in last else None


def _resolve_destination_path(
    destination_dir: Path,
    *,
    url: str,
    content_disposition: str | None,
    content_type: str,
) -> Path:
    """Pick a non-colliding destination path."""
    # Try content-disposition first.
    cd_name = _filename_from_content_disposition(content_disposition)
    raw_name: str | None = None
    if cd_name:
        raw_name = cd_name
    else:
        url_name = _filename_from_url(url)
        if url_name:
            raw_name = url_name

    if raw_name:
        if "." in raw_name:
            stem, _, suffix = raw_name.rpartition(".")
            stem = _sanitize_stem(stem) or "download"
            ext = "." + _sanitize_stem(suffix)
        else:
            stem = _sanitize_stem(raw_name) or "download"
            ext = ""
    else:
        # Hash fallback. Stable per URL so a retry produces the same
        # name (and so the collision suffix logic kicks in).
        stem = "download-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        ext = ""

    # If no extension and we have a content-type, try to guess one.
    if not ext and content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            ext = guessed

    base = destination_dir / f"{stem}{ext}"
    if not base.exists():
        return base

    # Collision: -1, -2, -3, ...
    for n in range(1, 1000):
        candidate = destination_dir / f"{stem}-{n}{ext}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(
        f"vault_download could not find a non-colliding name for {stem}{ext} "
        f"after 999 attempts; clean up {destination_dir}"
    )


class VaultTools:
    """Per-worker bundle for vault-write tools."""

    def __init__(self, *, config: AnnaConfig) -> None:
        self._config = config
        self._log = get_logger("anna.tools.vault")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
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

    async def vault_download(
        self,
        *,
        url: str,
        conv_key: str,
    ) -> dict[str, Any]:
        cfg = self._config.tools.vault_download
        if not url or not url.strip():
            raise ValueError("url cannot be empty")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"url must be http(s); got {url!r}")

        destination_dir = cfg.resolved_destination
        destination_dir.mkdir(parents=True, exist_ok=True)

        self._audit(
            "audit.tool.vault_download.call",
            conv_key=conv_key,
            url=url,
            destination_dir=str(destination_dir),
        )

        client = await self._get_client()
        # Use a per-worker httpx timeout proportional to the size cap but
        # not unbounded. 5 min is enough for a 50 MB file on a slow link.
        timeout = httpx.Timeout(300.0, connect=30.0)

        try:
            async with client.stream(
                "GET",
                url,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    self._audit(
                        "audit.tool.vault_download.fail",
                        conv_key=conv_key,
                        level="WARNING",
                        url=url,
                        error=f"http_{resp.status_code}",
                    )
                    return _text_response(
                        f"vault_download failed: HTTP {resp.status_code} from {url}"
                    )

                content_type = (resp.headers.get("content-type") or "").strip()
                content_disposition = resp.headers.get("content-disposition")

                # Pre-flight Content-Length check when the server set
                # one; saves the round trip if the file is obviously
                # too big.
                declared_length = resp.headers.get("content-length")
                if declared_length:
                    try:
                        declared = int(declared_length)
                        if declared > cfg.max_size_bytes:
                            self._audit(
                                "audit.tool.vault_download.fail",
                                conv_key=conv_key,
                                level="WARNING",
                                url=url,
                                error="size_cap_declared",
                                declared_bytes=declared,
                                max_bytes=cfg.max_size_bytes,
                            )
                            return _text_response(
                                f"vault_download refused: Content-Length "
                                f"{declared} exceeds max_size_bytes "
                                f"{cfg.max_size_bytes}"
                            )
                    except ValueError:
                        pass

                dest_path = _resolve_destination_path(
                    destination_dir,
                    url=url,
                    content_disposition=content_disposition,
                    content_type=content_type,
                )

                # Stream + tally. Abort + delete the partial file if
                # we cross the cap.
                written = 0
                tmp_path = dest_path.with_suffix(dest_path.suffix + ".partial")
                try:
                    with tmp_path.open("wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65_536):
                            written += len(chunk)
                            if written > cfg.max_size_bytes:
                                f.close()
                                tmp_path.unlink(missing_ok=True)
                                self._audit(
                                    "audit.tool.vault_download.fail",
                                    conv_key=conv_key,
                                    level="WARNING",
                                    url=url,
                                    error="size_cap_streaming",
                                    written_bytes=written,
                                    max_bytes=cfg.max_size_bytes,
                                )
                                return _text_response(
                                    f"vault_download aborted: response exceeded "
                                    f"max_size_bytes {cfg.max_size_bytes} mid-stream "
                                    f"(at {written} bytes), partial removed"
                                )
                            f.write(chunk)
                    tmp_path.rename(dest_path)
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    raise

        except httpx.TimeoutException as exc:
            self._audit(
                "audit.tool.vault_download.fail",
                conv_key=conv_key,
                level="WARNING",
                url=url,
                error="timeout",
            )
            raise RuntimeError(f"vault_download timed out for {url}") from exc
        except httpx.HTTPError as exc:
            self._audit(
                "audit.tool.vault_download.fail",
                conv_key=conv_key,
                level="WARNING",
                url=url,
                error=str(exc),
            )
            raise RuntimeError(f"vault_download transport error for {url}: {exc}") from exc

        self._audit(
            "audit.tool.vault_download.complete",
            conv_key=conv_key,
            url=url,
            destination=str(dest_path),
            bytes_written=written,
        )

        return _text_response(
            f"downloaded {written} bytes from {url}\n"
            f"saved to {dest_path}"
        )


__all__ = ["VaultTools"]
