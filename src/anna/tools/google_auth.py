"""Google OAuth + service-account credential management.

This module owns every read or write of the credential files under
``$ANNA_HOME/state/google/``. Two flavors:

* OAuth user-consent (personal Gmail accounts). The OAuth client JSON
  describes the GCP-side Desktop app; the per-account refresh token is
  captured by the setup CLI and persisted to disk. ``load_oauth_credentials``
  reads the refresh token, refreshes the access token if needed, and
  returns a ``google.oauth2.credentials.Credentials`` object.

* Service account with domain-wide delegation (Workspace accounts).
  ``load_service_account_credentials`` reads the SA key JSON, then
  applies ``with_subject(email)`` so requests act as the configured user.

The module is import-safe even if ``google-api-python-client`` is not
installed; the actual imports are deferred until a function is called.
This lets unit tests that mock the credential loaders run on a fresh
checkout without google deps installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.log import get_logger

if TYPE_CHECKING:
    # Pure typing imports — these names are referenced in annotations only,
    # so the real package does not need to be installed at module load time.
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google.oauth2.service_account import Credentials as SACredentials

    AnyGoogleCredentials = OAuthCredentials | SACredentials


# The two scopes that cover phase-1 read-only use. Listed in alphabetical
# order so the comparison strings sent to Google match cached consents.
READONLY_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
)


class GoogleAuthError(RuntimeError):
    """Raised when credentials cannot be loaded or refreshed."""


class TokenMissingError(GoogleAuthError):
    """Raised when an OAuth slug has not been authorized yet.

    The setup CLI catches this and prints the ``add <slug>`` instructions.
    """


def _ensure_state_dir(config: AnnaConfig) -> Path:
    """Create the credential state dir if missing, lock it to 700."""
    path = config.google_state_dir
    path.mkdir(parents=True, exist_ok=True)
    try:
        # Best-effort perm tightening; only matters on POSIX.
        path.chmod(0o700)
    except OSError:
        pass
    return path


def find_account(config: AnnaConfig, slug: str) -> GoogleAccountConfig:
    """Look up an account by slug. Raises if not configured."""
    for acct in config.google.accounts:
        if acct.slug == slug:
            return acct
    raise GoogleAuthError(
        f"no google account with slug {slug!r} in anna.yaml; "
        f"configured slugs: {[a.slug for a in config.google.accounts] or '(none)'}"
    )


def load_oauth_credentials(
    config: AnnaConfig,
    account: GoogleAccountConfig,
) -> "OAuthCredentials":
    """Load and refresh OAuth credentials for a personal account.

    Reads the persisted token JSON at ``state/google/token_<slug>.json``,
    refreshes the access token if it has expired, writes the refreshed
    token back to disk (so the next call sees the new expiry), and
    returns the ready-to-use Credentials.

    Raises :class:`TokenMissingError` if the per-account token file does
    not exist yet — the caller (typically the setup CLI) should run the
    ``add`` flow.
    """
    if account.auth_type != "oauth":
        raise GoogleAuthError(
            f"account {account.slug!r} has auth_type={account.auth_type!r}, "
            f"expected 'oauth'"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    log = get_logger("anna.google.auth").bind(slug=account.slug)
    token_path = config.google_token_path(account)

    if not token_path.is_file():
        raise TokenMissingError(
            f"no OAuth token for slug {account.slug!r}; "
            f"run: python -m anna.setup.google_auth add {account.slug}"
        )

    creds = Credentials.from_authorized_user_file(
        str(token_path),
        list(READONLY_SCOPES),
    )

    # Refresh if needed. The library exposes a `valid` flag for tokens
    # that are present and not expired; otherwise we attempt a refresh.
    if not creds.valid:
        if not creds.refresh_token:
            raise GoogleAuthError(
                f"OAuth token for {account.slug!r} is invalid and has no "
                f"refresh_token; re-run: python -m anna.setup.google_auth add {account.slug}"
            )
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleAuthError(
                f"failed to refresh OAuth token for {account.slug!r}: {exc}; "
                f"re-run: python -m anna.setup.google_auth add {account.slug}"
            ) from exc
        # Persist the refreshed expiry so we don't refresh on every call.
        try:
            token_path.write_text(creds.to_json(), encoding="utf-8")
            try:
                token_path.chmod(0o600)
            except OSError:
                pass
            log.debug("google.oauth.refresh_persisted")
        except OSError as exc:
            log.warning("google.oauth.refresh_persist_failed", error=str(exc))

    return creds


def load_service_account_credentials(
    config: AnnaConfig,
    account: GoogleAccountConfig,
) -> "SACredentials":
    """Load a service-account key and apply domain-wide-delegation impersonation.

    Per-Workspace DWD must have been authorized in the Workspace admin
    console with the same scopes we request here. If not, every API call
    will return HTTP 403 ``unauthorized_client``.
    """
    if account.auth_type != "service_account":
        raise GoogleAuthError(
            f"account {account.slug!r} has auth_type={account.auth_type!r}, "
            f"expected 'service_account'"
        )

    # Check key file existence before importing google libs so a missing
    # file gives a clear runbook-friendly error even when the libs aren't
    # installed yet (e.g. fresh dev checkout before `uv sync`).
    key_path = config.resolve_google_credentials_path(account)
    if not key_path.is_file():
        raise GoogleAuthError(
            f"service-account key for {account.slug!r} not found at {key_path}; "
            f"see Inbox/2026-06-01-ANNA-Google-Workspace-OAuth-Setup.md"
        )

    from google.oauth2 import service_account

    base_creds = service_account.Credentials.from_service_account_file(
        str(key_path),
        scopes=list(READONLY_SCOPES),
    )
    # Domain-wide delegation: impersonate the configured user.
    return base_creds.with_subject(account.email)


def load_credentials(
    config: AnnaConfig,
    account: GoogleAccountConfig,
) -> "AnyGoogleCredentials":
    """Auth-type-dispatching loader. The high-level entry point."""
    if account.auth_type == "oauth":
        return load_oauth_credentials(config, account)
    if account.auth_type == "service_account":
        return load_service_account_credentials(config, account)
    raise GoogleAuthError(
        f"unknown auth_type {account.auth_type!r} for account {account.slug!r}"
    )


def run_oauth_install_flow(
    config: AnnaConfig,
    account: GoogleAccountConfig,
    *,
    open_browser: bool = True,
) -> Path:
    """Drive the OAuth installed-app flow for one account.

    Spawns the local-server flow from ``google_auth_oauthlib``. The user
    completes consent in a browser; on success the resulting token is
    written to ``state/google/token_<slug>.json`` and the path is
    returned. Raises :class:`GoogleAuthError` on any failure.
    """
    if account.auth_type != "oauth":
        raise GoogleAuthError(
            f"run_oauth_install_flow called on non-oauth account {account.slug!r}"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    _ensure_state_dir(config)
    client_path = config.resolve_google_credentials_path(account)
    if not client_path.is_file():
        raise GoogleAuthError(
            f"OAuth client JSON not found at {client_path}; "
            f"download from GCP Console -> APIs & Services -> Credentials"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_path),
        list(READONLY_SCOPES),
    )
    # ``run_local_server`` spins up a one-shot HTTP listener on a free
    # local port and prints the consent URL. ``access_type='offline'``
    # ensures Google returns a refresh_token. ``prompt='consent'``
    # forces the consent screen even if the user has previously
    # granted, which guarantees we get a fresh refresh_token (Google
    # only returns one on first consent or when explicitly forced).
    creds = flow.run_local_server(
        port=0,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
    )

    token_path = config.google_token_path(account)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return token_path


def credentials_email(creds: Any) -> str:
    """Best-effort: pull the authenticated user email from a Credentials.

    Used by the verify CLI to confirm the impersonation/auth actually
    landed on the expected mailbox. For service-account creds the
    ``signer_email`` is the SA itself, not the impersonated subject, so
    we prefer the ``subject`` attribute when present.
    """
    subject = getattr(creds, "_subject", None) or getattr(creds, "subject", None)
    if subject:
        return str(subject)
    # OAuth user creds expose ``id_token`` claims after a refresh; the
    # ``email`` claim is not guaranteed but is usually there for the
    # scopes we request. Falling back to "(unknown)" is fine — verify()
    # makes a profile API call anyway.
    return "(unknown)"
