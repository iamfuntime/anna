"""CLI for one-time Google account authorization.

Usage::

    python -m anna.setup.google_auth list
    python -m anna.setup.google_auth add <slug>
    python -m anna.setup.google_auth verify <slug>

``list`` enumerates every account in ``anna.yaml -> google.accounts`` with
its email, auth_type, and whether credentials are usable right now.

``add`` drives the OAuth installed-app flow for one slug. Personal Gmail
only; service-account slugs are rejected because they do not need a
browser flow. The resulting refresh token is written to
``state/google/token_<slug>.json`` with 600 perms.

``verify`` confirms that ANNA can actually impersonate / authenticate the
account by making a no-op profile + calendarList call against Gmail and
Calendar respectively. Works for both auth types.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from anna.config import AnnaConfig, GoogleAccountConfig, load_config
from anna.tools.google_auth import (
    GoogleAuthError,
    TokenMissingError,
    find_account,
    load_credentials,
    run_oauth_install_flow,
)


def _print_ok(msg: str) -> None:
    sys.stdout.write(f"OK    {msg}\n")
    sys.stdout.flush()


def _print_warn(msg: str) -> None:
    sys.stdout.write(f"WARN  {msg}\n")
    sys.stdout.flush()


def _print_err(msg: str) -> None:
    sys.stderr.write(f"ERROR {msg}\n")
    sys.stderr.flush()


def _credentials_status(config: AnnaConfig, account: GoogleAccountConfig) -> str:
    """One-line status: 'ready', 'missing token', 'no key file', etc."""
    try:
        load_credentials(config, account)
        return "ready"
    except TokenMissingError:
        return "missing token (run: add)"
    except GoogleAuthError as exc:
        return f"error: {exc}"
    except FileNotFoundError as exc:
        return f"file missing: {exc}"
    except Exception as exc:
        return f"error: {exc}"


def cmd_list(args: argparse.Namespace, config: AnnaConfig) -> int:
    accts = config.google.accounts
    if not accts:
        print("(no google accounts configured in anna.yaml)")
        return 0
    print(f"{len(accts)} account(s):")
    for a in accts:
        status = _credentials_status(config, a)
        cred_path = config.resolve_google_credentials_path(a)
        print(
            f"  - {a.slug:20s}  {a.email:40s}  {a.auth_type:16s}  "
            f"creds={cred_path}  status={status}"
        )
    return 0


def cmd_add(args: argparse.Namespace, config: AnnaConfig) -> int:
    slug: str = args.slug
    try:
        account = find_account(config, slug)
    except GoogleAuthError as exc:
        _print_err(str(exc))
        return 2

    if account.auth_type != "oauth":
        _print_err(
            f"add only applies to oauth accounts; slug {slug!r} is "
            f"auth_type={account.auth_type!r}. Use 'verify {slug}' instead."
        )
        return 2

    client_path = config.resolve_google_credentials_path(account)
    if not client_path.is_file():
        _print_err(
            f"OAuth client JSON not found at {client_path}\n"
            f"Download it from GCP Console -> APIs & Services -> Credentials, "
            f"then drop the file at that path with 600 perms."
        )
        return 2

    _print_ok(
        f"starting OAuth flow for {slug} ({account.email}). "
        f"A browser tab will open."
    )
    _print_warn(
        "expect a Google 'unverified app' warning — click Advanced -> "
        "Go to ANNA (unsafe). This is normal for External + Published, unverified."
    )
    try:
        token_path = run_oauth_install_flow(
            config=config,
            account=account,
            open_browser=not args.no_browser,
        )
    except GoogleAuthError as exc:
        _print_err(str(exc))
        return 2
    except Exception as exc:
        _print_err(f"OAuth flow failed: {exc}")
        return 2

    _print_ok(f"token persisted to {token_path}")
    return cmd_verify(args, config)


def cmd_verify(args: argparse.Namespace, config: AnnaConfig) -> int:
    slug: str = args.slug
    try:
        account = find_account(config, slug)
    except GoogleAuthError as exc:
        _print_err(str(exc))
        return 2

    try:
        creds = load_credentials(config, account)
    except GoogleAuthError as exc:
        _print_err(f"credential load failed for {slug}: {exc}")
        return 2

    # Lazy-import googleapiclient.discovery; the deps may not be available
    # in environments where the CLI is only used for `list`.
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        _print_err(
            f"google-api-python-client not installed: {exc}; "
            f"install with: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )
        return 2

    # Gmail no-op: getProfile returns emailAddress, historyId, messagesTotal.
    try:
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = gmail.users().getProfile(userId="me").execute()
    except Exception as exc:
        _print_err(f"gmail.getProfile failed for {slug}: {exc}")
        return 3
    actual_email = profile.get("emailAddress", "?")
    if actual_email.lower() != account.email.lower():
        _print_warn(
            f"gmail authenticated as {actual_email}, but anna.yaml says "
            f"{account.email}. Service-account impersonation subject may "
            f"not match."
        )
    _print_ok(
        f"gmail OK: {actual_email}, messagesTotal={profile.get('messagesTotal')}"
    )

    # Calendar no-op: list primary calendar.
    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        primary = cal.calendars().get(calendarId="primary").execute()
    except Exception as exc:
        _print_err(f"calendar.calendars.get failed for {slug}: {exc}")
        return 3
    _print_ok(
        f"calendar OK: {primary.get('summary', '(no summary)')}, "
        f"tz={primary.get('timeZone', '?')}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m anna.setup.google_auth",
        description="One-time authorization for ANNA's Google accounts.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_list = subparsers.add_parser("list", help="Show every configured account.")
    p_list.set_defaults(func=cmd_list)

    p_add = subparsers.add_parser(
        "add",
        help="Run the OAuth browser flow for a personal Gmail account.",
    )
    p_add.add_argument("slug", help="Account slug from anna.yaml")
    p_add.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open a browser; print the URL instead.",
    )
    p_add.set_defaults(func=cmd_add)

    p_verify = subparsers.add_parser(
        "verify",
        help="Confirm an account's credentials work end-to-end.",
    )
    p_verify.add_argument("slug", help="Account slug from anna.yaml")
    p_verify.add_argument(
        "--no-browser",
        action="store_true",
        help="(Unused for verify; accepted for symmetry with add.)",
    )
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        config = load_config()
    except Exception as exc:
        _print_err(f"failed to load anna.yaml: {exc}")
        return 2
    if not config.google.enabled:
        _print_warn(
            "google.enabled is false in anna.yaml; the CLI will still run, "
            "but ANNA's worker will not mount the google MCP server until "
            "you set it true and restart."
        )
    return int(args.func(args, config))


if __name__ == "__main__":
    raise SystemExit(main())
