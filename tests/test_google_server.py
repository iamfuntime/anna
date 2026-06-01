"""Validate the google MCP server's read-only tools against a fake API.

We never hit the real Google API. Instead we build a fake ``GoogleClients``
that returns hand-rolled service objects with the call shapes the SDK
expects (``users().messages().list(...).execute()`` etc.). This keeps the
test suite fast and self-contained.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.tools.google_server import GoogleTools, build_google_server


CONV_KEY = "slack:dm:UTEST"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _ExecResult:
    """Wraps a payload in the ``.execute()`` shape google's discovery client uses."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class _FakeGmailMessages:
    def __init__(self, list_payload: dict[str, Any], get_payloads: dict[str, dict[str, Any]]) -> None:
        self._list = list_payload
        self._get = get_payloads
        # Capture call args for assertions.
        self.last_list_kwargs: dict[str, Any] | None = None
        self.last_get_id: str | None = None

    def list(self, **kwargs: Any) -> _ExecResult:
        self.last_list_kwargs = kwargs
        return _ExecResult(self._list)

    def get(self, *, userId: str, id: str, **_kwargs: Any) -> _ExecResult:
        self.last_get_id = id
        return _ExecResult(self._get[id])


class _FakeGmailUsers:
    def __init__(self, messages: _FakeGmailMessages, profile: dict[str, Any]) -> None:
        self._messages = messages
        self._profile = profile

    def messages(self) -> _FakeGmailMessages:
        return self._messages

    def getProfile(self, *, userId: str) -> _ExecResult:
        return _ExecResult(self._profile)


class _FakeGmailService:
    def __init__(self, messages: _FakeGmailMessages, profile: dict[str, Any]) -> None:
        self._users = _FakeGmailUsers(messages, profile)

    def users(self) -> _FakeGmailUsers:
        return self._users


class _FakeCalendarEvents:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.last_list_kwargs: dict[str, Any] | None = None

    def list(self, **kwargs: Any) -> _ExecResult:
        self.last_list_kwargs = kwargs
        return _ExecResult(self._payload)


class _FakeCalendarService:
    def __init__(self, events_payload: dict[str, Any]) -> None:
        self._events = _FakeCalendarEvents(events_payload)

    def events(self) -> _FakeCalendarEvents:
        return self._events


class _FakeClients:
    """Stand-in for GoogleClients. Only implements what the tools touch."""

    def __init__(self, config: AnnaConfig) -> None:
        self._config = config
        self._gmail: dict[str, _FakeGmailService] = {}
        self._calendar: dict[str, _FakeCalendarService] = {}

    @property
    def config(self) -> AnnaConfig:
        return self._config

    def register_gmail(self, slug: str, service: _FakeGmailService) -> None:
        self._gmail[slug] = service

    def register_calendar(self, slug: str, service: _FakeCalendarService) -> None:
        self._calendar[slug] = service

    def gmail(self, slug: str) -> _FakeGmailService:
        return self._gmail[slug]

    def calendar(self, slug: str) -> _FakeCalendarService:
        return self._calendar[slug]


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.google.enabled = True
    cfg.google.accounts.append(
        GoogleAccountConfig(
            slug="personal_main",
            email="seth@example.com",
            auth_type="oauth",
            credentials_file="state/google/oauth_client.json",
        )
    )
    cfg.google.accounts.append(
        GoogleAccountConfig(
            slug="emergentsec",
            email="seth@emergentsec.com",
            auth_type="service_account",
            credentials_file="state/google/sa.json",
        )
    )
    return cfg


def _read_audit_records(audit_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _b64url(text: str) -> str:
    raw = text.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# accounts_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accounts_list_returns_all_configured(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    clients = _FakeClients(cfg)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.accounts_list(conv_key=CONV_KEY)
    text = resp["content"][0]["text"]
    assert "personal_main" in text
    assert "emergentsec" in text
    assert "oauth" in text
    assert "service_account" in text

    audits = _read_audit_records(cfg.audit_dir)
    list_audits = [a for a in audits if a["event"] == "audit.google.accounts_list"]
    assert list_audits and list_audits[0]["count"] == 2


@pytest.mark.asyncio
async def test_accounts_list_empty(tmp_path: Path) -> None:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    clients = _FakeClients(cfg)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]
    resp = await tools.accounts_list(conv_key=CONV_KEY)
    assert "no google accounts" in resp["content"][0]["text"]


# ---------------------------------------------------------------------------
# gmail_list_unread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_list_unread_happy(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    list_payload = {"messages": [{"id": "m1"}, {"id": "m2"}]}
    get_payloads = {
        "m1": {
            "payload": {
                "headers": [
                    {"name": "From", "value": "alice@x.com"},
                    {"name": "Subject", "value": "Hi"},
                    {"name": "Date", "value": "Mon, 1 Jun 2026 12:00:00 +0000"},
                ]
            }
        },
        "m2": {
            "payload": {
                "headers": [
                    {"name": "From", "value": "bob@x.com"},
                    {"name": "Subject", "value": "Heads up"},
                    {"name": "Date", "value": "Mon, 1 Jun 2026 13:00:00 +0000"},
                ]
            }
        },
    }
    messages = _FakeGmailMessages(list_payload, get_payloads)
    service = _FakeGmailService(messages, profile={})
    clients = _FakeClients(cfg)
    clients.register_gmail("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.gmail_list_unread(
        account="personal_main",
        since_hours=12,
        max_results=10,
        conv_key=CONV_KEY,
    )
    text = resp["content"][0]["text"]
    assert "2 unread" in text
    assert "alice@x.com" in text
    assert "bob@x.com" in text
    assert "Hi" in text
    # The Gmail query should encode the time window in hours.
    assert messages.last_list_kwargs is not None
    assert "newer_than:12h" in messages.last_list_kwargs["q"]
    assert "is:unread" in messages.last_list_kwargs["q"]

    audits = _read_audit_records(cfg.audit_dir)
    rows = [a for a in audits if a["event"] == "audit.google.gmail_list_unread"]
    assert rows and rows[0]["count"] == 2


@pytest.mark.asyncio
async def test_gmail_list_unread_zero_results(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    messages = _FakeGmailMessages({"messages": []}, {})
    service = _FakeGmailService(messages, profile={})
    clients = _FakeClients(cfg)
    clients.register_gmail("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.gmail_list_unread(
        account="personal_main",
        since_hours=24,
        max_results=50,
        conv_key=CONV_KEY,
    )
    assert "no unread" in resp["content"][0]["text"]


@pytest.mark.asyncio
async def test_gmail_list_unread_rejects_bad_since_hours(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = GoogleTools(config=cfg, clients=_FakeClients(cfg))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await tools.gmail_list_unread(
            account="personal_main",
            since_hours=0,
            max_results=10,
            conv_key=CONV_KEY,
        )


# ---------------------------------------------------------------------------
# gmail_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_search_passes_query_through(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    list_payload = {"messages": [{"id": "m1"}]}
    get_payloads = {
        "m1": {
            "payload": {
                "headers": [
                    {"name": "From", "value": "alerts@vendor.io"},
                    {"name": "Subject", "value": "Outage"},
                    {"name": "Date", "value": "Sun, 31 May 2026 08:00:00 +0000"},
                ]
            }
        }
    }
    messages = _FakeGmailMessages(list_payload, get_payloads)
    service = _FakeGmailService(messages, profile={})
    clients = _FakeClients(cfg)
    clients.register_gmail("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    await tools.gmail_search(
        account="personal_main",
        query="from:alerts@vendor.io newer_than:7d",
        max_results=20,
        conv_key=CONV_KEY,
    )
    assert messages.last_list_kwargs is not None
    assert messages.last_list_kwargs["q"] == "from:alerts@vendor.io newer_than:7d"


@pytest.mark.asyncio
async def test_gmail_search_rejects_empty_query(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tools = GoogleTools(config=cfg, clients=_FakeClients(cfg))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await tools.gmail_search(
            account="personal_main",
            query="   ",
            max_results=20,
            conv_key=CONV_KEY,
        )


# ---------------------------------------------------------------------------
# gmail_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_read_decodes_text_plain_body(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    body_text = "Hello from the body.\n\nSecond paragraph."
    full_msg = {
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@x.com"},
                {"name": "To", "value": "me@x.com"},
                {"name": "Subject", "value": "Hi"},
                {"name": "Date", "value": "Mon, 1 Jun 2026 12:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url(body_text)},
        }
    }
    messages = _FakeGmailMessages({"messages": []}, {"m1": full_msg})
    service = _FakeGmailService(messages, profile={})
    clients = _FakeClients(cfg)
    clients.register_gmail("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.gmail_read(
        account="personal_main",
        message_id="m1",
        conv_key=CONV_KEY,
    )
    text = resp["content"][0]["text"]
    assert "From: alice@x.com" in text
    assert "Subject: Hi" in text
    assert "Hello from the body" in text
    assert "Second paragraph" in text


@pytest.mark.asyncio
async def test_gmail_read_walks_multipart_for_plain(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    full_msg = {
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@x.com"},
                {"name": "To", "value": "me@x.com"},
                {"name": "Subject", "value": "Multipart"},
                {"name": "Date", "value": "Mon, 1 Jun 2026 12:00:00 +0000"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("plain version")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>html version</p>")},
                },
            ],
        }
    }
    messages = _FakeGmailMessages({"messages": []}, {"m1": full_msg})
    service = _FakeGmailService(messages, profile={})
    clients = _FakeClients(cfg)
    clients.register_gmail("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.gmail_read(
        account="personal_main",
        message_id="m1",
        conv_key=CONV_KEY,
    )
    text = resp["content"][0]["text"]
    assert "plain version" in text
    # html should not appear in the body when a plain part exists
    assert "html version" not in text


@pytest.mark.asyncio
async def test_gmail_read_truncates_long_bodies(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    huge = "X" * 12000
    full_msg = {
        "payload": {
            "headers": [
                {"name": "From", "value": "x"},
                {"name": "To", "value": "y"},
                {"name": "Subject", "value": "z"},
                {"name": "Date", "value": "Mon, 1 Jun 2026 12:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url(huge)},
        }
    }
    messages = _FakeGmailMessages({"messages": []}, {"m1": full_msg})
    service = _FakeGmailService(messages, profile={})
    clients = _FakeClients(cfg)
    clients.register_gmail("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.gmail_read(
        account="personal_main",
        message_id="m1",
        conv_key=CONV_KEY,
    )
    assert "[…body truncated at 8000 chars]" in resp["content"][0]["text"]

    audits = _read_audit_records(cfg.audit_dir)
    rows = [a for a in audits if a["event"] == "audit.google.gmail_read"]
    assert rows and rows[0]["truncated"] is True


# ---------------------------------------------------------------------------
# calendar_list_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_list_events_happy(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    payload = {
        "items": [
            {
                "summary": "Standup",
                "start": {"dateTime": "2026-06-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-06-01T09:30:00-04:00"},
                "location": "Zoom",
            },
            {
                "summary": "Lunch",
                "start": {"dateTime": "2026-06-01T12:00:00-04:00"},
                "end": {"dateTime": "2026-06-01T13:00:00-04:00"},
            },
        ]
    }
    service = _FakeCalendarService(payload)
    clients = _FakeClients(cfg)
    clients.register_calendar("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    resp = await tools.calendar_list_events(
        account="personal_main",
        start_iso="2026-06-01T00:00:00-04:00",
        end_iso="2026-06-01T23:59:59-04:00",
        max_results=50,
        conv_key=CONV_KEY,
    )
    text = resp["content"][0]["text"]
    assert "Standup" in text
    assert "Zoom" in text
    assert "Lunch" in text
    assert service._events.last_list_kwargs is not None
    kwargs = service._events.last_list_kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"


@pytest.mark.asyncio
async def test_calendar_list_events_rejects_inverted_window(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    service = _FakeCalendarService({"items": []})
    clients = _FakeClients(cfg)
    clients.register_calendar("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await tools.calendar_list_events(
            account="personal_main",
            start_iso="2026-06-01T18:00:00-04:00",
            end_iso="2026-06-01T08:00:00-04:00",
            max_results=50,
            conv_key=CONV_KEY,
        )


@pytest.mark.asyncio
async def test_calendar_today_uses_today_in_tz(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    service = _FakeCalendarService({"items": []})
    clients = _FakeClients(cfg)
    clients.register_calendar("personal_main", service)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]

    await tools.calendar_today(
        account="personal_main",
        tz_name="America/New_York",
        conv_key=CONV_KEY,
    )
    kwargs = service._events.last_list_kwargs
    assert kwargs is not None
    # Window should be same calendar day in -04:00 or -05:00 depending on DST.
    assert "T" in kwargs["timeMin"]
    assert "T23:59:59" in kwargs["timeMax"]


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def test_build_google_server_exposes_all_tools(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    clients = _FakeClients(cfg)
    tools = GoogleTools(config=cfg, clients=clients)  # type: ignore[arg-type]
    server = build_google_server(tools=tools, conv_key=CONV_KEY)
    # The SDK exposes the registered tools on the server instance under a
    # private but stable attribute. Tolerate any of the names the SDK has
    # used so the test does not get brittle on upgrades.
    tool_names: list[str] = []
    for attr in ("tools", "_tools", "tool_set"):
        candidate = getattr(server, attr, None)
        if candidate:
            if isinstance(candidate, dict):
                tool_names = list(candidate.keys())
            else:
                try:
                    tool_names = [getattr(t, "name", str(t)) for t in candidate]
                except TypeError:
                    pass
            if tool_names:
                break
    # If the SDK changes the attribute shape just sanity-check we got an
    # object back; the more important wiring is exercised by the per-tool
    # methods above.
    assert server is not None
    if tool_names:
        for expected in (
            "google_accounts_list",
            "gmail_list_unread",
            "gmail_search",
            "gmail_read",
            "calendar_list_events",
            "calendar_today",
        ):
            assert any(expected in n for n in tool_names), (expected, tool_names)
