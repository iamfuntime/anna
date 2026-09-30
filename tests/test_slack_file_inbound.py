"""Slack inbound generic file intake (PDFs, docs, ...).

Regression for 2026-09-30: Seth attached PDFs to the DM and the model got
either the bare caption ("Here's the agreement") or an EMPTY turn, because
only voice and image ``files[]`` entries were handled. Every attached file
must now be described (id / name / mimetype) and document-type files are
downloaded into the Inbox with the local path passed along.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.transports.base import InboundEvent
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation

PDF_BYTES = b"%PDF-1.7\n fake agreement body"


def _make_adapter(
    tmp_path: Path, *, enabled: bool = True, max_bytes: int = 10_485_760
) -> SlackAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.files.inbound.enabled = enabled
    cfg.files.inbound.destination = str(tmp_path / "Inbox")
    cfg.files.inbound.max_file_size_bytes = max_bytes
    state_path = tmp_path / "anna_home" / "state" / "tp.jsonl"
    return SlackAdapter(
        config=cfg, thread_participation=ThreadParticipation(state_path=state_path)
    )


def _pdf_entry(file_id: str = "F0C5MQ6UW0J", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": file_id,
        "name": "Ostra ISO Agreement.pdf",
        "mimetype": "application/pdf",
        "filetype": "pdf",
        "size": len(PDF_BYTES),
        "url_private": f"https://files.slack.com/files-pri/T1-{file_id}/agreement.pdf",
    }
    entry.update(extra)
    return entry


def _event(files: list[dict[str, Any]], *, text: str = "") -> dict[str, Any]:
    return {
        "type": "message",
        "subtype": "file_share",
        "channel_type": "im",
        "channel": "D123",
        "user": "USP2QLB41",
        "ts": "1790770022.000100",
        "text": text,
        "files": files,
    }


def _stub_download(adapter: SlackAdapter, data: bytes = PDF_BYTES) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake(entry: dict[str, Any], *, max_bytes: int | None = None) -> bytes:
        calls.append({"entry": entry, "max_bytes": max_bytes})
        return data

    adapter._download_slack_file = fake  # type: ignore[method-assign]
    return calls


async def _capture(adapter: SlackAdapter, event: dict[str, Any]) -> list[InboundEvent]:
    got: list[InboundEvent] = []

    async def handler(ev: InboundEvent) -> None:
        got.append(ev)

    adapter.subscribe(handler)
    await adapter._handle_message_event(event, {})
    return got


@pytest.mark.asyncio
async def test_captioned_pdf_includes_metadata_and_local_path(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    calls = _stub_download(adapter)
    got = await _capture(adapter, _event([_pdf_entry()], text="Here's the agreement"))

    assert len(got) == 1
    text = got[0].text
    assert text.startswith("Here's the agreement\n")
    assert "id=F0C5MQ6UW0J" in text
    assert 'name="Ostra ISO Agreement.pdf"' in text
    assert "mimetype=application/pdf" in text
    saved = list((tmp_path / "Inbox").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == PDF_BYTES
    assert "F0C5MQ6UW0J" in saved[0].name and saved[0].name.endswith(".pdf")
    assert f"saved_to={saved[0]}" in text
    assert calls[0]["max_bytes"] == 10_485_760


@pytest.mark.asyncio
async def test_file_only_message_is_delivered_non_empty(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    _stub_download(adapter)
    entry = _pdf_entry("F0C5GMRLRPX", name="Equity Incentive Plan.pdf")
    got = await _capture(adapter, _event([entry], text=""))

    assert len(got) == 1
    assert got[0].text.strip() != ""
    assert got[0].text.startswith("[attached file: id=F0C5GMRLRPX")
    assert 'name="Equity Incentive Plan.pdf"' in got[0].text


@pytest.mark.asyncio
async def test_multiple_files_each_listed(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    _stub_download(adapter)
    files = [
        _pdf_entry("FA1", name="a.pdf"),
        _pdf_entry("FB2", name="b.docx", mimetype="application/msword"),
    ]
    got = await _capture(adapter, _event(files, text="two docs"))
    text = got[0].text
    assert "id=FA1" in text and "id=FB2" in text
    assert "mimetype=application/msword" in text
    assert len(list((tmp_path / "Inbox").iterdir())) == 2


@pytest.mark.asyncio
async def test_oversize_file_listed_not_downloaded(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, max_bytes=10)
    calls = _stub_download(adapter)
    got = await _capture(adapter, _event([_pdf_entry(size=11_000_000)]))
    assert calls == []
    assert "id=F0C5MQ6UW0J" in got[0].text
    assert "not downloaded: larger than 10 bytes" in got[0].text


@pytest.mark.asyncio
async def test_post_download_size_cap(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, max_bytes=5)
    _stub_download(adapter)
    entry = _pdf_entry()
    entry.pop("size")
    got = await _capture(adapter, _event([entry]))
    assert "not downloaded" in got[0].text
    assert not (tmp_path / "Inbox").exists() or not list((tmp_path / "Inbox").iterdir())


@pytest.mark.asyncio
async def test_download_failure_still_lists_file(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)

    async def boom(entry: dict[str, Any], *, max_bytes: int | None = None) -> bytes:
        raise RuntimeError("403 Forbidden")

    adapter._download_slack_file = boom  # type: ignore[method-assign]
    got = await _capture(adapter, _event([_pdf_entry()], text=""))
    assert len(got) == 1
    assert "id=F0C5MQ6UW0J" in got[0].text
    assert "download failed: 403 Forbidden" in got[0].text


@pytest.mark.asyncio
async def test_download_disabled_lists_metadata_only(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, enabled=False)
    calls = _stub_download(adapter)
    got = await _capture(adapter, _event([_pdf_entry()], text="hi"))
    assert calls == []
    assert "id=F0C5MQ6UW0J" in got[0].text
    assert "file download is off" in got[0].text


@pytest.mark.asyncio
async def test_sparse_entry_hydrated_via_files_info(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    _stub_download(adapter)

    class _Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def files_info(self, *, file: str) -> dict[str, Any]:
            self.calls.append(file)
            return {"ok": True, "file": _pdf_entry(file)}

    client = _Client()
    adapter._client = client
    sparse = {"id": "F0C5MQ6UW0J", "file_access": "check_file_info"}
    got = await _capture(adapter, _event([sparse]))
    assert client.calls == ["F0C5MQ6UW0J"]
    assert 'name="Ostra ISO Agreement.pdf"' in got[0].text
    assert "saved_to=" in got[0].text


@pytest.mark.asyncio
async def test_sparse_entry_without_client_still_listed(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)

    async def boom(entry: dict[str, Any], *, max_bytes: int | None = None) -> bytes:
        raise RuntimeError("slack file has no download URL")

    adapter._download_slack_file = boom  # type: ignore[method-assign]
    got = await _capture(adapter, _event([{"id": "FX"}]))
    assert "id=FX" in got[0].text and "mimetype=unknown" in got[0].text


@pytest.mark.asyncio
async def test_inline_image_left_to_image_path(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    calls = _stub_download(adapter, data=b"\x89PNG")
    img = {
        "id": "FIMG",
        "name": "shot.png",
        "mimetype": "image/png",
        "url_private_download": "https://files.slack.com/FIMG/download",
    }
    got = await _capture(adapter, _event([img]))
    assert len(got[0].images) == 1
    assert got[0].text == "[image]"
    # One download for the inline image path; nothing written to the Inbox.
    assert len(calls) == 1
    assert not (tmp_path / "Inbox").exists()


@pytest.mark.asyncio
async def test_unsupported_image_saved_and_listed(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    _stub_download(adapter, data=b"heicbytes")
    heic = _pdf_entry("FHEIC", name="photo.heic", mimetype="image/heic", size=9)
    got = await _capture(adapter, _event([heic]))
    assert "unsupported image type: image/heic" in got[0].text
    assert "id=FHEIC" in got[0].text and "saved_to=" in got[0].text


@pytest.mark.asyncio
async def test_pdf_alongside_image_both_reach_model(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    _stub_download(adapter)
    img = {
        "id": "FIMG",
        "name": "shot.png",
        "mimetype": "image/png",
        "url_private_download": "https://files.slack.com/FIMG/download",
    }
    got = await _capture(adapter, _event([img, _pdf_entry()], text="see these"))
    assert len(got[0].images) == 1
    assert got[0].text.startswith("see these\n[attached file: id=F0C5MQ6UW0J")


@pytest.mark.asyncio
async def test_plain_text_message_unchanged(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    ev = _event([], text="just text")
    ev.pop("files")
    ev.pop("subtype")
    got = await _capture(adapter, ev)
    assert got[0].text == "just text"


def test_safe_filename_strips_traversal() -> None:
    assert SlackAdapter._safe_filename("../../etc/passwd") == "passwd"
    assert SlackAdapter._safe_filename("a b/c?.pdf") == "c_.pdf"
    assert SlackAdapter._safe_filename("") == "file"
