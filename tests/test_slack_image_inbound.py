"""Slack inbound image detection, download, and graceful-failure markers
(Phase 2.6), plus the download-hardening fast-follow.

A dragged-in image on Slack arrives as an ``image/*`` ``files[]`` entry on
the message event. :meth:`SlackAdapter._detect_image_files` classifies each
entry (ok / unsupported / overflow); :meth:`_handle_image_inbound` downloads
the accepted ones, enforces the size caps, and appends an operator-facing
marker for every skip or failure so images are never silently dropped.

These mirror :mod:`tests.test_voice_slack_inbound`. They also pin the
download-hardening added in :meth:`_download_slack_file`: non-Slack hosts are
refused before the bot token is attached, redirects are not silently
accepted, and an over-cap ``Content-Length`` aborts the download.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from anna.config import AnnaConfig
from anna.transports.base import ImageAttachment
from anna.transports.slack import SlackAdapter
from anna.transports.slack_thread_state import ThreadParticipation


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeVoice:
    """Minimal VoiceProcessor stand-in for the voice-precedence test."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self.transcribe_calls: list[dict[str, Any]] = []
        self.mark_calls: list[str] = []
        self.clear_calls: list[str] = []

    async def transcribe_inbound(self, **kwargs: Any) -> str:
        self.transcribe_calls.append(kwargs)
        return self._transcript

    def mark_voice_inbound(self, *, conv_key: str) -> None:
        self.mark_calls.append(conv_key)

    def clear_voice_inbound(self, *, conv_key: str) -> None:
        self.clear_calls.append(conv_key)


def _make_adapter(
    tmp_path: Path,
    *,
    voice: _FakeVoice | None = None,
    images_enabled: bool = True,
    voice_inbound_enabled: bool = True,
    max_images: int = 8,
    max_image_size_bytes: int = 5_242_880,
    max_total_bytes: int = 20_971_520,
) -> SlackAdapter:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.images.inbound.enabled = images_enabled
    cfg.images.inbound.max_images = max_images
    cfg.images.inbound.max_image_size_bytes = max_image_size_bytes
    cfg.images.inbound.max_total_bytes = max_total_bytes
    cfg.voice.inbound.enabled = voice_inbound_enabled
    state_path = tmp_path / "anna_home" / "state" / "slack_thread_participation.jsonl"
    tp = ThreadParticipation(state_path=state_path)
    return SlackAdapter(config=cfg, thread_participation=tp, voice=voice)


def _img_entry(
    file_id: str,
    *,
    mime: str = "image/png",
    size: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": file_id,
        "mimetype": mime,
        "filetype": mime.split("/", 1)[1],
        "url_private_download": f"https://files.slack.com/{file_id}/download",
    }
    if size is not None:
        entry["size"] = size
    return entry


def _image_event(files: list[dict[str, Any]], *, text: str = "") -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "user": "U_OP",
        "ts": "1716832500.000300",
        "text": text,
        "files": files,
    }


# ---------------------------------------------------------------------------
# _detect_image_files
# ---------------------------------------------------------------------------


def test_detect_single_supported_image(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    records = adapter._detect_image_files(_image_event([_img_entry("F1")]))
    assert len(records) == 1
    assert records[0]["status"] == "ok"
    assert records[0]["mime"] == "image/png"


def test_detect_multiple_supported_images(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    event = _image_event(
        [_img_entry("F1"), _img_entry("F2", mime="image/jpeg")]
    )
    records = adapter._detect_image_files(event)
    assert [r["status"] for r in records] == ["ok", "ok"]
    assert [r["mime"] for r in records] == ["image/png", "image/jpeg"]


def test_detect_unsupported_subtype(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    event = _image_event([_img_entry("F1", mime="image/svg+xml")])
    records = adapter._detect_image_files(event)
    assert len(records) == 1
    assert records[0]["status"] == "unsupported"


def test_detect_over_count_marks_overflow(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, max_images=2)
    event = _image_event(
        [_img_entry("F1"), _img_entry("F2"), _img_entry("F3")]
    )
    records = adapter._detect_image_files(event)
    assert [r["status"] for r in records] == ["ok", "ok", "overflow"]


def test_detect_ignores_size_at_detection(tmp_path: Path) -> None:
    """Detection does not enforce the size cap — that is _handle's job. A
    wildly oversize but supported image is still classified ``ok`` here."""
    adapter = _make_adapter(tmp_path, max_image_size_bytes=10)
    event = _image_event([_img_entry("F1", size=10_000)])
    records = adapter._detect_image_files(event)
    assert records[0]["status"] == "ok"


def test_detect_no_files_returns_empty(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    assert adapter._detect_image_files({"type": "message"}) == []


# ---------------------------------------------------------------------------
# _handle_image_inbound — graceful markers
# ---------------------------------------------------------------------------


async def test_handle_disabled_returns_marker(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, images_enabled=False)
    records = [{"entry": _img_entry("F1"), "mime": "image/png", "status": "ok"}]
    text, images = await adapter._handle_image_inbound(
        event=_image_event([_img_entry("F1")]),
        image_files=records,
        conv_key="slack:dm:U_OP",
        caption="",
    )
    assert images == []
    assert text == "[image received — image understanding is off]"


async def test_handle_unsupported_marker(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    records = [
        {
            "entry": _img_entry("F1", mime="image/svg+xml"),
            "mime": "image/svg+xml",
            "status": "unsupported",
        }
    ]
    text, images = await adapter._handle_image_inbound(
        event=_image_event([]),
        image_files=records,
        conv_key="slack:dm:U_OP",
        caption="check this",
    )
    assert images == []
    assert "check this" in text
    assert "[unsupported image type: image/svg+xml" in text


async def test_handle_oversize_pre_download_marker(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, max_image_size_bytes=10)
    entry = _img_entry("F1", size=100)
    records = [{"entry": entry, "mime": "image/png", "status": "ok"}]
    text, images = await adapter._handle_image_inbound(
        event=_image_event([entry]),
        image_files=records,
        conv_key="slack:dm:U_OP",
        caption="",
    )
    assert images == []
    assert "[image too large (100 bytes) — skipped]" in text


async def test_handle_download_failure_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _make_adapter(tmp_path)

    async def _boom(entry: dict[str, Any], **kwargs: Any) -> bytes:
        raise RuntimeError("cdn exploded")

    monkeypatch.setattr(adapter, "_download_slack_file", _boom)

    entry = _img_entry("F1")
    records = [{"entry": entry, "mime": "image/png", "status": "ok"}]
    text, images = await adapter._handle_image_inbound(
        event=_image_event([entry]),
        image_files=records,
        conv_key="slack:dm:U_OP",
        caption="",
    )
    assert images == []
    assert "[couldn't download an image — please try again]" in text


async def test_handle_success_builds_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _make_adapter(tmp_path)

    async def _ok(entry: dict[str, Any], **kwargs: Any) -> bytes:
        return b"\x89PNG-bytes"

    monkeypatch.setattr(adapter, "_download_slack_file", _ok)

    entry = _img_entry("F1")
    records = [{"entry": entry, "mime": "image/png", "status": "ok"}]
    text, images = await adapter._handle_image_inbound(
        event=_image_event([entry]),
        image_files=records,
        conv_key="slack:dm:U_OP",
        caption="",
    )
    assert len(images) == 1
    assert images[0] == ImageAttachment(media_type="image/png", data=b"\x89PNG-bytes")
    # Caption-less turn with an accepted image gets the "[image]" placeholder.
    assert text == "[image]"


async def test_handle_post_download_oversize_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack's ``size`` can be absent/stale; the downloaded length is the
    authoritative guard and must still produce the oversize marker."""
    adapter = _make_adapter(tmp_path, max_image_size_bytes=4)

    async def _big(entry: dict[str, Any], **kwargs: Any) -> bytes:
        return b"way too many bytes"

    monkeypatch.setattr(adapter, "_download_slack_file", _big)

    entry = _img_entry("F1")  # no size field
    records = [{"entry": entry, "mime": "image/png", "status": "ok"}]
    text, images = await adapter._handle_image_inbound(
        event=_image_event([entry]),
        image_files=records,
        conv_key="slack:dm:U_OP",
        caption="",
    )
    assert images == []
    assert "too large" in text


# ---------------------------------------------------------------------------
# Voice precedence: a voice note suppresses image detection entirely
# ---------------------------------------------------------------------------


def _patch_voice_download(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}
        content = b"\x00\x01"

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_voice_present_skips_image_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_voice_download(monkeypatch)
    voice = _FakeVoice("a spoken message")
    adapter = _make_adapter(tmp_path, voice=voice)

    # Event carries BOTH an audio file and an image file. Voice is exclusive:
    # the image must never be downloaded and inbound.images must be empty.
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "user": "U_OP",
        "ts": "1716832500.000300",
        "text": "",
        "files": [
            {
                "id": "FAUDIO",
                "mode": "audio",
                "mimetype": "audio/webm",
                "filetype": "webm",
                "url_private_download": "https://files.slack.com/FAUDIO/download",
            },
            _img_entry("FIMG"),
        ],
    }

    inbound = await adapter._to_inbound_event(event)

    assert inbound.images == []
    assert inbound.text == "[voice transcript]: a spoken message"
    assert voice.mark_calls == [inbound.conversation_key]


# ---------------------------------------------------------------------------
# _download_slack_file — hardening (Workstream B)
# ---------------------------------------------------------------------------


class _HardenResp:
    def __init__(self, *, status: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=None, response=None  # type: ignore[arg-type]
            )


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content: bytes = b"abc",
    record: list[dict[str, Any]] | None = None,
) -> None:
    resp = _HardenResp(status=status, headers=headers or {}, content=content)

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _HardenResp:
            if record is not None:
                record.append({"url": url, "headers": headers})
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_download_refuses_non_slack_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _make_adapter(tmp_path)
    record: list[dict[str, Any]] = []
    _patch_httpx(monkeypatch, record=record)

    entry = {"url_private_download": "https://evil.example.com/steal"}
    with pytest.raises(RuntimeError, match="non-Slack host"):
        await adapter._download_slack_file(entry)
    # The token must never have been sent — no request was made at all.
    assert record == []


async def test_download_rejects_redirect_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _make_adapter(tmp_path)
    _patch_httpx(monkeypatch, status=302, headers={"Location": "https://x"})

    entry = _img_entry("F1")
    with pytest.raises(RuntimeError, match="unexpected status 302"):
        await adapter._download_slack_file(entry)


async def test_download_aborts_on_oversize_content_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _make_adapter(tmp_path)
    _patch_httpx(monkeypatch, headers={"Content-Length": "999"}, content=b"x" * 999)

    entry = _img_entry("F1")
    with pytest.raises(RuntimeError, match="exceeds cap"):
        await adapter._download_slack_file(entry, max_bytes=100)


async def test_download_success_sends_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    adapter = _make_adapter(tmp_path)
    record: list[dict[str, Any]] = []
    _patch_httpx(
        monkeypatch,
        headers={"Content-Length": "3"},
        content=b"abc",
        record=record,
    )

    entry = _img_entry("F1")
    data = await adapter._download_slack_file(entry, max_bytes=100)
    assert data == b"abc"
    assert record[0]["headers"]["Authorization"] == "Bearer xoxb-test"


async def test_download_voice_path_unaffected_by_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The voice path passes no ``max_bytes`` — a large Content-Length must
    NOT abort the voice download (behavior identical to before)."""
    adapter = _make_adapter(tmp_path)
    _patch_httpx(
        monkeypatch,
        headers={"Content-Length": "9999999"},
        content=b"audio-bytes",
    )

    entry = _img_entry("FAUDIO")  # files.slack.com host
    data = await adapter._download_slack_file(entry)
    assert data == b"audio-bytes"
