"""Daemon-side live verification of one Google account through its serialized owner lane."""

from __future__ import annotations

from typing import Any

from anna.config import AnnaConfig
from anna.tools.google_auth import find_account


async def verify_account(clients: Any, config: AnnaConfig, slug: str) -> dict[str, Any]:
    """Read-only: Gmail profile / primary calendar. Reports error CLASSES and an identity match flag, no addresses."""
    account = find_account(config, slug)
    result: dict[str, Any] = {"services": {}, "identity": "unchecked"}
    emails: list[str] = []
    for service in account.services:
        try:
            if service == "gmail":
                prof = await clients.run(slug, "gmail", lambda s: s.users().getProfile(userId="me").execute())
                emails.append(str(prof.get("emailAddress", "")))
            else:
                cal = await clients.run(slug, "calendar", lambda s: s.calendars().get(calendarId="primary").execute())
                emails.append(str(cal.get("id", "")))
            result["services"][service] = "ok"
        except Exception as exc:  # noqa: BLE001 - class name only, never the message
            result["services"][service] = type(exc).__name__
    if emails:
        result["identity"] = "match" if all(e.lower() == account.email.lower() for e in emails) else "MISMATCH"
    return result
