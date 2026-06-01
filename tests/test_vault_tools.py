"""Unit tests for anna.tools.vault_tools.vault_download.

Uses httpx.MockTransport to inject responses. Filesystem writes land in
``tmp_path``; the vault_download.destination override is applied per
test via the tools config block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from anna.config import AnnaConfig
from anna.tools.vault_tools import VaultTools


CONV_KEY = "slack:dm:UTEST"


def _make_config(tmp_path: Path, *, destination: Path | None = None) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    (tmp_path / "anna_home" / "audit").mkdir(parents=True, exist_ok=True)
    if destination is not None:
        cfg.tools.vault_download.destination = str(destination)
    else:
        cfg.tools.vault_download.destination = str(tmp_path / "vault_inbox")
    return cfg


def _install_mock(tools: VaultTools, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    tools._client = httpx.AsyncClient(transport=transport, follow_redirects=True)


def _read_audit(cfg: AnnaConfig) -> list[dict[str, Any]]:
    audit_files = list(cfg.audit_dir.glob("audit-*.jsonl"))
    events: list[dict[str, Any]] = []
    for f in sorted(audit_files):
        for line in f.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


@pytest.mark.asyncio
async def test_vault_download_happy_path(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = VaultTools(config=cfg)

    payload = b"%PDF-1.7\n...binary PDF body..."

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="advisory.pdf"',
            },
        )

    _install_mock(tools, handler)
    result = await tools.vault_download(
        url="https://example.com/adv?id=1",
        conv_key=CONV_KEY,
    )
    text = result["content"][0]["text"]
    assert "downloaded" in text
    dest = cfg.tools.vault_download.resolved_destination
    files = list(dest.iterdir())
    assert len(files) == 1
    assert files[0].name == "advisory.pdf"
    assert files[0].read_bytes() == payload

    events = _read_audit(cfg)
    event_names = [e["event"] for e in events]
    assert "audit.tool.vault_download.call" in event_names
    assert "audit.tool.vault_download.complete" in event_names

    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_filename_from_url_when_no_disposition(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = VaultTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"hello",
            headers={"content-type": "text/plain"},
        )

    _install_mock(tools, handler)
    await tools.vault_download(
        url="https://example.com/data/report.txt",
        conv_key=CONV_KEY,
    )
    dest = cfg.tools.vault_download.resolved_destination
    files = sorted(p.name for p in dest.iterdir())
    assert "report.txt" in files
    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_filename_hash_fallback(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = VaultTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x",
            headers={"content-type": "application/octet-stream"},
        )

    _install_mock(tools, handler)
    # URL has no extension in path and content type is generic, so the
    # hash fallback is the only option.
    await tools.vault_download(
        url="https://example.com/api/blob",
        conv_key=CONV_KEY,
    )
    dest = cfg.tools.vault_download.resolved_destination
    files = sorted(p.name for p in dest.iterdir())
    assert len(files) == 1
    assert files[0].startswith("download-")
    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_collision_suffix(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = VaultTools(config=cfg)

    dest = cfg.tools.vault_download.resolved_destination
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.txt").write_bytes(b"existing")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"new",
            headers={"content-type": "text/plain"},
        )

    _install_mock(tools, handler)
    await tools.vault_download(
        url="https://example.com/report.txt",
        conv_key=CONV_KEY,
    )
    files = sorted(p.name for p in dest.iterdir())
    assert "report.txt" in files
    assert "report-1.txt" in files
    assert (dest / "report.txt").read_bytes() == b"existing"
    assert (dest / "report-1.txt").read_bytes() == b"new"
    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_size_cap_declared(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.tools.vault_download.max_size_bytes = 100
    tools = VaultTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 50,
            headers={
                "content-type": "application/octet-stream",
                "content-length": "1000",
            },
        )

    _install_mock(tools, handler)
    result = await tools.vault_download(
        url="https://example.com/huge",
        conv_key=CONV_KEY,
    )
    text = result["content"][0]["text"]
    assert "refused" in text
    assert "Content-Length" in text
    dest = cfg.tools.vault_download.resolved_destination
    if dest.exists():
        assert list(dest.iterdir()) == []
    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_size_cap_streaming(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.tools.vault_download.max_size_bytes = 20
    tools = VaultTools(config=cfg)

    async def body_gen():
        for _ in range(100):
            yield b"x"

    def handler(req: httpx.Request) -> httpx.Response:
        # Async-generator content; httpx does not compute Content-Length
        # in advance for this, so the pre-flight check skips and the
        # streaming abort path takes over.
        return httpx.Response(
            200,
            content=body_gen(),
            headers={"content-type": "application/octet-stream"},
        )

    _install_mock(tools, handler)
    result = await tools.vault_download(
        url="https://example.com/huge2",
        conv_key=CONV_KEY,
    )
    text = result["content"][0]["text"]
    assert "aborted" in text
    assert "max_size_bytes 20" in text
    dest = cfg.tools.vault_download.resolved_destination
    if dest.exists():
        partials = [p for p in dest.iterdir() if p.suffix == ".partial"]
        assert not partials
        # Final file should also not be present (the abort cleaned up).
        files = list(dest.iterdir())
        assert files == []
    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_creates_destination(tmp_path: Path) -> None:
    nonexistent = tmp_path / "deep" / "nested" / "inbox"
    cfg = _make_config(tmp_path, destination=nonexistent)
    tools = VaultTools(config=cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x",
            headers={"content-type": "text/plain"},
        )

    _install_mock(tools, handler)
    assert not nonexistent.exists()
    await tools.vault_download(url="https://example.com/x.txt", conv_key=CONV_KEY)
    assert nonexistent.is_dir()
    assert any(nonexistent.iterdir())
    await tools.aclose()


@pytest.mark.asyncio
async def test_vault_download_rejects_bad_url(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = VaultTools(config=cfg)
    with pytest.raises(ValueError, match="http"):
        await tools.vault_download(url="ftp://example.com/x", conv_key=CONV_KEY)
    with pytest.raises(ValueError, match="url"):
        await tools.vault_download(url="", conv_key=CONV_KEY)


@pytest.mark.asyncio
async def test_vault_download_http_error(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = VaultTools(config=cfg)
    _install_mock(tools, lambda req: httpx.Response(404, content=b"nope"))
    result = await tools.vault_download(url="https://example.com/x", conv_key=CONV_KEY)
    text = result["content"][0]["text"]
    assert "HTTP 404" in text
    await tools.aclose()
