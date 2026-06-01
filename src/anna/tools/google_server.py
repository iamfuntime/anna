"""Read-only Google MCP server.

Mounted alongside ``anna_self_edit`` by each conversation worker. Exposes
Gmail and Calendar tools across every account configured in
``anna.yaml -> google.accounts``. Every tool call:

* Accepts an ``account`` slug as its first argument.
* Emits an audit event tagged with the slug and the calling
  conversation key.
* Returns a structured MCP text response (the SDK convention).

Read-only by design. Write tools (drafts, sends, calendar mutations) are
scoped for a later phase and will live in a parallel module so the
read-only surface stays unambiguous.
"""

from __future__ import annotations

import base64
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import create_sdk_mcp_server, tool

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.tools.google_auth import GoogleAuthError
from anna.tools.google_clients import GoogleClients

if TYPE_CHECKING:
    pass


GOOGLE_TOOL_NAMES: tuple[str, ...] = (
    "google_accounts_list",
    "gmail_list_unread",
    "gmail_search",
    "gmail_read",
    "calendar_list_events",
    "calendar_today",
)


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _format_header(headers: list[dict[str, str]], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _decode_body_part(part: dict[str, Any]) -> str:
    """Pull the text body out of a Gmail message part.

    Walks the MIME tree depth-first, preferring text/plain over text/html.
    Returns the empty string if no decodable text part is found.
    """
    mime_type = part.get("mimeType", "")
    body = part.get("body", {})
    data = body.get("data")
    parts = part.get("parts", [])

    if mime_type == "text/plain" and data:
        return _b64url_decode(data)

    # Recurse into multipart parts; collect the first text/plain found,
    # falling back to text/html if no plain part exists.
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for sub in parts:
        sub_mime = sub.get("mimeType", "")
        if sub_mime.startswith("multipart/"):
            nested = _decode_body_part(sub)
            if nested:
                plain_chunks.append(nested)
        elif sub_mime == "text/plain" and sub.get("body", {}).get("data"):
            plain_chunks.append(_b64url_decode(sub["body"]["data"]))
        elif sub_mime == "text/html" and sub.get("body", {}).get("data"):
            html_chunks.append(_b64url_decode(sub["body"]["data"]))

    if plain_chunks:
        return "\n\n".join(plain_chunks)
    if mime_type == "text/html" and data:
        return _b64url_decode(data)
    if html_chunks:
        return "\n\n".join(html_chunks)
    return ""


def _b64url_decode(data: str) -> str:
    """Gmail returns body data as base64url; decode and strip BOMs."""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except Exception:
        return ""
    try:
        return raw.decode("utf-8", errors="replace").lstrip("﻿")
    except Exception:
        return ""


class GoogleTools:
    """Per-worker tool bundle. Holds the shared GoogleClients + config."""

    def __init__(
        self,
        *,
        config: AnnaConfig,
        clients: GoogleClients,
    ) -> None:
        self._config = config
        self._clients = clients
        self._log = get_logger("anna.tools.google")

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
    # Accounts
    # ------------------------------------------------------------------

    async def accounts_list(self, *, conv_key: str) -> dict[str, Any]:
        accts = self._config.google.accounts
        if not accts:
            return _text_response("(no google accounts configured)")
        lines = [
            f"- {a.slug}: {a.email} ({a.auth_type})"
            for a in accts
        ]
        self._audit(
            "audit.google.accounts_list",
            conv_key=conv_key,
            count=len(accts),
        )
        return _text_response("\n".join(lines))

    # ------------------------------------------------------------------
    # Gmail
    # ------------------------------------------------------------------

    async def gmail_list_unread(
        self,
        *,
        account: str,
        since_hours: int = 24,
        max_results: int = 50,
        conv_key: str,
    ) -> dict[str, Any]:
        """List unread mail in the inbox within the trailing window.

        Returns one line per message with id, from, subject, and a
        timestamp. Useful for morning-brief triage.
        """
        if since_hours <= 0:
            raise ValueError("since_hours must be > 0")
        if max_results <= 0 or max_results > 500:
            raise ValueError("max_results must be between 1 and 500")

        try:
            service = self._clients.gmail(account)
        except GoogleAuthError as exc:
            return _text_response(f"error: {exc}")

        # Gmail's `q` parameter supports `newer_than:Nd|h|m`. Use hours
        # to keep the operator's "last N hours" mental model exact.
        query = f"is:unread in:inbox newer_than:{since_hours}h"
        try:
            resp = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()
        except Exception as exc:
            self._audit(
                "audit.google.gmail_list_unread.failed",
                conv_key=conv_key,
                level="WARNING",
                account=account,
                error=str(exc),
            )
            return _text_response(f"error fetching unread for {account}: {exc}")

        message_ids = [m["id"] for m in resp.get("messages", [])]
        if not message_ids:
            self._audit(
                "audit.google.gmail_list_unread",
                conv_key=conv_key,
                account=account,
                count=0,
                since_hours=since_hours,
            )
            return _text_response(
                f"(no unread messages in {account} in the last {since_hours}h)"
            )

        # Pull metadata for each message in one batch-style sequential
        # fetch. The Gmail API doesn't support metadata-only batch in
        # a clean way without batchHttpRequest plumbing, so we do N gets
        # with `format=metadata`. For max_results=50 this is well under
        # the per-second quota.
        lines: list[str] = []
        for mid in message_ids:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
            except Exception as exc:
                lines.append(f"- {mid}: (metadata fetch failed: {exc})")
                continue
            headers = msg.get("payload", {}).get("headers", [])
            sender = _format_header(headers, "From") or "(no sender)"
            subject = _format_header(headers, "Subject") or "(no subject)"
            date = _format_header(headers, "Date") or ""
            lines.append(f"- {mid} | {date} | {sender} | {subject}")

        self._audit(
            "audit.google.gmail_list_unread",
            conv_key=conv_key,
            account=account,
            count=len(message_ids),
            since_hours=since_hours,
        )
        return _text_response(
            f"{len(message_ids)} unread in {account} (last {since_hours}h):\n"
            + "\n".join(lines)
        )

    async def gmail_search(
        self,
        *,
        account: str,
        query: str,
        max_results: int = 20,
        conv_key: str,
    ) -> dict[str, Any]:
        """Run an arbitrary Gmail search.

        Accepts the same query syntax as the web UI's search bar
        (``from:`` ``to:`` ``subject:`` ``label:`` ``has:attachment``
        ``before:`` ``after:`` etc). Returns message metadata, same shape
        as ``gmail_list_unread``.
        """
        if not query.strip():
            raise ValueError("query cannot be empty")
        if max_results <= 0 or max_results > 500:
            raise ValueError("max_results must be between 1 and 500")

        try:
            service = self._clients.gmail(account)
        except GoogleAuthError as exc:
            return _text_response(f"error: {exc}")

        try:
            resp = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()
        except Exception as exc:
            self._audit(
                "audit.google.gmail_search.failed",
                conv_key=conv_key,
                level="WARNING",
                account=account,
                query=query,
                error=str(exc),
            )
            return _text_response(f"error searching {account}: {exc}")

        message_ids = [m["id"] for m in resp.get("messages", [])]
        if not message_ids:
            self._audit(
                "audit.google.gmail_search",
                conv_key=conv_key,
                account=account,
                query=query,
                count=0,
            )
            return _text_response(f"(no results for {query!r} in {account})")

        lines: list[str] = []
        for mid in message_ids:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
            except Exception as exc:
                lines.append(f"- {mid}: (metadata fetch failed: {exc})")
                continue
            headers = msg.get("payload", {}).get("headers", [])
            sender = _format_header(headers, "From") or "(no sender)"
            subject = _format_header(headers, "Subject") or "(no subject)"
            date = _format_header(headers, "Date") or ""
            lines.append(f"- {mid} | {date} | {sender} | {subject}")

        self._audit(
            "audit.google.gmail_search",
            conv_key=conv_key,
            account=account,
            query=query,
            count=len(message_ids),
        )
        return _text_response(
            f"{len(message_ids)} results for {query!r} in {account}:\n"
            + "\n".join(lines)
        )

    async def gmail_read(
        self,
        *,
        account: str,
        message_id: str,
        conv_key: str,
    ) -> dict[str, Any]:
        """Fetch one message's headers + decoded text body.

        Returns a formatted block: From, To, Subject, Date, then a blank
        line, then the body (text/plain preferred, text/html fallback).
        Body is truncated at 8000 chars to keep context spend bounded.
        """
        if not message_id.strip():
            raise ValueError("message_id cannot be empty")

        try:
            service = self._clients.gmail(account)
        except GoogleAuthError as exc:
            return _text_response(f"error: {exc}")

        try:
            msg = service.users().messages().get(
                userId="me",
                id=message_id,
                format="full",
            ).execute()
        except Exception as exc:
            self._audit(
                "audit.google.gmail_read.failed",
                conv_key=conv_key,
                level="WARNING",
                account=account,
                message_id=message_id,
                error=str(exc),
            )
            return _text_response(
                f"error reading {message_id} in {account}: {exc}"
            )

        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        sender = _format_header(headers, "From") or "(no sender)"
        recipients = _format_header(headers, "To") or "(no to)"
        subject = _format_header(headers, "Subject") or "(no subject)"
        date = _format_header(headers, "Date") or ""

        body = _decode_body_part(payload)
        truncated = False
        if len(body) > 8000:
            body = body[:8000]
            truncated = True

        self._audit(
            "audit.google.gmail_read",
            conv_key=conv_key,
            account=account,
            message_id=message_id,
            body_chars=len(body),
            truncated=truncated,
        )
        suffix = "\n\n[…body truncated at 8000 chars]" if truncated else ""
        return _text_response(
            f"From: {sender}\n"
            f"To: {recipients}\n"
            f"Subject: {subject}\n"
            f"Date: {date}\n"
            f"\n{body}{suffix}"
        )

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------

    async def calendar_list_events(
        self,
        *,
        account: str,
        start_iso: str,
        end_iso: str,
        max_results: int = 50,
        conv_key: str,
    ) -> dict[str, Any]:
        """List events on the primary calendar within an explicit window.

        Both bounds are ISO-8601. Timezone-naive strings are treated as
        UTC. Returns one line per event with start/end, summary, and
        location if present.
        """
        if max_results <= 0 or max_results > 250:
            raise ValueError("max_results must be between 1 and 250")

        try:
            service = self._clients.calendar(account)
        except GoogleAuthError as exc:
            return _text_response(f"error: {exc}")

        try:
            start_dt = datetime.fromisoformat(start_iso)
            end_dt = datetime.fromisoformat(end_iso)
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 in start/end: {exc}") from exc
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        if end_dt <= start_dt:
            raise ValueError("end_iso must be after start_iso")

        try:
            resp = service.events().list(
                calendarId="primary",
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            ).execute()
        except Exception as exc:
            self._audit(
                "audit.google.calendar_list_events.failed",
                conv_key=conv_key,
                level="WARNING",
                account=account,
                error=str(exc),
            )
            return _text_response(f"error fetching calendar for {account}: {exc}")

        items = resp.get("items", [])
        if not items:
            self._audit(
                "audit.google.calendar_list_events",
                conv_key=conv_key,
                account=account,
                count=0,
            )
            return _text_response(
                f"(no events in {account} from {start_dt.isoformat()} to {end_dt.isoformat()})"
            )

        lines: list[str] = []
        for ev in items:
            start = ev.get("start", {})
            end = ev.get("end", {})
            start_when = start.get("dateTime") or start.get("date") or "?"
            end_when = end.get("dateTime") or end.get("date") or "?"
            summary = ev.get("summary") or "(no title)"
            location = ev.get("location")
            loc_suffix = f" @ {location}" if location else ""
            lines.append(f"- {start_when} -> {end_when} | {summary}{loc_suffix}")

        self._audit(
            "audit.google.calendar_list_events",
            conv_key=conv_key,
            account=account,
            count=len(items),
        )
        return _text_response(
            f"{len(items)} events in {account}:\n" + "\n".join(lines)
        )

    async def calendar_today(
        self,
        *,
        account: str,
        tz_name: str = "America/New_York",
        conv_key: str,
    ) -> dict[str, Any]:
        """Convenience: events from now through end-of-day in the given TZ."""
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            raise ValueError(f"invalid tz_name {tz_name!r}: {exc}") from exc
        now = datetime.now(tz)
        end_of_day = datetime.combine(now.date(), time(23, 59, 59), tzinfo=tz)
        return await self.calendar_list_events(
            account=account,
            start_iso=now.isoformat(),
            end_iso=end_of_day.isoformat(),
            conv_key=conv_key,
        )


def build_google_server(*, tools: GoogleTools, conv_key: str) -> Any:
    """Construct the per-worker Google MCP server."""

    @tool(
        "google_accounts_list",
        "List every Google account ANNA can access, with email and auth type. Call before any other gmail_* or calendar_* tool if you do not know the available slugs.",
        {},
    )
    async def _google_accounts_list(_args: dict[str, Any]) -> dict[str, Any]:
        return await tools.accounts_list(conv_key=conv_key)

    @tool(
        "gmail_list_unread",
        "List unread messages in the inbox over the last `since_hours` hours for one account. Returns id, sender, subject, date per message. account is the slug from google_accounts_list.",
        {
            "account": str,
            "since_hours": int,
            "max_results": int,
        },
    )
    async def _gmail_list_unread(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.gmail_list_unread(
            account=args["account"],
            since_hours=int(args.get("since_hours") or 24),
            max_results=int(args.get("max_results") or 50),
            conv_key=conv_key,
        )

    @tool(
        "gmail_search",
        "Search Gmail with the standard query syntax (from:, to:, subject:, label:, has:attachment, before:, after:, newer_than:). account is the slug. Returns metadata, one message per line.",
        {
            "account": str,
            "query": str,
            "max_results": int,
        },
    )
    async def _gmail_search(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.gmail_search(
            account=args["account"],
            query=args["query"],
            max_results=int(args.get("max_results") or 20),
            conv_key=conv_key,
        )

    @tool(
        "gmail_read",
        "Read one message's headers and text body. message_id is the id returned by gmail_list_unread or gmail_search. Body is text/plain preferred, truncated at 8000 chars.",
        {
            "account": str,
            "message_id": str,
        },
    )
    async def _gmail_read(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.gmail_read(
            account=args["account"],
            message_id=args["message_id"],
            conv_key=conv_key,
        )

    @tool(
        "calendar_list_events",
        "List calendar events on the primary calendar between two ISO-8601 timestamps. account is the slug. Both bounds must include timezone or are treated as UTC.",
        {
            "account": str,
            "start_iso": str,
            "end_iso": str,
            "max_results": int,
        },
    )
    async def _calendar_list_events(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.calendar_list_events(
            account=args["account"],
            start_iso=args["start_iso"],
            end_iso=args["end_iso"],
            max_results=int(args.get("max_results") or 50),
            conv_key=conv_key,
        )

    @tool(
        "calendar_today",
        "Convenience: list events from now through end-of-day in the given timezone. account is the slug. tz_name defaults to America/New_York.",
        {
            "account": str,
            "tz_name": str,
        },
    )
    async def _calendar_today(args: dict[str, Any]) -> dict[str, Any]:
        return await tools.calendar_today(
            account=args["account"],
            tz_name=args.get("tz_name") or "America/New_York",
            conv_key=conv_key,
        )

    return create_sdk_mcp_server(
        name="anna_google",
        version="1.0.0",
        tools=[
            _google_accounts_list,
            _gmail_list_unread,
            _gmail_search,
            _gmail_read,
            _calendar_list_events,
            _calendar_today,
        ],
    )


__all__ = [
    "GOOGLE_TOOL_NAMES",
    "GoogleTools",
    "build_google_server",
]
