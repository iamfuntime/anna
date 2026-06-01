"""Unit tests for anna.tools.web_tools.

Uses httpx.MockTransport to stub the Brave Search REST API and arbitrary
URL fetches. No live network calls. Audit-emission paths are covered by
asserting the audit JSONL is written; the JSON shape is not parsed back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from anna.config import AnnaConfig
from anna.tools.web_tools import WebTools


CONV_KEY = "slack:dm:UTEST"


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    (tmp_path / "anna_home" / "audit").mkdir(parents=True, exist_ok=True)
    return cfg


def _install_mock(tools: WebTools, handler: Any) -> None:
    """Replace tools._client with one wired to a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    tools._client = httpx.AsyncClient(transport=transport, follow_redirects=True)


def _read_audit(cfg: AnnaConfig) -> list[dict[str, Any]]:
    audit_files = list(cfg.audit_dir.glob("audit-*.jsonl"))
    if not audit_files:
        return []
    events: list[dict[str, Any]] = []
    for f in sorted(audit_files):
        for line in f.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "abc")
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "search.brave.com" in str(request.url)
        assert request.headers.get("X-Subscription-Token") == "abc"
        assert request.url.params.get("q") == "CVE-2026-20182"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://example.com/1",
                            "title": "Cisco SD-WAN advisory",
                            "description": "Critical RCE in vManage",
                            "page_age": "1 day ago",
                        },
                        {
                            "url": "https://example.com/2",
                            "title": "CISA KEV entry",
                            "description": "FCEB 3-day deadline",
                        },
                    ]
                }
            },
        )

    _install_mock(tools, handler)
    result = await tools.web_search(
        query="CVE-2026-20182",
        max_results=5,
        conv_key=CONV_KEY,
    )
    text = result["content"][0]["text"]
    assert "2 results" in text
    assert "https://example.com/1" in text
    assert "Cisco SD-WAN advisory" in text
    assert "1 day ago" in text

    events = _read_audit(cfg)
    event_names = [e["event"] for e in events]
    assert "audit.tool.web_search.call" in event_names
    assert "audit.tool.web_search.complete" in event_names

    await tools.aclose()


@pytest.mark.asyncio
async def test_web_search_missing_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)

    with pytest.raises(RuntimeError, match="BRAVE_SEARCH_API_KEY"):
        await tools.web_search(query="x", max_results=1, conv_key=CONV_KEY)

    events = _read_audit(cfg)
    fail = [e for e in events if e["event"] == "audit.tool.web_search.fail"]
    assert fail
    assert fail[0]["error"] == "missing_api_key"


@pytest.mark.asyncio
async def test_web_search_401_unauthorized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "bad")
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    _install_mock(tools, lambda req: httpx.Response(401, text="invalid token"))

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await tools.web_search(query="x", max_results=1, conv_key=CONV_KEY)
    await tools.aclose()


@pytest.mark.asyncio
async def test_web_search_429_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "abc")
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    _install_mock(tools, lambda req: httpx.Response(429, text="rate limited"))

    with pytest.raises(RuntimeError, match="HTTP 429"):
        await tools.web_search(query="x", max_results=1, conv_key=CONV_KEY)
    await tools.aclose()


@pytest.mark.asyncio
async def test_web_search_5xx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "abc")
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    _install_mock(tools, lambda req: httpx.Response(503, text="service unavailable"))

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await tools.web_search(query="x", max_results=1, conv_key=CONV_KEY)
    await tools.aclose()


@pytest.mark.asyncio
async def test_web_search_empty_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "abc")
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    _install_mock(tools, lambda req: httpx.Response(200, json={"web": {"results": []}}))

    result = await tools.web_search(query="zzzz", max_results=1, conv_key=CONV_KEY)
    assert "no Brave results" in result["content"][0]["text"]
    await tools.aclose()


@pytest.mark.asyncio
async def test_web_search_rejects_bad_max_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "abc")
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    for bad in (0, -1, 51, 100):
        with pytest.raises(ValueError, match="max_results"):
            await tools.web_search(query="x", max_results=bad, conv_key=CONV_KEY)


@pytest.mark.asyncio
async def test_web_search_rejects_empty_query(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    with pytest.raises(ValueError, match="query"):
        await tools.web_search(query="", max_results=1, conv_key=CONV_KEY)
    with pytest.raises(ValueError, match="query"):
        await tools.web_search(query="   ", max_results=1, conv_key=CONV_KEY)


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_html_to_markdown(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        # UA assertion: standardized Chrome-on-Linux.
        assert "Chrome/" in req.headers["User-Agent"]
        return httpx.Response(
            200,
            html="<html><body><h1>Hello</h1><p>Paragraph.</p></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _install_mock(tools, handler)
    result = await tools.web_fetch(url="https://example.com/page", conv_key=CONV_KEY)
    text = result["content"][0]["text"]
    assert "used_playwright: false" in text
    assert "content_type: text/html" in text
    assert "Hello" in text
    assert "Paragraph." in text
    await tools.aclose()


@pytest.mark.asyncio
async def test_web_fetch_non_text_content(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.7 binary stuff",
            headers={"content-type": "application/pdf"},
        )

    _install_mock(tools, handler)
    result = await tools.web_fetch(url="https://example.com/x.pdf", conv_key=CONV_KEY)
    text = result["content"][0]["text"]
    assert "content_type: application/pdf" in text
    assert "non-text" in text
    assert "vault_download" in text
    await tools.aclose()


@pytest.mark.asyncio
async def test_web_fetch_rejects_bad_url(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)
    with pytest.raises(ValueError, match="http"):
        await tools.web_fetch(url="ftp://example.com/x", conv_key=CONV_KEY)
    with pytest.raises(ValueError, match="url"):
        await tools.web_fetch(url="", conv_key=CONV_KEY)


@pytest.mark.asyncio
async def test_web_fetch_http_error(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = WebTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    _install_mock(tools, handler)
    with pytest.raises(RuntimeError, match="transport error"):
        await tools.web_fetch(url="https://nonexistent.invalid/", conv_key=CONV_KEY)
    await tools.aclose()
